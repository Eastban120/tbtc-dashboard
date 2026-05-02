"""
config.py — single source of truth for environment-derived settings.

Loads .env once at import time and exposes typed, validated constants
for the rest of the app to use. Fails fast if anything required is
missing, so we don't get cryptic errors deep in a sync run.
"""

import os
import sys
from dotenv import load_dotenv
from web3 import Web3

# Walk up from cwd looking for a .env file and load it into os.environ.
# Returns True if a file was found, False otherwise — useful for diagnostics.
_loaded = load_dotenv()


def _required(key: str) -> str:
    """Read an env var or exit with a clear error if it's missing."""
    value = os.getenv(key)
    if not value:
        sys.exit(
            f"ERROR: {key} is not set. "
            f"Check your .env file in the project root. "
            f"(load_dotenv found a file: {_loaded})"
        )
    return value


def _checksum(address: str) -> str:
    """
    Convert an address to EIP-55 checksum format.
    web3.py rejects non-checksummed addresses on contract calls,
    so we normalize here once.
    """
    return Web3.to_checksum_address(address)


# --- Required ---
RPC_URL: str = _required("RPC_URL")

BRIDGE_ADDRESS: str = _checksum(_required("BRIDGE_ADDRESS"))
BANK_ADDRESS: str = _checksum(_required("BANK_ADDRESS"))
TREASURY_ADDRESS: str = _checksum(_required("TREASURY_ADDRESS"))

# --- Optional, with sensible defaults ---
LOG_CHUNK_SIZE: int = int(os.getenv("LOG_CHUNK_SIZE", "10000"))

# Local file for the SQLite cache (Phase 2). Kept here so every module
# that touches it agrees on the path.
DB_PATH: str = os.getenv("DB_PATH", "treasury.db")


if __name__ == "__main__":
    # Running `python config.py` directly prints the resolved config
    # without leaking the full RPC URL to the terminal.
    print("Configuration loaded successfully:")
    print(f"  RPC_URL          : {RPC_URL[:40]}... (truncated)")
    print(f"  BRIDGE_ADDRESS   : {BRIDGE_ADDRESS}")
    print(f"  BANK_ADDRESS     : {BANK_ADDRESS}")
    print(f"  TREASURY_ADDRESS : {TREASURY_ADDRESS}")
    print(f"  LOG_CHUNK_SIZE   : {LOG_CHUNK_SIZE}")
    print(f"  DB_PATH          : {DB_PATH}")