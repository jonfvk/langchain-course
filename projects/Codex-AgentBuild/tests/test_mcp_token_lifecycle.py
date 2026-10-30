import subprocess
import unittest
from src.broker.sandbox_inspection import NODE,ROOT

class InstalledMCPLifecycleTests(unittest.TestCase):
    def test_real_installed_cache_refresh_and_rest_invalidation_offline(self):
        code=r'''
import assert from 'node:assert/strict';
import axios from './.tools/tastytrade-mcp/node_modules/axios/index.js';
import {TastytradeOAuthClient} from './.tools/tastytrade-mcp/dist/oauth-client.js';
import {TastytradeClient} from './.tools/tastytrade-mcp/dist/api-client.js';
import {lifecycleEvent} from './scripts/mcp-lifecycle.mjs';
let grants=0;
axios.post=async()=>({data:{access_token:'FAKE-'+(++grants),expires_in:900,token_type:'Bearer'}});
const config={apiUrl:'https://api.cert.tastyworks.com',clientId:'fake-id',clientSecret:'fake-secret',refreshToken:'fake-refresh'};
const auth=new TastytradeOAuthClient(config);
await auth.getAccessToken();await auth.getAccessToken();assert.equal(grants,1);
const realNow=Date.now;
Date.now=()=>realNow()+841000;
await auth.getAccessToken();assert.equal(grants,2); // Before 900s expiry, after 60s safety margin.
Date.now=realNow;
let rest=0;
const client=new TastytradeClient(config,{adapter:async c=>{
  rest++;
  if(rest===1){const e=new Error('fake unauthorized');e.isAxiosError=true;e.config=c;e.response={status:401,data:{error:{code:'unauthorized'}},headers:{},config:c};throw e;}
  return {status:200,statusText:'OK',data:{data:{items:[]}},headers:{'content-type':'application/json'},config:c};
}});
await assert.rejects(()=>client.getAccounts());
const previous=grants;
await client.getAccounts();assert.equal(grants,previous+1);assert.equal(rest,2);
const audit=lifecycleEvent('oauth',{status:200,data:JSON.stringify({access_token:'FAKE-NEVER-OUTPUT',expires_in:900})});
assert.equal(audit.access_token_obtained,true);assert.equal(audit.refresh_margin_seconds,60);
assert.ok(!JSON.stringify(audit).includes('FAKE'));
assert.equal(lifecycleEvent('balances',{status:401}).phase,'balances');
const failure=lifecycleEvent('dry_run',{status:422,data:{error:{code:'invalid_symbol',message:'fake-secret Invalid symbol'}}},['fake-secret']);
assert.equal(failure.error_message,'<REDACTED> Invalid symbol');
assert.ok(!JSON.stringify(failure).includes('fake-secret'));
'''
        result=subprocess.run([str(NODE),'--input-type=module','-e',code],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
