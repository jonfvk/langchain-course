#!/usr/bin/env python3
"""Bounded live MU + four near-money options; prints normalized market data only."""
import json
from dataclasses import asdict
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.data.market_data.mu_data import MUMarketData, MarketDataError


def main():
    if len(sys.argv)!=1:
        raise SystemExit('No arguments accepted')
    with MUMarketData() as data:
        underlying,status=data.underlying_snapshot()
        print(json.dumps({'underlying':asdict(underlying),'connection':status},allow_nan=False),flush=True)
        if underlying.mid is None or not status['authenticated']:
            raise MarketDataError('Valid MU underlying quote not obtained')
        contracts=data.discover_options()
        sample=data.near_money_sample(underlying.mid)
        print(json.dumps({'chain_contracts':len(contracts),'expirations':sorted({c.expiration for c in contracts}),
                          'sample':[asdict(c) for c in sample]}),flush=True)
        underlying,options,status=data.option_snapshots(sample)
        print(json.dumps({'underlying':asdict(underlying),'options':[asdict(o) for o in options],
                          'connection':status},allow_nan=False),flush=True)


if __name__=='__main__':
    try:
        main()
    except MarketDataError as exc:
        print(str(exc))  # Only internally authored sanitized MarketDataError messages.
        raise SystemExit(1)
    except (Exception,KeyboardInterrupt):
        print('Market-data verification failed locally; sensitive details withheld.')
        raise SystemExit(1)
