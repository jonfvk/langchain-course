// Read-only wrapper around the installed official server. Never emits credentials.
import {createHash} from 'node:crypto';
import {lifecycleEvent} from './mcp-lifecycle.mjs';
import axios from '../.tools/tastytrade-mcp/node_modules/axios/index.js';
import {resolveApiUrl} from '../.tools/tastytrade-mcp/dist/credential-target.js';
import {TastytradeMCPServer} from '../.tools/tastytrade-mcp/dist/index.js';
const report = params => process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/sandboxAudit',params})+'\n');
if (process.env.TASTYTRADE_ENV !== 'sandbox' || !['0','1'].includes(process.env.TASTYTRADE_READ_ONLY) ||
    resolveApiUrl() !== 'https://api.cert.tastyworks.com') process.exit(1);
const credentials = {};
for (const key of ['CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN']) {
  const value = process.env['TASTYTRADE_'+key];
  if (typeof value !== 'string' || !value) process.exit(1);
  // In-memory protocol metadata only: Python consumes hashes, never logs them.
  credentials[key] = {present:true,length:value.length,digest:createHash('sha256').update(value).digest('hex')};
}
report({kind:'environment',environment:process.env.TASTYTRADE_ENV,read_only:process.env.TASTYTRADE_READ_ONLY,
        destination:resolveApiUrl(),credentials});
const adapter = axios.getAdapter(axios.defaults.adapter);
axios.defaults.adapter = async config => {
  const url = new URL(config.url,config.baseURL);
  const readonly = process.env.TASTYTRADE_READ_ONLY === '1';
  const oauth = url.pathname === '/oauth/token' && config.method === 'post';
  const read = config.method === 'get' && (url.pathname === '/customers/me/accounts' ||
    /^\/accounts\/[A-Za-z0-9-]+(?:\/(?:balances|positions|trading-status|position-limit|orders(?:\/(?:live|[0-9]+))?))?$/.test(url.pathname));
  const submit = !readonly && config.method === 'post' && /^\/accounts\/[A-Za-z0-9-]+\/orders(?:\/dry-run)?$/.test(url.pathname);
  const cancel = !readonly && config.method === 'delete' && /^\/accounts\/[A-Za-z0-9-]+\/orders\/[0-9]+$/.test(url.pathname);
  if (url.origin !== 'https://api.cert.tastyworks.com' || url.username || url.password || !(oauth || read || submit || cancel))
    throw new Error('Sandbox route refused');
  if (submit) {
    const body = typeof config.data === 'string' ? JSON.parse(config.data) : config.data;
    if (body['order-type'] !== 'Limit' || body['time-in-force'] !== 'Day' || !Array.isArray(body.legs) || body.legs.length !== 1)
      throw new Error('Only long MU limit options supported');
    const leg = body.legs[0];
    if (!/^MU\s+\d{6}[CP]\d{8}$/.test(leg.symbol) || leg['instrument-type'] !== 'Equity Option' ||
        !['Buy to Open','Sell to Close'].includes(leg.action) || !Number.isInteger(leg.quantity) || leg.quantity <= 0)
      throw new Error('Unsafe option leg refused');
  }
  const phase = oauth ? 'oauth' : submit ? (url.pathname.endsWith('/dry-run')?'dry_run':'order') : cancel ? 'cancel' :
    url.pathname === '/customers/me/accounts' ? 'accounts' :
    url.pathname.endsWith('/orders/live') ? 'working_orders' :
    /\/orders\/[0-9]+$/.test(url.pathname) ? 'order_read' : url.pathname.split('/').at(-1);
  try {
    const response=await adapter(config);
    report(lifecycleEvent(phase,response));
    return response;
  } catch (error) {
    report(lifecycleEvent(phase,error.response,[...['CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN'].map(k=>process.env['TASTYTRADE_'+k]),config.headers?.Authorization,config.headers?.Authorization?.replace(/^Bearer /,'')]));
    throw error;
  }
};
try {await new TastytradeMCPServer().run();}
catch {process.exitCode=1;}
