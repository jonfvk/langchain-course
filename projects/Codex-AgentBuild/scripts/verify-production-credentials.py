#!/usr/bin/env python3
"""Temporary local verification utility. Never writes credentials or uses network."""
import getpass
import hmac
from pathlib import Path
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.market_data.keychain import load_production_market_data


def compare(stored, entered):
    """Return only lengths and booleans; lengths measure UTF-8 bytes."""
    stored_bytes = entered_bytes = None
    try:
        stored_bytes = stored.encode('utf-8')
        entered_bytes = entered.encode('utf-8')
        return {
            'matches': hmac.compare_digest(stored_bytes, entered_bytes),
            'stored_length': len(stored_bytes), 'entered_length': len(entered_bytes),
            'stored_edge_whitespace': stored != stored.strip(),
            'entered_edge_whitespace': entered != entered.strip(),
            'edge_whitespace_difference': (
                stored[:len(stored) - len(stored.lstrip())], stored[len(stored.rstrip()):]
            ) != (
                entered[:len(entered) - len(entered.lstrip())], entered[len(entered.rstrip()):]
            ),
            'stored_cr': '\r' in stored, 'entered_cr': '\r' in entered,
            'cr_difference': tuple(i for i, c in enumerate(stored) if c == '\r') != tuple(i for i, c in enumerate(entered) if c == '\r'),
            'stored_newline': '\n' in stored, 'entered_newline': '\n' in entered,
            'newline_difference': tuple(i for i, c in enumerate(stored) if c == '\n') != tuple(i for i, c in enumerate(entered) if c == '\n'),
        }
    finally:
        stored_bytes = entered_bytes = stored = entered = None


def main():
    auth = entered_secret = entered_token = None
    try:
        if len(sys.argv) != 1 or not sys.stdin.isatty() or not sys.stderr.isatty():
            return 1
        auth = load_production_market_data()
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            entered_secret = getpass.getpass('Production Client Secret (hidden): ')
            secret_result = compare(auth.client_secret._for_auth(), entered_secret)
            entered_secret = None
            entered_token = getpass.getpass('CURRENT production read-only Grant Refresh Token (hidden): ')
            token_result = compare(auth.refresh_token._for_auth(), entered_token)
            entered_token = None
        auth = None
        for label, result in (('Client Secret', secret_result), ('Refresh Token', token_result)):
            print(f"{label} matches Keychain: {'YES' if result['matches'] else 'NO'}")
            print(f"{label} UTF-8 byte lengths: stored={result['stored_length']}, entered={result['entered_length']}")
            for suffix in ('edge_whitespace', 'cr', 'newline'):
                description = {'edge_whitespace': 'leading/trailing whitespace', 'cr': 'CR', 'newline': 'newline'}[suffix]
                difference = 'edge_whitespace_difference' if suffix == 'edge_whitespace' else suffix + '_difference'
                print(f"{label} {description}: stored={'YES' if result['stored_' + suffix] else 'NO'}, entered={'YES' if result['entered_' + suffix] else 'NO'}, difference={'YES' if result[difference] else 'NO'}")
        return 0
    except (Exception, KeyboardInterrupt):
        # No exception text or traceback: even backend/input failures may hold secrets.
        return 1
    finally:
        auth = entered_secret = entered_token = None


if __name__ == '__main__':
    raise SystemExit(main())
