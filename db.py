"""
db.py — schema and connection helpers for the treasury database.

One file owns: the schema, the connection-getter, and a few convenience
helpers (insert_many, get_sync_state). Every other module imports from here.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import config


# Schema is a single string so we can apply it idempotently with executescript.
# Every CREATE uses IF NOT EXISTS, every INSERT uses INSERT OR IGNORE.
# That means running this file twice is safe — it never destroys data.
SCHEMA = """
-- ─── Bridge activity ──────────────────────────────────────────────
-- Every deposit reveal, redemption request, and sweep we've seen.
-- (tx_hash, log_index) is unique on Ethereum, making it a safe primary key.
CREATE TABLE IF NOT EXISTS bridge_events (
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL,
    block_number        INTEGER NOT NULL,
    block_time          INTEGER NOT NULL,        -- unix seconds
    event_type          TEXT NOT NULL,           -- 'deposit_revealed'|'redemption_requested'|'deposits_swept'
    actor               TEXT,                    -- depositor/redeemer; NULL for sweeps
    vault               TEXT,                    -- destination vault (deposits only)
    amount_sats         INTEGER,                 -- requested amount; NULL for sweeps
    fee_sats            INTEGER,                 -- redemption: from event (0 = waived); deposit: NULL until resolved
    deposit_key         TEXT,                    -- keccak256(fundingTxHash, fundingOutputIndex) for deposits
    wallet_pubkey_hash  TEXT,                    -- needed to match deposits to sweeps
    PRIMARY KEY (tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_bridge_block_time ON bridge_events(block_time);
CREATE INDEX IF NOT EXISTS idx_bridge_event_type ON bridge_events(event_type);
CREATE INDEX IF NOT EXISTS idx_bridge_deposit_key ON bridge_events(deposit_key);

-- ─── Bank activity ────────────────────────────────────────────────
-- Treasury inflows and outflows on the Bank ledger.
-- Source of truth for "what hit the treasury and when".
CREATE TABLE IF NOT EXISTS bank_events (
    tx_hash       TEXT NOT NULL,
    log_index     INTEGER NOT NULL,
    block_number  INTEGER NOT NULL,
    block_time    INTEGER NOT NULL,
    direction     TEXT NOT NULL,                 -- 'in'|'out'|'approve'|'decrease'
    counterparty  TEXT,                          -- 'from' or 'to' or 'spender'
    amount_sats   INTEGER NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_bank_block_time ON bank_events(block_time);
CREATE INDEX IF NOT EXISTS idx_bank_direction ON bank_events(direction);

-- ─── Parameter history ────────────────────────────────────────────
-- Every time governance updated deposit or redemption fee parameters.
-- Used to compute "what was the divisor at block X" for historical fee math.
CREATE TABLE IF NOT EXISTS parameter_history (
    block_number  INTEGER NOT NULL,
    block_time    INTEGER NOT NULL,
    source        TEXT NOT NULL,                 -- 'deposit' | 'redemption'
    divisor       INTEGER NOT NULL,
    dust_sats     INTEGER,
    tx_max_fee    INTEGER,
    tx_hash       TEXT NOT NULL,
    PRIMARY KEY (block_number, source, tx_hash)
);

-- ─── Sync watermarks ──────────────────────────────────────────────
-- Last block we've successfully synced for each source. Lets us resume.
CREATE TABLE IF NOT EXISTS sync_state (
    source             TEXT PRIMARY KEY,         -- 'bridge'|'bank'|'parameters'
    last_synced_block  INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL          -- unix seconds, for diagnostics
);
"""


@contextmanager
def connect():
    """
    Yield a sqlite3 connection with sensible defaults.

    Using a context manager (`with connect() as conn`) means we get
    automatic commit on success and rollback on exception — which is
    exactly what we want for our insert-many patterns.
    """
    conn = sqlite3.connect(config.DB_PATH)
    # Return rows as dict-like objects so we can do row['amount_sats']
    # rather than positional row[5].
    conn.row_factory = sqlite3.Row
    # Enforce foreign keys (we don't have any yet, but cheap insurance).
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they don't exist. Idempotent."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def get_sync_state(source: str) -> int | None:
    """Return last_synced_block for a source, or None if never synced."""
    with connect() as conn:
        row = conn.execute(
            "SELECT last_synced_block FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return row["last_synced_block"] if row else None


def set_sync_state(source: str, last_synced_block: int) -> None:
    """Upsert the watermark for a source."""
    import time
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (source, last_synced_block, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE
              SET last_synced_block = excluded.last_synced_block,
                  updated_at        = excluded.updated_at
            """,
            (source, last_synced_block, int(time.time())),
        )


if __name__ == "__main__":
    # Self-test: create the database, then print what's inside.
    print(f"Database path: {config.DB_PATH}")
    init_db()

    with connect() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"Tables created: {[r['name'] for r in tables]}")

        for table in ('bridge_events', 'bank_events', 'parameter_history', 'sync_state'):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            print(f"  {table:<20}: {count['n']} rows")