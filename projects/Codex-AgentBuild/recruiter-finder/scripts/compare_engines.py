import json,os,re
from urllib.parse import urlsplit
from dotenv import load_dotenv
import requests
from recruiter.core import ROOT,connect,reserve,canonical
load_dotenv(ROOT/'.env',override=True)
cfg=json.loads((ROOT/'config.json').read_text());db=connect()
reports=[]
for engine in ('google','google_light'):
 reserve(db,cfg['monthly_request_limit'])
 try:
  d=requests.get('https://serpapi.com/search.json',params={'api_key':os.environ['SERPAPI_KEY'],'engine':engine,'q':'site:linkedin.com/posts "hiring" DevOps','filter':'0','nfpr':'1','no_cache':'true','gl':'us','hl':'en'},timeout=(10,45)).json()
  results=d.get('organic_results',[])
  report={'engine':engine,'id':d.get('search_metadata',{}).get('id'),'error':bool(d.get('error')),'results':[{k:r.get(k) for k in ('title','link','snippet','date')} for r in results]}
  raw_url=d.get('search_metadata',{}).get('raw_html_file')
  if raw_url and urlsplit(raw_url).hostname=='serpapi.com':
   raw=requests.get(raw_url,timeout=(10,30))
   report['raw_http']=raw.status_code
   if raw.ok:
    (ROOT/f'state/{engine}-diagnostic.html').write_text(raw.text)
    report['raw_linkedin_mentions']=len(re.findall('linkedin',raw.text,re.I))
    report['raw_linkedin_post_urls']=list(dict.fromkeys(re.findall(r'https?://(?:www\.)?linkedin\.com/posts/[^\s"<>\\&]+',raw.text)))[:12]
  print(json.dumps(report,indent=2),flush=True); reports.append(report)
 except Exception as e: print(engine,type(e).__name__,flush=True)
(ROOT/'state/engine-comparison.json').write_text(json.dumps(reports,indent=2))
