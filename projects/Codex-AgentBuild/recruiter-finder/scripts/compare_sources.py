import json, os
from dotenv import load_dotenv
import requests
from recruiter.core import ROOT,connect,reserve,canonical,ingest
load_dotenv(ROOT/'.env',override=True)
cfg=json.loads((ROOT/'config.json').read_text()); db=connect()
variants=[
 {'engine':'bing','q':'site:linkedin.com/posts "hiring" DevOps','cc':'US'},
 {'engine':'google','q':'inurl:linkedin.com/posts "hiring" DevOps','gl':'us','hl':'en'},
 {'engine':'google','q':'site:linkedin.com/posts "hiring" DevOps','gl':'us','hl':'en','location':'Charlotte, North Carolina, United States','device':'mobile'},
]
reports=[]
for params in variants:
 reserve(db,cfg['monthly_request_limit'])
 try:
  r=requests.get('https://serpapi.com/search.json',params={**params,'api_key':os.environ['SERPAPI_KEY']},timeout=(10,35));r.raise_for_status();d=r.json()
  results=d.get('organic_results',[])
  report={'request':params,'id':d.get('search_metadata',{}).get('id'),'error_present':bool(d.get('error')),'linkedin_posts':sum(canonical(x.get('link','')) is not None for x in results),'results':[{k:x.get(k) for k in ('title','link','snippet','date')} for x in results]}
  for item in results: ingest(db,item,cfg)
 except Exception as e:report={'request':params,'error':type(e).__name__}
 reports.append(report);print(json.dumps(report,indent=2),flush=True)
(ROOT/'state/source-comparison.json').write_text(json.dumps(reports,indent=2))
