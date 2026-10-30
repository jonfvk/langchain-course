#!/usr/bin/env python3
"""Isolated bounded production worker; JSON stdout contains only market objects."""
import json
from dataclasses import asdict
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.data.market_data.mu_data import MUMarketData
with MUMarketData() as data:
    catalog=None
    for line in sys.stdin:
        try:
            request=json.loads(line)
            selected=request.get('selected')
            held=request.get('held',[])
            if not isinstance(held,list) or len(held)>8:raise ValueError
            if selected is not None and (not isinstance(selected,list) or not 1<=len(selected)<=8):raise ValueError
            if catalog is None:catalog=data.discover_options()
            bysymbol={c.symbol:c for c in catalog}
            if selected is not None:
                sample=[bysymbol[s] for s in selected]
            else:
                underlying,status=data.underlying_snapshot(seconds=3)
                if underlying.mid is None:raise ValueError
                sample=[bysymbol[s] for s in held]+list(data.near_money_sample(underlying.mid,count=4))
                sample=list({c.symbol:c for c in sample}.values())[:8]
            underlying,options,status=data.option_snapshots(sample,seconds=3)
            if not status.get('authenticated'):raise ValueError
            print(json.dumps({'catalog':[asdict(c) for c in sample],'underlying':asdict(underlying),
                'options':[asdict(o) for o in options]},allow_nan=False),flush=True)
        except Exception:
            print(json.dumps({'error':'Market data unavailable; no execution permitted'}),flush=True)
