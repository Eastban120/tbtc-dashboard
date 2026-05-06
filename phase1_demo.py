"""
phase1_demo.py — fetch and decode one real RedemptionRequested event.

Walks backward from the chain tip in 10k-block chunks until it finds a
redemption, then prints every field of that one log so we can see the
raw shape of bridge data before we start structuring it.
"""

from datetime import datetime, timezone

import config
from chain import w3
from contracts import bridge


# def find_one_redemption() -> dict | None:
#     """Walk back from tip in chunks. Return the first redemption we find."""
#     latest = w3.eth.block_number
#     chunk = config.LOG_CHUNK_SIZE  # 10_000 by default

#     # Try up to 50 chunks back (~500k blocks ≈ 70 days). If the bridge had
#     # zero redemptions in that window, something is unusual — we'll bail.
#     for i in range(50):
#         to_block = latest - i * chunk
#         from_block = max(0, to_block - chunk + 1)

#         print(f"Scanning blocks {from_block:,} → {to_block:,} ...", end=" ", flush=True)

#         # get_logs() builds an eth_getLogs request scoped to:
#         #   - this contract's address
#         #   - this event's topic0 (the keccak256 hash of the event signature)
#         #   - the given block range
#         # The node returns a list of decoded EventData objects.
#         logs = bridge.events.RedemptionRequested.get_logs(
#             from_block=from_block,
#             to_block=to_block,
#         )

#         print(f"found {len(logs)} redemption(s)")

#         if logs:
#             # Return the most recent one in the chunk (last element).
#             return logs[-1]

#     return None

def find_one_redemption() -> dict | None:
    """Walk back from tip in chunks. Return the first redemption we find."""
    latest = w3.eth.block_number
    chunk = config.LOG_CHUNK_SIZE
    print(f"Chain tip reported as block {latest:,}")
    print(f"Chunk size: {chunk}")

    for i in range(50):
        to_block = latest - i * chunk
        from_block = max(0, to_block - chunk + 1)

        print(f"Scanning blocks {from_block:,} → {to_block:,} ...", end=" ", flush=True)

        try:
            logs = bridge.events.RedemptionRequested.get_logs(
                from_block=from_block,
                to_block=to_block,
            )
        except Exception as e:
            # Print the actual response body — Alchemy's error message lives there
            print("FAILED")
            print(f"  Exception type: {type(e).__name__}")
            print(f"  Message       : {e}")
            # If it's an HTTPError, the response object has the JSON body
            if hasattr(e, "response") and e.response is not None:
                print(f"  Status code   : {e.response.status_code}")
                print(f"  Response body : {e.response.text[:500]}")
            raise

        print(f"found {len(logs)} redemption(s)")
        if logs:
            return logs[-1]
    return None

def describe(log) -> None:
    """Pretty-print every field of an EventData object."""
    print("\n" + "=" * 60)
    print("Raw EventData fields")
    print("=" * 60)
    print(f"event           : {log.event}")
    print(f"blockNumber     : {log.blockNumber:,}")
    print(f"transactionHash : {log.transactionHash.hex()}")
    print(f"logIndex        : {log.logIndex}")
    print(f"address         : {log.address}")

    print("\n" + "=" * 60)
    print("Decoded args")
    print("=" * 60)
    args = log.args
    for k in args.keys():
        v = args[k]
        # bytes/HexBytes types print better as hex
        if isinstance(v, (bytes, bytearray)):
            v = "0x" + v.hex()
        print(f"  {k:<22}: {v}")

    print("\n" + "=" * 60)
    print("Derived view")
    print("=" * 60)
    block = w3.eth.get_block(log.blockNumber)
    when = datetime.fromtimestamp(block.timestamp, tz=timezone.utc)

    fee_sats = args.treasuryFee
    requested_sats = args.requestedAmount
    fee_btc = fee_sats / 1e8
    requested_btc = requested_sats / 1e8

    print(f"  Block timestamp     : {when.isoformat()}")
    print(f"  Redeemer            : {args.redeemer}")
    print(f"  Requested amount    : {requested_sats:>12,} sats  ({requested_btc:.8f} BTC)")
    print(f"  Treasury fee        : {fee_sats:>12,} sats  ({fee_btc:.8f} BTC)")
    if requested_sats > 0:
        print(f"  Fee as % of request : {100 * fee_sats / requested_sats:.4f}%")


if __name__ == "__main__":
    log = find_one_redemption()
    if log is None:
        raise SystemExit("No redemptions found in the last ~70 days. Unusual.")
    describe(log)