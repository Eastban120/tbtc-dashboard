"""
contracts.py — load contract instances and the event filters we'll use.

Each contract is a Web3 `Contract` object: ABI + address + a connection.
With it, web3.py can:
  - call view functions:        bridge.functions.treasury().call()
  - filter and decode events:   bridge.events.RedemptionRequested.create_filter(...)
  - decode raw logs:            bridge.events.RedemptionRequested.process_log(log)

We only build the objects here. Fetching is done in later modules.
"""

import json
from pathlib import Path

from web3.contract import Contract

import config
from chain import w3


# Folder holding our ABI JSON files. Path(__file__).parent is the directory
# this file lives in, so this works regardless of where you run python from.
ABI_DIR = Path(__file__).parent / "abis"


def _load_abi(name: str) -> list:
    """Read an ABI JSON file from abis/ and parse it into a Python list."""
    path = ABI_DIR / name
    with path.open() as f:
        return json.load(f)


# Build the Bridge contract object.
# Note: the address must already be checksum-cased (config.py handles that).
bridge: Contract = w3.eth.contract(
    address=config.BRIDGE_ADDRESS,
    abi=_load_abi("bridge.json"),
)

# Build the Bank contract object.
bank: Contract = w3.eth.contract(
    address=config.BANK_ADDRESS,
    abi=_load_abi("bank.json"),
)

""" if __name__ == "__main__":
    # Self-test: prove the ABI loaded and the address matches what's on-chain.
    # `bridge.functions.treasury().call()` invokes the Bridge's `treasury()`
    # view function via eth_call. View functions are free — no transaction,
    # no gas, just a read.
    on_chain_treasury = bridge.functions.treasury().call()

    print(f"Bridge address       : {bridge.address}")
    print(f"Bank address         : {bank.address}")
    print(f"On-chain treasury    : {on_chain_treasury}")
    print(f"Configured treasury  : {config.TREASURY_ADDRESS}")
    match = on_chain_treasury.lower() == config.TREASURY_ADDRESS.lower()
    print(f"Match                : {match}")

    # List the event names we have available — useful sanity check.
    event_names = [e.event_name for e in bridge.events]
    interesting = [n for n in event_names if "Deposit" in n or "Redemption" in n]
    print(f"Bridge events ({len(event_names)} total):")
    for n in sorted(interesting):
        print(f"  - {n}") """

if __name__ == "__main__":
    # Bridge sanity
    on_chain_treasury = bridge.functions.treasury().call()
    print(f"Bridge address       : {bridge.address}")
    print(f"On-chain treasury    : {on_chain_treasury}")
    print(f"Configured treasury  : {config.TREASURY_ADDRESS}")
    bridge_match = on_chain_treasury.lower() == config.TREASURY_ADDRESS.lower()
    print(f"Bridge treasury OK   : {bridge_match}")

    # Bank sanity — read the treasury's current Bank balance directly
    treasury_bank_balance = bank.functions.balanceOf(config.TREASURY_ADDRESS).call()
    btc_balance = treasury_bank_balance / 1e8
    print(f"\nBank address         : {bank.address}")
    print(f"Treasury Bank balance: {treasury_bank_balance:,} sats ({btc_balance:.8f} BTC)")

    # The Bank should know about the Bridge — confirm
    bank_bridge = bank.functions.bridge().call()
    print(f"Bank's bridge ref    : {bank_bridge}")
    bank_bridge_match = bank_bridge.lower() == config.BRIDGE_ADDRESS.lower()
    print(f"Bank↔Bridge link OK  : {bank_bridge_match}")