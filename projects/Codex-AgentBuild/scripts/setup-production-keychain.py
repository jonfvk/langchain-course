#!/usr/bin/env python3
"""Run locally in a terminal; accepts no secret arguments or environment values."""
import getpass
from pathlib import Path
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.market_data.keychain import (
    ProductionMarketDataAuth, Secret, store_production_market_data,
)


def main() -> int:
    if len(sys.argv) != 1 or not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Run with no arguments in an interactive local terminal.", file=sys.stderr)
        return 1
    try:
        print("Production READ-ONLY MARKET DATA OAuth setup. All input is hidden.")
        print("This replaces the production market-data Keychain item only.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            auth = ProductionMarketDataAuth(
                Secret(getpass.getpass("Production Client ID: ")),
                Secret(getpass.getpass("Production Client Secret: ")),
                Secret(getpass.getpass("Production Grant/Refresh Token: ")),
            )
        store_production_market_data(auth)
    except (Exception, KeyboardInterrupt):
        print("Setup did not complete. No values displayed. Check Keychain access and rerun.", file=sys.stderr)
        return 1
    print("Stored in macOS Keychain. No tastytrade connection was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
