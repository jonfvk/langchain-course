"""Bounded real search diagnostics. Does not send notifications or expose keys."""
import json
import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from recruiter.core import ROOT, connect, reserve, now
load_dotenv(ROOT / '.env', override=True)
cfg = json.loads((ROOT / 'config.json').read_text())
db = connect()
variants = [
    {'engine':'google', 'q':'site:linkedin.com/posts hiring'},
    {'engine':'google', 'q':'site:linkedin.com/posts/ ("hiring" OR "my team" OR "referral") ("SRE" OR "DevSecOps" OR "DevOps" OR "platform engineer")', 'tbs':'qdr:d3'},
]
reports=[]
for query in variants:
    reserve(db,cfg['monthly_request_limit'])
    try:
        response=requests.get('https://serpapi.com/search.json', params={**query,'api_key':os.environ['SERPAPI_KEY'],'gl':'us','hl':'en'},timeout=(10,45))
        response.raise_for_status()
        d=response.json()
        report={'request':query,'metadata':{k:d.get('search_metadata',{}).get(k) for k in ('id','status','google_url','created_at')},
                'parameters':{k:v for k,v in d.get('search_parameters',{}).items() if k != 'api_key'},
                'information':d.get('search_information'), 'dmca':d.get('dmca_messages'), 'error_present':bool(d.get('error')),
                'results':[{k:r.get(k) for k in ('title','link','snippet','date','author')} for r in d.get('organic_results',[])]}
    except Exception as exc:
        report={'request':query,'error':type(exc).__name__}
    reports.append(report)
    print(json.dumps(report,indent=2),flush=True)
(ROOT/'state/search-comparison.json').write_text(json.dumps({'time':now().isoformat(),'reports':reports},indent=2))
