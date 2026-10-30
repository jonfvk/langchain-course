#!/usr/bin/env python3
"""Explicit one-shot live MU test; safe numeric/status output only."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data.market_data.mu_stream import stream_mu_once

if __name__ == '__main__':
    if len(sys.argv) != 1:
        raise SystemExit('No arguments accepted.')
    try:
        print(json.dumps(stream_mu_once(), allow_nan=False))
    except (Exception, KeyboardInterrupt):
        print('MU test interrupted or failed; sensitive details withheld; no retry.')
        raise SystemExit(1)
