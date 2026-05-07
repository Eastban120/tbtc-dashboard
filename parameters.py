"""
parameters.py — sync DepositParametersUpdated and RedemptionParametersUpdated.

Records every fee divisor change in the parameter_history table. After
running, you can ask "what was the deposit fee divisor at block X?" with
a SQL query against this table.

This pins down the moment deposit fees were reinstated, which we've been
speculating about.
"""

import sys
import time
from datetime import datetime, timezone

import config
from chain import w3
from contracts import bridge
from db import connect, get_sync_state, init_db, set_sync_state


# Bridge contract was deployed in late 2022. We start from block 16,000,000
# (early Jan 2023) which predates the bridge by enough margin to capture
# any parameter setting from initialization. Saves us scanning ~16M empty
# blocks below that.
BRIDGE_GENESIS_BLOCK = 16_000_000


def fetch_parameter_events(from_block: int, to_block: int):
    """Fetch both parameter-update event types in the given range."""
    deposit_logs = bridge.events.DepositParametersUpdated.get_logs(
        from_block=from_block, to_block=to_block,
    )
    redemption_logs = bridge.events.RedemptionParametersUpdated.get_logs(
        from_block=from_block, to_block=to_block,
    )
    return deposit_logs, redemption_logs


def _block_time(block_number: int) -> int:
    """Cached single-block timestamp lookup. Adds one RPC per block touched."""
    return w3.eth.get_block(block_number).timestamp


def insert_deposit_param(log) -> None:
    """Insert one DepositParametersUpdated event."""
    block_time = _block_time(log.blockNumber)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO parameter_history
                (block_number, block_time, source, divisor, dust_sats, tx_max_fee, tx_hash)
            VALUES (?, ?, 'deposit', ?, ?, ?, ?)
            """,
            (
                log.blockNumber,
                block_time,
                str(log.args.depositTreasuryFeeDivisor),
                str(log.args.depositDustThreshold),
                str(log.args.depositTxMaxFee),
                log.transactionHash.hex(),
            ),
        )


def insert_redemption_param(log) -> None:
    """Insert one RedemptionParametersUpdated event."""
    block_time = _block_time(log.blockNumber)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO parameter_history
                (block_number, block_time, source, divisor, dust_sats, tx_max_fee, tx_hash)
            VALUES (?, ?, 'redemption', ?, ?, ?, ?)
            """,
            (
                log.blockNumber,
                block_time,
                str(log.args.redemptionTreasuryFeeDivisor),
                str(log.args.redemptionDustThreshold),
                str(log.args.redemptionTxMaxFee),
                log.transactionHash.hex(),
            ),
        )


def sync_parameters() -> None:
    """
    Walk from last sync (or genesis) to current head, in chunks. Insert
    every parameter-update event we find, then advance the watermark.
    """
    init_db()

    head = w3.eth.block_number
    start = get_sync_state("parameters") or BRIDGE_GENESIS_BLOCK
    chunk = config.LOG_CHUNK_SIZE
    print(f"Syncing parameters from block {start:,} → {head:,} "
          f"({head - start:,} blocks, {chunk}-block chunks)")

    cursor = start
    inserted = 0
    while cursor <= head:
        to = min(cursor + chunk - 1, head)
        # try:
        #     deposit_logs, redemption_logs = fetch_parameter_events(cursor, to)
        # except Exception as e:
        #     print(f"  ✗ {cursor:,} → {to:,}  failed: {e}")
        #     # Simple retry-with-smaller-chunk: halve the chunk and retry once.
        #     half = max(100, chunk // 2)
        #     try:
        #         to = min(cursor + half - 1, head)
        #         deposit_logs, redemption_logs = fetch_parameter_events(cursor, to)
        #         print(f"    retry succeeded with smaller chunk ({half})")
        #     except Exception as e2:
        #         print(f"    retry also failed: {e2}")
        #         raise
        deposit_logs = redemption_logs = None
        for attempt_chunk in (chunk, max(200, chunk // 2), max(100, chunk // 5)):
            attempt_to = min(cursor + attempt_chunk - 1, head)
            try:
                deposit_logs, redemption_logs = fetch_parameter_events(cursor, attempt_to)
                to = attempt_to
                if attempt_chunk != chunk:
                    print(f"    retry succeeded with smaller chunk ({attempt_chunk})")
                break
            except Exception as e:
                print(f"  ✗ {cursor:,} → {attempt_to:,}  failed: {e}")
                time.sleep(1.0)  # back off before retry
        else:
            raise RuntimeError(f"all retries failed at block {cursor:,}")

        for log in deposit_logs:
            insert_deposit_param(log)
            inserted += 1
        for log in redemption_logs:
            insert_redemption_param(log)
            inserted += 1

        if deposit_logs or redemption_logs:
            print(f"  {cursor:,} → {to:,}  +{len(deposit_logs)} deposit, "
                  f"+{len(redemption_logs)} redemption")

        set_sync_state("parameters", to)
        cursor = to + 1
        time.sleep(0.1)  # gentle on free-tier rate limits

    print(f"\nDone. Inserted {inserted} parameter events.")


def report() -> None:
    """Print parameter history in human-readable form."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT block_number, block_time, source, divisor, dust_sats, tx_hash
            FROM parameter_history
            ORDER BY block_number, source
            """
        ).fetchall()

    if not rows:
        print("No parameter history yet. Run sync first.")
        return

    print("\n─── Parameter history ────────────────────────────────────────")
    print(f"{'Block':<12}{'Date':<22}{'Source':<14}"
          f"{'Divisor':<12}{'Implied Rate':<14}{'Dust (sats)':<14}")
    print("─" * 86)

    # for r in rows:
    #     when = datetime.fromtimestamp(r['block_time'], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    #     rate = f"{100.0 / int(r['divisor']):.4f}%" if int(r['divisor']) > 0 else "0% (waived)"
    #     print(f"{r['block_number']:<12,}{when:<22}{r['source']:<14}"
    #           f"{r['divisor']:<12,}{rate:<14}{r['dust_sats']:<14,}")
    # print()
    for r in rows:
        when = datetime.fromtimestamp(r['block_time'], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        divisor = int(r['divisor'])
        dust_sats = int(r['dust_sats'])
        rate = f"{100.0 / divisor:.4f}%" if divisor > 0 else "0% (waived)"
        print(f"{r['block_number']:<12,}{when:<22}{r['source']:<14}"
              f"{divisor:<12,}{rate:<14}{dust_sats:<14,}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    else:
        sync_parameters()
        report()