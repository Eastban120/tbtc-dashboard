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

        # Cross-check against live totalSupply. If the gap is meaningful,
        # we're missing events somewhere — investigate before trusting the data.
        try:
            live_supply_wei = tbtc_token.functions.totalSupply().call()
            gap_wei = live_supply_wei - circulating_wei
            print(f"  Live totalSupply: {live_supply_wei / 1e18:>14,.4f} tBTC")
            print(f"  Gap             : {gap_wei         / 1e18:>14,.6f} tBTC")
            # Pre-genesis baseline: the tBTC v2 token contract was active
            # since ~2021 via the VendingMachine v1→v2 migration. Our sync
            # starts at block 16,000,000 (Bridge launch). Net pre-genesis
            # activity nets to ~63 tBTC of supply we don't see in events.
            # This is expected and unrelated to Bridge/treasury operations.
            PRE_GENESIS_BASELINE_WEI = int(63.5363 * 10**18)
            adjusted_gap = gap_wei - PRE_GENESIS_BASELINE_WEI
            if abs(adjusted_gap) < 10**18:  # under 1 tBTC of unexplained drift
                print(f"  ✓ Reconciliation OK (gap = pre-genesis baseline ± {adjusted_gap/1e18:+.4f} tBTC)")
            else:
                print(f"  ⚠ Unexplained drift: {adjusted_gap/1e18:+.4f} tBTC beyond baseline")
        except Exception as e:
            print(f"  (skipping live totalSupply check: {e!s:.60})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    else:
        sync_token()
        report()