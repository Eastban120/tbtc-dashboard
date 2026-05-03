"""
chain.py — wrap the Web3 client so the rest of the app talks to one object.

Centralizes connection setup and basic chain queries. Every other module
that needs to read the chain imports `w3` from here.
"""

from web3 import Web3
from web3.providers import HTTPProvider

import config


def make_client() -> Web3:
    """
    Build a Web3 instance pointed at our RPC endpoint.

    The HTTPProvider takes a URL and gives us back something that knows
    how to send JSON-RPC requests over HTTPS. Web3() wraps it with the
    high-level API: w3.eth, w3.contract, w3.to_wei, etc.

    `request_kwargs={"timeout": 30}` raises the default 10-second timeout.
    Some eth_getLogs calls against a public RPC can be slow and 10s isn't
    always enough on the first sync.
    """
    provider = HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30})
    return Web3(provider)


# A single shared client. Importing this module once builds the connection;
# every module that does `from chain import w3` gets the same instance.
w3: Web3 = make_client()


if __name__ == "__main__":
    # Self-test: connect, ask the node a few basic questions, print the answers.
    if not w3.is_connected():
        raise SystemExit("Could not connect to RPC. Check RPC_URL in .env.")

    chain_id = w3.eth.chain_id          # 1 = Ethereum mainnet
    latest_block = w3.eth.block_number  # current chain tip
    block = w3.eth.get_block(latest_block)

    print("Connected to RPC.")
    print(f"  Chain ID         : {chain_id} ({'mainnet' if chain_id == 1 else 'other'})")
    print(f"  Latest block     : {latest_block:,}")
    print(f"  Block hash       : {block.hash.hex()}")
    print(f"  Block timestamp  : {block.timestamp}")
    print(f"  Tx count in block: {len(block.transactions)}")