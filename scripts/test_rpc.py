"""
Validate that an RPC endpoint can handle the workloads this project needs:
1. Basic block_number / get_block calls (for report functions)
2. Filtered eth_getLogs with argument_filters (the drpc-broken case)
3. Unfiltered eth_getLogs over a busy 1000-block window
4. Contract .call() (for balanceOf / totalSupply reconciliation)
5. The "BalanceTransferred should never be zero" sanity check
"""
import os
import sys
import time
from web3 import Web3

# Read URL from env or first argv. Don't print the URL anywhere.
RPC_URL = os.environ.get("TEST_RPC_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
if not RPC_URL:
    print("Usage: TEST_RPC_URL=https://... python _test_alchemy.py")
    print("   or: python _test_alchemy.py https://...")
    sys.exit(1)

# Mask the URL for safe printing
def mask(url):
    if "/v2/" in url:
        prefix, key = url.rsplit("/v2/", 1)
        return f"{prefix}/v2/{key[:6]}...{key[-4:]}" if len(key) > 12 else f"{prefix}/v2/***"
    return url[:30] + "..."

print(f"Testing endpoint: {mask(RPC_URL)}\n")

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))

# Project addresses — same as in config.py
BANK = w3.to_checksum_address("0x65Fbae61ad2C8836fFbFB502A0dA41b0789D9Fc6")
TREASURY = w3.to_checksum_address("0x87F005317692D05BAA4193AB0c961c69e175f45f")

# Minimal Bank ABI — just what we need to test
BANK_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "BalanceTransferred", "type": "event", "anonymous": False,
     "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "amount", "type": "uint256", "indexed": False},
     ]},
]
bank = w3.eth.contract(address=BANK, abi=BANK_ABI)


def test(name, fn):
    print(f"  {name} ... ", end="", flush=True)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        print(f"OK ({elapsed:.2f}s) — {result}")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {str(e)[:100]}")
        return False


print("=== Basic chain calls ===")
test("eth_blockNumber",
     lambda: f"head={w3.eth.block_number:,}")
test("eth_getBlockByNumber (recent)",
     lambda: f"timestamp={w3.eth.get_block(25_000_000).timestamp}")
test("eth_call balanceOf(treasury)",
     lambda: f"{bank.functions.balanceOf(TREASURY).call() / 1e8:.6f} BTC")

print("\n=== eth_getLogs: unfiltered, 1000 blocks ===")
def t_unfiltered():
    logs = bank.events.BalanceTransferred.get_logs(
        from_block=24_500_000, to_block=24_500_999
    )
    return f"{len(logs)} BalanceTransferred events"
test("unfiltered 1000 blocks", t_unfiltered)

print("\n=== eth_getLogs: WITH argument_filters (drpc-broken case) ===")
def t_filtered():
    logs = bank.events.BalanceTransferred.get_logs(
        from_block=24_500_000, to_block=24_500_999,
        argument_filters={"to": TREASURY}
    )
    return f"{len(logs)} treasury-bound transfers"
test("filtered 1000 blocks", t_filtered)

print("\n=== eth_getLogs: large unfiltered (5000 blocks, busy range) ===")
def t_large():
    logs = bank.events.BalanceTransferred.get_logs(
        from_block=24_900_000, to_block=24_904_999
    )
    return f"{len(logs)} events in 5000 busy blocks"
test("unfiltered 5000 blocks", t_large)

print("\n=== Sanity: are we getting non-zero results? ===")
def t_sanity():
    # The known gap window — we already proved on-chain there are 200+ events here
    logs = bank.events.BalanceTransferred.get_logs(
        from_block=24_500_000, to_block=24_500_999
    )
    if len(logs) == 0:
        return "ZERO events — endpoint may be returning empty responses"
    return f"{len(logs)} events (good — empty-response bug not present)"
test("known-busy window check", t_sanity)

print("\n=== Burst: 10 sequential 1000-block log calls ===")
def t_burst():
    total = 0
    failures = 0
    for i in range(10):
        try:
            logs = bank.events.BalanceTransferred.get_logs(
                from_block=24_900_000 + i*1000, to_block=24_900_999 + i*1000
            )
            total += len(logs)
        except Exception:
            failures += 1
    return f"{total} events across 10 calls, {failures} failures"
test("10 sequential calls", t_burst)

print("\nAll tests complete.")
