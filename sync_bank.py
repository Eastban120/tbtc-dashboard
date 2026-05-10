"""
sync_bank.py — sync Bank events scoped to the treasury address.

We want four event types, filtered on the treasury address:
  - BalanceIncreased(owner=treasury)     ← treasury fee credit (in)
  - BalanceDecreased(owner=treasury)     ← burn (out)
  - BalanceTransferred(from=treasury)    ← treasury sending balance (out)
  - BalanceTransferred(to=treasury)      ← incoming transfer (in, rare)

argument_filters tells web3.py to add the matching topic filter at the
RPC level, so we don't have to download every Bank event and discard
the ones not involving the treasury.
"""

import sys
import time
from datetime import datetime, timezone

import config
from chain import w3
from contracts import bank
from db import connect, get_sync_state, init_db, set_sync_state


BRIDGE_GENESIS_BLOCK = 16_000_000
PROGRESS_EVERY = 50

TREASURY = config.TREASURY_ADDRESS


def fetch_bank_events(from_block: int, to_block: int):
    """Unfiltered queries — drpc no longer accepts argument_filters reliably,
    so we fetch all events of each type in the range and post-filter in Python."""
    all_increased = bank.events.BalanceIncreased.get_logs(
        from_block=from_block, to_block=to_block,
    )
    all_decreased = bank.events.BalanceDecreased.get_logs(
        from_block=from_block, to_block=to_block,
    )
    all_transferred = bank.events.BalanceTransferred.get_logs(
        from_block=from_block, to_block=to_block,
    )

    increased = [log for log in all_increased if log.args['owner'] == TREASURY]
    decreased = [log for log in all_decreased if log.args['owner'] == TREASURY]
    transferred_out = [log for log in all_transferred if log.args['from'] == TREASURY]
    transferred_in  = [log for log in all_transferred if log.args['to']   == TREASURY]
    
    # Sanity check: BalanceTransferred fires on essentially every Bridge tx.
    # A 1000-block window with truly zero events is suspect and likely indicates
    # an empty-response bug from the RPC. Raise so the retry logic kicks in.
    if (to_block - from_block) >= 500 and len(all_transferred) == 0:
        raise RuntimeError(
            f"Suspicious: 0 BalanceTransferred events in {from_block:,}–{to_block:,}. "
            "Likely an empty-response bug; raising to trigger retry."
        )

    return increased, decreased, transferred_out, transferred_in

_block_time_cache: dict[int, int] = {}

def block_time(block_number: int) -> int:
    if block_number not in _block_time_cache:
        _block_time_cache[block_number] = w3.eth.get_block(block_number).timestamp
    return _block_time_cache[block_number]


def insert_increased(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO bank_events
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_sats)
           VALUES (?, ?, ?, ?, 'in', NULL, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args.amount),
    )


def insert_decreased(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO bank_events
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_sats)
           VALUES (?, ?, ?, ?, 'decrease', NULL, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args.amount),
    )


def insert_transferred_out(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO bank_events
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_sats)
           VALUES (?, ?, ?, ?, 'out', ?, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args['to'], log.args.amount),
    )


def insert_transferred_in(log, conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO bank_events
           (tx_hash, log_index, block_number, block_time, direction, counterparty, amount_sats)
           VALUES (?, ?, ?, ?, 'in', ?, ?)""",
        (log.transactionHash.hex(), log.logIndex, log.blockNumber,
         block_time(log.blockNumber), log.args['from'], log.args.amount),
    )


def sync_bank() -> None:
    init_db()

    head = w3.eth.block_number
    start = get_sync_state("bank") or BRIDGE_GENESIS_BLOCK
    chunk = config.LOG_CHUNK_SIZE
    total_blocks = head - start

    print(f"Syncing bank events (treasury={TREASURY}) "
          f"from block {start:,} → {head:,} ({total_blocks:,} blocks)")
    if total_blocks > 100_000:
        eta_min = total_blocks / chunk / 60
        print(f"Fresh sync. Estimated time: ~{eta_min:.0f} minutes.\n")

    cursor = start
    chunks_done = 0
    started = time.time()
    counts = {"in": 0, "out": 0, "decrease": 0}

    while cursor <= head:
        to = min(cursor + chunk - 1, head)
        inc = dec = t_out = t_in = None
        for attempt_chunk in (chunk, max(200, chunk // 2), max(100, chunk // 5)):
            attempt_to = min(cursor + attempt_chunk - 1, head)
            try:
                inc, dec, t_out, t_in = fetch_bank_events(cursor, attempt_to)
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
            for log in inc:
                insert_increased(log, conn); counts["in"] += 1
            for log in dec:
                insert_decreased(log, conn); counts["decrease"] += 1
            for log in t_out:
                insert_transferred_out(log, conn); counts["out"] += 1
            for log in t_in:
                insert_transferred_in(log, conn); counts["in"] += 1

        set_sync_state("bank", to)

        chunks_done += 1
        if chunks_done % PROGRESS_EVERY == 0 or to == head:
            elapsed = time.time() - started
            blocks_done = to - start
            rate = blocks_done / elapsed if elapsed else 0
            remaining = (head - to) / rate if rate else 0
            print(f"  {to:,} ({100*blocks_done/total_blocks:.1f}%)  "
                  f"in={counts['in']} out={counts['out']} dec={counts['decrease']}  "
                  f"~{remaining/60:.1f}m left")

        cursor = to + 1
        time.sleep(0.1)

    print(f"\nDone. {counts}")


def report() -> None:
    with connect() as conn:
        last = get_sync_state("bank")
        head = w3.eth.block_number
        print(f"Chain head : {head:,}")
        print(f"Last synced: {last:,}" if last else "Last synced: (none)")

        print("\n─── bank_events (treasury) ──────────────────────────────")
        rows = conn.execute(
            """SELECT direction, COUNT(*) AS n, COALESCE(SUM(amount_sats),0) AS total
               FROM bank_events GROUP BY direction"""
        ).fetchall()
        for r in rows:
            print(f"  {r['direction']:<12}: {r['n']:>6,} events   "
                  f"{r['total']/1e8:>14,.8f} BTC")

        # Reconciliation: reconstructed balance from events
        ins = conn.execute(
            "SELECT COALESCE(SUM(amount_sats),0) AS v FROM bank_events WHERE direction='in'"
        ).fetchone()['v']
        outs = conn.execute(
            """SELECT COALESCE(SUM(amount_sats),0) AS v FROM bank_events
               WHERE direction IN ('out','decrease')"""
        ).fetchone()['v']
        reconstructed = ins - outs

        # Live balance via eth_call
        live = bank.functions.balanceOf(TREASURY).call()
        gap = live - reconstructed

        print(f"\n─── Reconciliation ──────────────────────────────────────")
        print(f"  Reconstructed (Σin − Σout): {reconstructed:>14,} sats  ({reconstructed/1e8:.8f} BTC)")
        print(f"  Live bank.balanceOf       : {live:>14,} sats  ({live/1e8:.8f} BTC)")
        print(f"  Gap                       : {gap:>14,} sats  ({gap/1e8:.8f} BTC)")
        if abs(gap) < 1000:
            print("  ✓ Reconciliation tight (under 1000 sats drift)")
        else:
            print("  ⚠ Gap larger than 1000 sats — may indicate missed events")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    else:
        sync_bank()
        report()