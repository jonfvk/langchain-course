#!/usr/bin/env python3
"""Hidden local sandbox refresh-token replacement; no network operations."""
import getpass
import hmac
import json
from pathlib import Path
import sys
import warnings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.security.sandbox_keychain import SERVICE, ACCOUNT, _decode
from src.data.market_data.keychain import _backend


def main():
    original = bundle = token = confirmation = replacement = verified = None
    try:
        if len(sys.argv) != 1 or not sys.stdin.isatty() or not sys.stderr.isatty():
            print('Run with no arguments in an interactive local Terminal.', file=sys.stderr)
            return 1
        backend = _backend()
        original = backend.get_password(SERVICE, ACCOUNT)
        _decode(original)  # Require an existing valid sandbox bundle.
        print('Update SANDBOX grant only. Existing Client ID and Client Secret are preserved.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('New SANDBOX Grant/Refresh Token (hidden): ')
            confirmation = getpass.getpass('Repeat new SANDBOX Grant/Refresh Token (hidden): ')
        if not token or any(c.isspace() for c in token):
            raise ValueError('Invalid token input')
        if not hmac.compare_digest(token.encode(), confirmation.encode()):
            raise ValueError('Input mismatch')
        bundle = json.loads(original)
        bundle['credentials']['refresh_token'] = token
        replacement = json.dumps(bundle)
        if backend.get_password(SERVICE, ACCOUNT) != original:
            raise ValueError('Concurrent Keychain update')
        backend.set_password(SERVICE, ACCOUNT, replacement)
        verified = backend.get_password(SERVICE, ACCOUNT)
        _decode(verified)
        if json.loads(verified) != bundle:
            raise ValueError('Verification failed')
        print('Sandbox refresh token updated and verified. No authentication performed.')
        return 0
    except (Exception, KeyboardInterrupt):
        print('Update not confirmed. Input may differ, contain whitespace, or Keychain access may have failed. No values displayed.', file=sys.stderr)
        return 1
    finally:
        original = bundle = token = confirmation = replacement = verified = None


if __name__ == '__main__':
    raise SystemExit(main())
