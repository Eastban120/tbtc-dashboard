"""
reconcile_token.py — bisect and backfill missed mints/burns.

Run after sync_token.py if the report shows unexplained drift. Bisects the
synced block range to localize windows where event-derived supply diverges
from totalSupply(), then re-fetches Transfer logs for those windows.

Safe to run repeatedly: uses INSERT OR IGNORE keyed on (tx_hash, log_index).

Usage: python reconcile_token.py
"""

import sys
import time

from chain import w3
from contracts import tbtc_token
from db import connect, get_sync_state
from sync_token import (
    insert_mint, insert_burn,
    ZERO_ADDRESS, TOKEN_GENESIS_BLOCK,
    RECONCILE_TOLERANCE_WEI, pre_genesis_baseline_wei,
)


# Stop bisecting once a window is this small — refetch the window wholesale.
MIN_BISECT_WINDOW = 100


def event_supply_at(block_number: int) -> int:
    """Indexed mints minus burns at or before this block, in wei."""
    with connect() as conn:
        mint_rows = conn.execute(
            "SELECT amount_wei FROM token_transfers "
            "WHERE direction='mint' AND block_number <= ?",
            (block_number,)
        ).fetchall()
        burn_rows = conn.execute(
            "SELECT amount_wei FROM token_transfers "
            "WHERE direction='burn' AND block_number <= ?",
            (block_number,)
        ).fetchall()
    return sum(int(r['amount_wei']) for r in mint_rows) \
         - sum(int(r['amount_wei']) for r in burn_rows)


def drift_at(block_number: int) -> int:
    """Baseline-adjusted drift in wei. Positive = we're missing mints."""
    event_supply = event_supply_at(block_number)
    actual = _call_with_backoff(
        lambda: tbtc_token.functions.totalSupply().call(block_identifier=block_number)
    )
    return (actual - event_supply) - pre_genesis_baseline_wei()


def _call_with_backoff(fn, max_retries: int = 5):
    """Run `fn()` with exponential backoff on rate-limit (HTTP 429) errors."""
    import requests
    for attempt in range(max_retries):
        try:
            time.sleep(0.15)  # baseline throttle: ~6 req/s
            return fn()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = 2 ** attempt
                print(f"    rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            # Other transient errors — retry once with short backoff
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue
            raise
    raise RuntimeError("max retries exceeded")


def bisect_for_drift(lo: int, hi: int, found: list) -> None:
    """
    Recursively find narrow block windows where drift changes — i.e. where
    a missed event lives. Populates `found` with (lo, hi) tuples.
    """
    drift_lo = drift_at(lo)
    drift_hi = drift_at(hi)
    if abs(drift_hi - drift_lo) < RECONCILE_TOLERANCE_WEI:
        return  # no change of drift across this range
    if hi - lo <= MIN_BISECT_WINDOW:
        found.append((lo, hi))
        return
    mid = (lo + hi) // 2
    bisect_for_drift(lo, mid, found)
    bisect_for_drift(mid, hi, found)


def refetch_window(lo: int, hi: int) -> tuple[int, int]:
    """Re-pull Transfer events for a window and upsert. Returns (mints_added, burns_added)."""
    mints = _call_with_backoff(lambda: tbtc_token.events.Transfer.get_logs(
        from_block=lo, to_block=hi,
        argument_filters={"from": ZERO_ADDRESS},
    ))
    burns = _call_with_backoff(lambda: tbtc_token.events.Transfer.get_logs(
        from_block=lo, to_block=hi,
        argument_filters={"to": ZERO_ADDRESS},
    ))
    added_m = added_b = 0
    with connect() as conn:
        for log in mints:
            exists = conn.execute(
                "SELECT 1 FROM token_transfers WHERE tx_hash=? AND log_index=?",
                (log.transactionHash.hex(), log.logIndex)
            ).fetchone()
            if not exists:
                insert_mint(log, conn)
                added_m += 1
        for log in burns:
            exists = conn.execute(
                "SELECT 1 FROM token_transfers WHERE tx_hash=? AND log_index=?",
                (log.transactionHash.hex(), log.logIndex)
            ).fetchone()
            if not exists:
                insert_burn(log, conn)
                added_b += 1
    return added_m, added_b


def main() -> None:
    last = get_sync_state("token")
    if last is None:
        sys.exit("ERROR: No token sync state. Run `python sync_token.py` first.")

    print(f"Reconciling token_transfers up to block {last:,}")
    print(f"Pre-genesis baseline: {pre_genesis_baseline_wei() / 1e18:.6f} tBTC\n")

    initial_drift = drift_at(last)
    print(f"  Initial drift: {initial_drift / 1e18:+.6f} tBTC")
    if abs(initial_drift) < RECONCILE_TOLERANCE_WEI:
        print("  ✓ Within tolerance. Nothing to do.")
        return

    print(f"  Bisecting {TOKEN_GENESIS_BLOCK:,}–{last:,} for drift windows...")
    windows: list[tuple[int, int]] = []
    bisect_for_drift(TOKEN_GENESIS_BLOCK, last, windows)
    print(f"  Found {len(windows)} suspect window(s).\n")

    if not windows:
        print("  ⚠ Drift detected but no windows found by bisection. "
              "This shouldn't happen — check pre_genesis_baseline_wei().")
        return

    total_added_m = total_added_b = 0
    for i, (lo, hi) in enumerate(windows, 1):
        added_m, added_b = refetch_window(lo, hi)
        total_added_m += added_m
        total_added_b += added_b
        print(f"  [{i}/{len(windows)}] {lo:,}–{hi:,}: "
              f"+{added_m} mints, +{added_b} burns")
        time.sleep(0.3)

    print(f"\n  Backfilled total: {total_added_m} mints, {total_added_b} burns")
    final_drift = drift_at(last)
    print(f"  Final drift: {final_drift / 1e18:+.6f} tBTC")
    if abs(final_drift) < RECONCILE_TOLERANCE_WEI:
        print("  ✓ Reconciliation complete.")
    else:
        print(f"  ⚠ Drift remains. Try lowering MIN_BISECT_WINDOW in reconcile_token.py "
              f"(currently {MIN_BISECT_WINDOW}).")


if __name__ == "__main__":
    main()