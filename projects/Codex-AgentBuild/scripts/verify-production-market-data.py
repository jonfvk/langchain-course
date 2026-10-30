#!/usr/bin/env python3
"""Explicit one-shot live verification. Output contains safe metadata only."""
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.market_data.authentication import verify_mu_market_data_access


def main():
    if len(sys.argv) != 1:
        print('No arguments accepted.')
        return 1
    try:
        result = verify_mu_market_data_access()
        print(json.dumps(asdict(result), allow_nan=False))
        return 0 if result.quote_tokens_succeeded else 1
    except (Exception, KeyboardInterrupt):
        print('Verification interrupted or failed locally; no retry performed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
