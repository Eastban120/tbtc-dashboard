"""
sync_bridge.py — sync Bridge events into bridge_events table.

Pulls three event types in lockstep:
  - DepositRevealed     (every deposit reveal — captures volume regardless of fee)
  - RedemptionRequested (every redemption — fee_sats comes from the event)
  - DepositsSwept       (sweep transactions — used to resolve deposit fees later)

Resumable: re-runs from last sync watermark.
"""

import sys
import time
from datetime import datetime, timezone

import config
from chain import w3
from contracts import bridge
from db import connect, get_sync_state, init_db, set_sync_state


BRIDGE_GENESIS_BLOCK = 16_000_000   # see parameters.py for rationale
PROGRESS_EVERY = 50                  # print a heartbeat every N chunks


def fetch_bridge_events(from_block: int, to_block: int):
    """One RPC trip per event type, parallel-safe per chunk."""
    revealed = bridge.events.DepositRevealed.get_logs(
        from_block=from_block, to_block=to_block,
    )
    requested = bridge.events.RedemptionRequested.get_logs(
        from_block=from_block, to_block=to_block,
    )
    swept = bridge.events.DepositsSwept.get_logs(
        from_block=from_block, to_block=to_block,
    )

    # Sanity check: bridge activity is near-continuous since ~16.2M. A large
    # window with zero of all three event types past that point is suspicious.
    window = to_block - from_block + 1
    if (window >= 1000 and from_block >= 16_200_000
            and len(revealed) == 0 and len(requested) == 0 and len(swept) == 0):
        raise RuntimeError(
            f"Suspicious: 0 bridge events in {from_block:,}–{to_block:,} "
            f"({window} blocks). Likely a silent RPC empty-response; raising to retry."
        )

    return revealed, requested, swept


# Block timestamps are constant for a given block, so we cache them
# in a tiny in-memory dict to avoid re-fetching the same block when
# multiple events share it.
_block_time_cache: dict[int, int] = {}

def block_time(block_number: int) -> int:
    if block_number not in _block_time_cache:
        _block_time_cache[block_number] = w3.eth.get_block(block_number).timestamp
    return _block_time_cache[block_number]


def deposit_key(funding_tx_hash: bytes, funding_output_index: int) -> str:
    """
    The bridge identifies deposits by keccak256(fundingTxHash, fundingOutputIndex).
    We compute it here so we can later match a DepositRevealed to the row in
    bridge.deposits() storage.
    """
    return w3.keccak(
        funding_tx_hash + funding_output_index.to_bytes(32, "big")
    ).hex()


def insert_revealed(log, conn) -> None:
    args = log.args
    conn.execute(
        """
        INSERT OR IGNORE INTO bridge_events
            (tx_hash, log_index, block_number, block_time, event_type,
             actor, vault, amount_sats, fee_sats, deposit_key, wallet_pubkey_hash)
        VALUES (?, ?, ?, ?, 'deposit_revealed', ?, ?, ?, NULL, ?, ?)
        """,
        (
            log.transactionHash.hex(),
            log.logIndex,
            log.blockNumber,
            block_time(log.blockNumber),
            args.depositor,
            args.vault,
            args.amount,
            deposit_key(args.fundingTxHash, args.fundingOutputIndex),
            "0x" + args.walletPubKeyHash.hex(),
        ),
    )


def insert_redemption(log, conn) -> None:
    args = log.args
    conn.execute(
        """
        INSERT OR IGNORE INTO bridge_events
            (tx_hash, log_index, block_number, block_time, event_type,
             actor, vault, amount_sats, fee_sats, deposit_key, wallet_pubkey_hash)
        VALUES (?, ?, ?, ?, 'redemption_requested', ?, NULL, ?, ?, NULL, ?)
        """,
        (
            log.transactionHash.hex(),
            log.logIndex,
            log.blockNumber,
            block_time(log.blockNumber),
            args.redeemer,
            args.requestedAmount,
            args.treasuryFee,           # 0 for waivers, > 0 otherwise
            "0x" + args.walletPubKeyHash.hex(),
        ),
    )


def insert_swept(log, conn) -> None:
    args = log.args
    conn.execute(
        """
        INSERT OR IGNORE INTO bridge_events
            (tx_hash, log_index, block_number, block_time, event_type,
             actor, vault, amount_sats, fee_sats, deposit_key, wallet_pubkey_hash)
        VALUES (?, ?, ?, ?, 'deposits_swept', NULL, NULL, NULL, NULL, NULL, ?)
        """,
        (
            log.transactionHash.hex(),
            log.logIndex,
            log.blockNumber,
            block_time(log.blockNumber),
            "0x" + args.walletPubKeyHash.hex(),
        ),
    )



def sync_bridge() -> None:
    init_db()

    head = w3.eth.block_number
    start = get_sync_state("bridge") or BRIDGE_GENESIS_BLOCK
    chunk = config.LOG_CHUNK_SIZE
    total_blocks = head - start

    print(f"Syncing bridge events from block {start:,} → {head:,} "
          f"({total_blocks:,} blocks, {chunk}-block chunks)")
    if total_blocks > 100_000:
        eta_min = total_blocks / chunk / 60   # ~1 chunk per second
        print(f"This is a fresh sync. Estimated time: ~{eta_min:.0f} minutes.\n")

    cursor = start
    chunks_done = 0
    started = time.time()
    counts = {"revealed": 0, "requested": 0, "swept": 0}

    while cursor <= head:
        to = min(cursor + chunk - 1, head)
        revealed = requested = swept = None
        for attempt_chunk in (chunk, max(200, chunk // 2), max(100, chunk // 5)):
            attempt_to = min(cursor + attempt_chunk - 1, head)
            try:
                revealed, requested, swept = fetch_bridge_events(cursor, attempt_to)
                to = attempt_to
                if attempt_chunk != chunk:
                    print(f"    retry succeeded with smaller chunk ({attempt_chunk})")
                break
            except Exception as e:
                print(f"  ✗ {cursor:,} → {attempt_to:,}  failed: {e!s:.80}")
                time.sleep(1.0)
        else:
            raise RuntimeError(f"all retries failed at block {cursor:,}")

        # All inserts for this chunk go in one transaction (the with-block).
        with connect() as conn:
            for log in revealed:
                insert_revealed(log, conn)
                counts["revealed"] += 1
            for log in requested:
                insert_redemption(log, conn)
                counts["requested"] += 1
            for log in swept:
                insert_swept(log, conn)
                counts["swept"] += 1

        set_sync_state("bridge", to)

        chunks_done += 1
        if chunks_done % PROGRESS_EVERY == 0 or to == head:
            elapsed = time.time() - started
            blocks_done = to - start
            rate = blocks_done / elapsed if elapsed else 0
            remaining = (head - to) / rate if rate else 0
            print(f"  {to:,} ({100*blocks_done/total_blocks:.1f}%)  "
                  f"R={counts['revealed']} Q={counts['requested']} S={counts['swept']}  "
                  f"~{remaining/60:.1f}m left")

        cursor = to + 1
        time.sleep(0.1)

    print(f"\nDone. {counts}")


def report() -> None:
    with connect() as conn:
        head = w3.eth.block_number
        last = get_sync_state("bridge")
        print(f"Chain head     : {head:,}")
        print(f"Last synced    : {last:,}" if last else "Last synced    : (none)")
        if last:
            print(f"Lag            : {head - last:,} blocks")

        print("\n─── bridge_events ────────────────────────────────────────")
        rows = conn.execute(
            """
            SELECT event_type, COUNT(*) AS n,
                   MIN(block_time) AS first_t, MAX(block_time) AS last_t
            FROM bridge_events GROUP BY event_type
            """
        ).fetchall()
        for r in rows:
            first = datetime.fromtimestamp(r['first_t'], tz=timezone.utc).strftime("%Y-%m-%d") if r['first_t'] else '-'
            last_ = datetime.fromtimestamp(r['last_t'], tz=timezone.utc).strftime("%Y-%m-%d") if r['last_t'] else '-'
            print(f"  {r['event_type']:<22}: {r['n']:>8,}  ({first} → {last_})")

        # Quick volume sanity check
        deposit_volume = conn.execute(
            "SELECT COALESCE(SUM(amount_sats),0) AS v FROM bridge_events WHERE event_type='deposit_revealed'"
        ).fetchone()['v']
        redemption_volume = conn.execute(
            "SELECT COALESCE(SUM(amount_sats),0) AS v FROM bridge_events WHERE event_type='redemption_requested'"
        ).fetchone()['v']

        print(f"\nDeposit volume    : {deposit_volume / 1e8:>14,.4f} BTC")
        print(f"Redemption volume : {redemption_volume / 1e8:>14,.4f} BTC")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    else:
        sync_bridge()
        report()