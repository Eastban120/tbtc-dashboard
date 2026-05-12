"""
sync_token.py — sync tBTC token Transfer events filtered to mints and burns.

Mints: Transfer(from=0x0)  → tBTC was minted to a user
Burns: Transfer(to=0x0)    → tBTC was burned (redemption)

Two filtered queries per chunk. amount_wei is uint256 (1e18 decimals)
so we store as TEXT to avoid SQLite int64 overflow.
"""

import sys
import time
from datetime import datetime, timezone

import config
from chain import w3
from contracts import tbtc_token
from db import connect, get_sync_state, init_db, set_sync_state


# Same genesis as the rest of the project. The first tBTC transfer is
# a few hundred thousand blocks later, but those scans are essentially free
# (zero matching events) and not worth a separate constant.
TOKEN_GENESIS_BLOCK = 16_000_000
PROGRESS_EVERY = 50

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
from contracts import tbtc_token
from db import connect, get_sync_state, init_db, set_sync_state


# Same genesis as the rest of the project. The first tBTC transfer is
# a few hundred thousand blocks later, but those scans are essentially free
# (zero matching events) and not worth a separate constant.
TOKEN_GENESIS_BLOCK = 16_000_000
PROGRESS_EVERY = 50

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Tolerance for considering supply "reconciled": 0.001 tBTC.
# Anything below this is rounding noise; anything above warrants investigation.
RECONCILE_TOLERANCE_WEI = 10**15

# tBTC v2 token has supply that predates our Bridge genesis (from the
# VendingMachine v1→v2 migration in 2021). We compute this lazily on first
# use rather than hardcoding it — auto-corrects if TOKEN_GENESIS_BLOCK changes.
_pre_genesis_baseline_wei: int | None = None

def pre_genesis_baseline_wei() -> int:
    """totalSupply() at TOKEN_GENESIS_BLOCK — supply we don't see in our event sync."""
    global _pre_genesis_baseline_wei
    if _pre_genesis_baseline_wei is None:
        _pre_genesis_baseline_wei = tbtc_token.functions.totalSupply().call(
            block_identifier=TOKEN_GENESIS_BLOCK
        )
    return _pre_genesis_baseline_wei


def fetch_token_events(from_block: int, to_block: int):
    """Two filtered queries — RPC does the indexed-topic filtering."""
    mints = tbtc_token.events.Transfer.get_logs(
        from_block=from_block, to_block=to_block,
        argument_filters={"from": ZERO_ADDRESS},
    )
    burns = tbtc_token.events.Transfer.get_logs(
        from_block=from_block, to_block=to_block,
        argument_filters={"to": ZERO_ADDRESS},
    )

    # Sanity check: mint activity on tBTC is near-continuous since late 2022.
    # A 10k-block window (~33 hours) with zero mints AND zero burns is
    # essentially impossible during active periods and likely indicates a
    # silent RPC empty-response. Raise so the retry-with-smaller-chunk kicks in.
    window = to_block - from_block + 1
    if window >= 5000 and len(mints) == 0 and len(burns) == 0 and from_block >= 16_200_000:
        raise RuntimeError(
            f"Suspicious: 0 mints AND 0 burns in {from_block:,}–{to_block:,} "
            f"({window} blocks). Likely a silent RPC empty-response; raising to retry."
        )
    
    return mints, burns


_block_time_cache: dict[int, int] = {}

def block_time(block_number: int) -> int:
    if block_number not in _block_time_cache:
        _block_time_cache[block_number] = w3.eth.get_block(block_number).timestamp
    return _block_time_cache[block_number]


def insert_mint(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO token_transfers
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_wei)
           VALUES (?, ?, ?, ?, 'mint', ?, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args['to'], str(log.args.value)),
    )


def insert_burn(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO token_transfers
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_wei)
           VALUES (?, ?, ?, ?, 'burn', ?, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args['from'], str(log.args.value)),
    )


def sync_token() -> None:
    init_db()

    head = w3.eth.block_number
    start = get_sync_state("token") or TOKEN_GENESIS_BLOCK
    chunk = config.LOG_CHUNK_SIZE
    total_blocks = head - start

    print(f"Syncing tBTC token transfers (mints + burns) "
          f"from block {start:,} → {head:,} ({total_blocks:,} blocks)")
    if total_blocks > 100_000:
        eta_min = total_blocks / chunk / 60
        print(f"Fresh sync. Estimated time: ~{eta_min:.0f} minutes.\n")

    cursor = start
    chunks_done = 0
    started = time.time()
    counts = {"mint": 0, "burn": 0}

    while cursor <= head:
        to = min(cursor + chunk - 1, head)
        mints = burns = None
        for attempt_chunk in (chunk, max(200, chunk // 2), max(100, chunk // 5)):
            attempt_to = min(cursor + attempt_chunk - 1, head)
            try:
                mints, burns = fetch_token_events(cursor, attempt_to)
                to = attempt_to
                if attempt_chunk != chunk:
                    print(f"    retry succeeded with smaller chunk ({attempt_chunk})")
                break
            except Exception as e:
                print(f"  ✗ {cursor:,} → {attempt_to:,}  failed: {e!s:.80}")
                time.sleep(1.0)
        else:
            raise RuntimeError(f"all retries failed at block {cursor:,}")

        with connect() as conn:
            for log in mints:
                insert_mint(log, conn); counts["mint"] += 1
            for log in burns:
                insert_burn(log, conn); counts["burn"] += 1

        set_sync_state("token", to)

        chunks_done += 1
        if chunks_done % PROGRESS_EVERY == 0 or to == head:
            elapsed = time.time() - started
            blocks_done = to - start
            rate = blocks_done / elapsed if elapsed else 0
            remaining = (head - to) / rate if rate else 0
            print(f"  {to:,} ({100*blocks_done/total_blocks:.1f}%)  "
                  f"M={counts['mint']} B={counts['burn']}  "
                  f"~{remaining/60:.1f}m left")

        cursor = to + 1
        time.sleep(0.1)

    print(f"\nDone. {counts}")
    # Post-sync reconciliation: catch silent misses immediately.
    try:
        live_supply = tbtc_token.functions.totalSupply().call(block_identifier=head)
        mint_rows = []
        burn_rows = []
        with connect() as conn:
            mint_rows = conn.execute(
                "SELECT amount_wei FROM token_transfers "
                "WHERE direction='mint' AND block_number <= ?",
                (head,)
            ).fetchall()
            burn_rows = conn.execute(
                "SELECT amount_wei FROM token_transfers "
                "WHERE direction='burn' AND block_number <= ?",
                (head,)
            ).fetchall()
        event_supply = sum(int(r['amount_wei']) for r in mint_rows) \
                     - sum(int(r['amount_wei']) for r in burn_rows)
        drift = (live_supply - event_supply) - pre_genesis_baseline_wei()
        if abs(drift) < RECONCILE_TOLERANCE_WEI:
            print(f"✓ Supply reconciled at block {head:,} "
                  f"(drift {drift/1e18:+.6f} tBTC)")
        else:
            print(f"⚠ Drift {drift/1e18:+.4f} tBTC at block {head:,} — "
                  f"run `python reconcile_token.py` to bisect and backfill")
    except Exception as e:
        print(f"(post-sync verification skipped: {e!s:.60})")


def report() -> None:
    with connect() as conn:
        last = get_sync_state("token")
        head = w3.eth.block_number
        print(f"Chain head : {head:,}")
        print(f"Last synced: {last:,}" if last else "Last synced: (none)")

        print("\n─── token_transfers ────────────────────────────────────")
        rows = conn.execute(
            """SELECT direction, COUNT(*) AS n,
                      MIN(block_time) AS first_t, MAX(block_time) AS last_t
               FROM token_transfers GROUP BY direction"""
        ).fetchall()
        for r in rows:
            first = datetime.fromtimestamp(r['first_t'], tz=timezone.utc).strftime("%Y-%m-%d")
            last_d = datetime.fromtimestamp(r['last_t'], tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"  {r['direction']:<8}: {r['n']:>6,} events  ({first} → {last_d})")

        # Sum mints and burns. amount_wei is TEXT so we sum in Python with int().
        mint_rows = conn.execute(
            "SELECT amount_wei FROM token_transfers WHERE direction='mint'"
        ).fetchall()
        burn_rows = conn.execute(
            "SELECT amount_wei FROM token_transfers WHERE direction='burn'"
        ).fetchall()
        total_minted_wei = sum(int(r['amount_wei']) for r in mint_rows)
        total_burned_wei = sum(int(r['amount_wei']) for r in burn_rows)
        circulating_wei  = total_minted_wei - total_burned_wei

        print(f"\n─── Supply ─────────────────────────────────────────────")
        print(f"  Total minted    : {total_minted_wei / 1e18:>14,.4f} tBTC")
        print(f"  Total burned    : {total_burned_wei / 1e18:>14,.4f} tBTC")
        print(f"  Circulating     : {circulating_wei  / 1e18:>14,.4f} tBTC")

        # Cross-check against live totalSupply at chain head.
        try:
            live_supply_wei = tbtc_token.functions.totalSupply().call()
            gap_wei = live_supply_wei - circulating_wei
            print(f"  Live totalSupply: {live_supply_wei / 1e18:>14,.4f} tBTC")
            print(f"  Gap             : {gap_wei         / 1e18:>14,.6f} tBTC")
            adjusted_gap = gap_wei - pre_genesis_baseline_wei()
            if abs(adjusted_gap) < RECONCILE_TOLERANCE_WEI:
                print(f"  ✓ Reconciled (drift {adjusted_gap/1e18:+.6f} tBTC within tolerance)")
            else:
                print(f"  ⚠ Unexplained drift: {adjusted_gap/1e18:+.4f} tBTC beyond baseline")
                print(f"    → run `python reconcile_token.py` to bisect and backfill")
        except Exception as e:
            print(f"  (skipping live totalSupply check: {e!s:.60})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    else:
        sync_token()
        report()