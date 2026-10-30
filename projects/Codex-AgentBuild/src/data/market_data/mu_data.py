"""Bounded public MU data interface. Fixed REST reads; transport remains internal."""
from datetime import datetime, timezone
import json
import math
import random
import ssl
import time

import certifi
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from . import authentication as auth
from .keychain import Secret, load_production_market_data
from .models import MUUnderlyingSnapshot, MUOptionSnapshot, MUOptionContract, require_mu, freshness
from .mu_stream import _LOGGER
from .quote_validation import inspect_quote

_FIELDS = {
    'Quote': ['eventType','eventSymbol','bidPrice','askPrice','bidSize','askSize','bidTime','askTime','time'],
    'Trade': ['eventType','eventSymbol','price','size','dayVolume','time'],
    'Greeks': ['eventType','eventSymbol','time','volatility','delta','gamma','theta','vega','rho'],
    'Summary': ['eventType','eventSymbol','openInterest','dayId'],
    'TheoPrice': ['eventType','eventSymbol','time','price','delta','gamma'],
}


class MarketDataError(RuntimeError):
    """Fixed, sanitized error text only."""


class _Session:
    def __init__(self):
        self._access = None
        self._deadline = 0
        self.status = {}

    def __repr__(self):
        return '_Session([REDACTED])'

    def close(self):
        self._access = None
        self._deadline = 0

    def _login(self):
        if self._access is not None and time.monotonic() < self._deadline:
            return
        stored = load_production_market_data()
        status, data, error = auth._exchange('POST','/oauth/token',payload=auth._production_refresh_payload(stored))
        del stored
        self.status['oauth_http_status'] = status
        if error or not data or not auth._nonempty(data.get('access_token')) or str(data.get('token_type','')).lower() != 'bearer':
            raise MarketDataError('Production OAuth failed; no retry')
        if 'scope' in data and (not isinstance(data['scope'],str) or set(data['scope'].split()) != {'read'}):
            raise MarketDataError('Unexpected production scope')
        self._access = Secret(data['access_token'])
        self._deadline = time.monotonic() + max(0,(auth._lifetime(data) or 900)-30)

    def _read(self, path):
        self._login()
        status, data, error = auth._exchange('GET',path,bearer=self._access._for_auth())
        self.status['last_read_http_status'] = status
        if error or not data:
            detail = error if error in ('Response exceeded size limit','Invalid JSON response schema','Operation not permitted') else 'response unavailable'
            raise MarketDataError('Production market-data read failed; HTTP ' + str(status) + '; ' + detail)
        return data

    def chain(self, underlying='MU'):
        require_mu(underlying)
        contracts = {}
        for page in range(100):
            data = self._read('/option-chains/MU?per-page=250&page-offset=' + str(page))
            items = data.get('data',{}).get('items')
            if not isinstance(items,list):
                raise MarketDataError('Invalid MU chain schema')
            for item in items:
                require_mu(item.get('underlying-symbol'))
                if item.get('root-symbol') != 'MU' or item.get('option-chain-type') not in (None,'Standard'):
                    continue  # Adjusted contracts deliberately excluded in first version.
                contract = MUOptionContract('MU',item['symbol'],item['streamer-symbol'],
                    item['expiration-date'],float(item['strike-price']),item['option-type'],
                    _timestamp(item.get('stops-trading-at')))
                contracts[contract.streamer_symbol] = contract
            pages = data.get('pagination',{}).get('total-pages')
            if (isinstance(pages,int) and page+1 >= pages) or (pages is None and len(items)!=250):
                return tuple(contracts.values())
            time.sleep(0.1)
        raise MarketDataError('MU chain pagination exceeded bounded limit')

    def quote_credentials(self):
        data = self._read('/api-quote-tokens')
        self.status['quote_http_status'] = self.status['last_read_http_status']
        check = inspect_quote(data,self.status['quote_http_status'])
        if not check['accepted']:
            raise MarketDataError('Quote destination rejected: ' + check['rejection_rule'])
        expires = check['expires_at']
        if expires is None:
            raise MarketDataError('Quote expiration missing')
        remaining = (datetime.fromisoformat(expires)-datetime.now(timezone.utc)).total_seconds()
        if remaining <= 30:
            raise MarketDataError('Quote token expires too soon')
        return Secret(data['data']['dxlink-url']), Secret(data['data']['token']), time.monotonic()+remaining


def _number(value, signed=False):
    if type(value) in (int,float) and math.isfinite(value) and (signed or value>=0):
        return value
    return None


def _timestamp(value):
    if isinstance(value,str):
        try:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None
        except ValueError:
            return None
    if _number(value) and value>0:
        try:
            return datetime.fromtimestamp(value/1000,timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError):
            pass
    return None


def _subscription(contracts, catalog):
    # Caller-provided options cannot bypass underlying/identity/discovery checks.
    add = [{'type':kind,'symbol':'MU'} for kind in ('Quote','Trade')]
    if len(contracts)>8:
        raise MarketDataError('At most eight MU option contracts per sample')
    for contract in contracts:
        require_mu(contract.underlying)
        if catalog.get(contract.streamer_symbol) != contract:
            raise MarketDataError('Option was not discovered from the MU instrument endpoint')
        contract.__post_init__()
        add.extend({'type':kind,'symbol':contract.streamer_symbol} for kind in _FIELDS)
    return {'type':'FEED_SUBSCRIPTION','channel':3,'reset':True,'add':add}


def _decode(data, fields, allowed, events):
    if not isinstance(data,list) or len(data)%2:
        raise MarketDataError('Invalid compact feed')
    for i in range(0,len(data),2):
        kind, values = data[i:i+2]
        columns = fields.get(kind)
        if not columns or not isinstance(values,list) or len(values)%len(columns):
            raise MarketDataError('Feed schema mismatch')
        for j in range(0,len(values),len(columns)):
            row = dict(zip(columns,values[j:j+len(columns)]))
            symbol = row.get('eventSymbol')
            if symbol not in allowed or kind not in allowed[symbol]:
                raise MarketDataError('Unsubscribed instrument/event rejected')
            if row.get('eventType',kind) != kind:
                raise MarketDataError('Feed event type mismatch')
            # Only latest standard non-historical subscription values, no time series.
            events.setdefault(symbol,{})[kind] = (row,datetime.now(timezone.utc).isoformat())


def _normalize(symbol, events, contract=None):
    snap = MUOptionSnapshot(contract=contract) if contract else MUUnderlyingSnapshot()
    for kind,(row,received) in events.items():
        snap.available_events.append(kind)
        snap.received_timestamps[kind] = received
        if kind=='Quote':
            for source,dest in [('bidPrice','bid'),('askPrice','ask'),('bidSize','bid_size'),('askSize','ask_size')]:
                setattr(snap,dest,_number(row.get(source)))
            times=[t for t in (_timestamp(row.get('bidTime')),_timestamp(row.get('askTime')),_timestamp(row.get('time'))) if t]
            snap.event_timestamps[kind] = min(times) if times else None
        elif kind=='Trade':
            for source,dest in [('price','last'),('size','last_size'),('dayVolume','volume')]:
                setattr(snap,dest,_number(row.get(source)))
            snap.event_timestamps[kind] = _timestamp(row.get('time'))
        elif kind=='Greeks' and contract:
            for source,dest in [('volatility','iv'),('delta','delta'),('gamma','gamma'),('theta','theta'),('vega','vega'),('rho','rho')]:
                setattr(snap,dest,_number(row.get(source),signed=source!='volatility'))
            snap.event_timestamps[kind] = _timestamp(row.get('time'))
        elif kind=='Summary' and contract:
            snap.open_interest = _number(row.get('openInterest'))
            day = row.get('dayId')
            snap.summary_day_id = day if type(day) is int else None
            snap.event_timestamps[kind] = None
        elif kind=='TheoPrice' and contract:
            snap.theoretical_price = _number(row.get('price'))
            snap.theoretical_delta = _number(row.get('delta'),signed=True)
            snap.theoretical_gamma = _number(row.get('gamma'),signed=True)
            snap.event_timestamps[kind] = _timestamp(row.get('time'))
    for kind in ('Quote','Trade') + (('Greeks','Summary','TheoPrice') if contract else ()):
        snap.freshness[kind] = freshness(snap.event_timestamps.get(kind),snap.received_timestamps.get(kind))
    if snap.bid is not None and snap.ask is not None and 0<=snap.bid<=snap.ask and snap.ask>0:
        snap.mid=round((snap.bid+snap.ask)/2,8)
        snap.mark=snap.mid
        snap.mark_source='bid_ask_midpoint'
    required=['bid','ask','bid_size','ask_size','last','last_size','volume']
    if contract:
        required+=['iv','delta','gamma','theta','vega','rho','open_interest','theoretical_price']
    snap.missing_fields=[name for name in required if getattr(snap,name) is None]
    return snap


def _stream(session, contracts=(), catalog=None, seconds=14):
    request = _subscription(contracts,catalog or {})
    allowed={'MU':{'Quote','Trade'}}
    allowed.update({c.streamer_symbol:set(_FIELDS) for c in contracts})
    requested={k:v for k,v in _FIELDS.items() if contracts or k in ('Quote','Trade')}
    # One reconnect maximum. Each new connection gets fresh quote credentials.
    for attempt in range(2):
        url,token,expiry = session.quote_credentials()
        ws=None
        events={}
        status=dict(connected=False,authenticated=False,subscribed=False,clean_disconnect=False,attempts=attempt+1)
        retry=False
        try:
            ws=connect(url._for_auth(),ssl=ssl.create_default_context(cafile=certifi.where()),proxy=None,
                       open_timeout=10,close_timeout=1,ping_interval=None,compression=None,
                       max_size=1024*1024,logger=_LOGGER,user_agent_header='codex-agentbuild/0.1')
            status['connected']=True
            deadline=min(time.monotonic()+seconds,expiry-10)
            def send(message):
                ws.send(json.dumps(message))
            send({'type':'SETUP','channel':0,'version':'0.1-DXF-JS/0.3.0','keepaliveTimeout':60,'acceptKeepaliveTimeout':60})
            setup=auth_sent=channel_requested=feed_requested=False
            fields={}
            interval=5.0
            last_send=time.monotonic()
            last_receive=last_send
            while time.monotonic()<deadline:
                now=time.monotonic()
                if now-last_send>=interval:
                    send({'type':'KEEPALIVE','channel':0});last_send=now
                if now-last_receive>10 and not status['authenticated']:
                    raise MarketDataError('DXLink authentication timed out')
                try:
                    msg=json.loads(ws.recv(timeout=min(1,max(0.001,deadline-now))))
                except TimeoutError:
                    continue
                last_receive=time.monotonic()
                kind,ch=msg.get('type'),msg.get('channel')
                if kind=='ERROR':
                    code=msg.get('error')
                    safe=code if code in ('UNAUTHORIZED','TIMEOUT','INVALID_MESSAGE','BAD_ACTION','UNSUPPORTED_PROTOCOL') else 'UNKNOWN'
                    raise MarketDataError('DXLink error: '+safe)
                if kind=='SETUP' and ch==0:
                    setup=True
                    server_timeout=_number(msg.get('keepaliveTimeout'))
                    if server_timeout and server_timeout>0:
                        interval=max(0.1,min(5,server_timeout/2))
                elif kind=='AUTH_STATE' and ch==0:
                    if msg.get('state')=='UNAUTHORIZED':
                        if auth_sent or not setup:
                            raise MarketDataError('DXLink authorization failed')
                        send({'type':'AUTH','channel':0,'token':token._for_auth()});auth_sent=True
                    elif msg.get('state')=='AUTHORIZED' and auth_sent and not channel_requested:
                        status['authenticated']=True
                        send({'type':'CHANNEL_REQUEST','channel':3,'service':'FEED','parameters':{'contract':'AUTO'}})
                        channel_requested=True
                elif kind=='CHANNEL_OPENED' and ch==3 and status['authenticated'] and not feed_requested:
                    send({'type':'FEED_SETUP','channel':3,'acceptAggregationPeriod':0.1,'acceptDataFormat':'COMPACT','acceptEventFields':requested})
                    send(request);feed_requested=True;status['subscribed']=True
                elif kind=='FEED_CONFIG' and ch==3 and feed_requested:
                    if msg.get('dataFormat')!='COMPACT':
                        continue
                    for event,columns in msg.get('eventFields',{}).items():
                        if event in requested:
                            if not isinstance(columns,list) or not all(isinstance(c,str) for c in columns) or 'eventSymbol' not in columns or len(set(columns))!=len(columns):
                                raise MarketDataError('Invalid negotiated DXLink fields')
                            fields[event]=columns
                elif kind=='FEED_DATA' and ch==3 and feed_requested:
                    _decode(msg.get('data'),fields,allowed,events)
                elif kind=='CHANNEL_CLOSED' and ch==3:
                    raise ConnectionError('Feed channel closed')
            if channel_requested:
                send({'type':'CHANNEL_CANCEL','channel':3})
            status['event_counts']={symbol:sorted(rows) for symbol,rows in events.items()}
            status['negotiated_fields']={kind:cols for kind,cols in fields.items() if all(c in _FIELDS[kind] for c in cols)}
        except (ConnectionClosed,ConnectionError,TimeoutError,OSError):
            retry=True
        finally:
            if ws is not None:
                try:
                    ws.close(code=1000)
                    status['clean_disconnect']=ws.close_code in (1000,1001)
                except Exception:
                    status['clean_disconnect']=False
            ws=url=token=None
        if retry:
            if attempt==0:
                time.sleep(0.5+random.random()*0.5)
                continue
            raise MarketDataError('DXLink connection failed after bounded reconnect')
        status.update(session.status)
        return _normalize('MU',events.get('MU',{})),tuple(_normalize(c.streamer_symbol,events.get(c.streamer_symbol,{}),c) for c in contracts),status
    raise MarketDataError('DXLink unavailable')


class MUMarketData:
    """Public API exposes MU discovery and bounded normalized snapshots only."""
    def __init__(self):
        self._session=_Session()
        self._catalog={}

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()

    def discover_options(self,underlying='MU'):
        require_mu(underlying)
        contracts=self._session.chain(underlying)
        self._catalog={c.streamer_symbol:c for c in contracts}
        return contracts

    def underlying_snapshot(self,underlying='MU',seconds=14):
        require_mu(underlying)
        self._duration(seconds)
        snapshot,_,status=_stream(self._session,seconds=seconds)
        return snapshot,status

    def option_snapshots(self,contracts,seconds=14):
        self._duration(seconds)
        return _stream(self._session,tuple(contracts),self._catalog,seconds)

    @staticmethod
    def _duration(seconds):
        if type(seconds) not in (int,float) or not 1<=seconds<=30:
            raise ValueError('Bounded stream duration must be 1..30 seconds')

    def near_money_sample(self,spot,count=4):
        if type(count) is not int or count not in (2,4,6,8) or _number(spot) is None or spot<=0:
            raise ValueError('Invalid sample size or reference price')
        now=datetime.now(timezone.utc)
        candidates=[c for c in self._catalog.values()
                    if (datetime.fromisoformat(c.stops_trading_at)>now if c.stops_trading_at
                        else c.expiration>now.date().isoformat())]
        if not candidates:
            raise MarketDataError('No unexpired MU option contracts')
        expiry=min(c.expiration for c in candidates)
        selected=[]
        for side in ('C','P'):
            selected.extend(sorted((c for c in candidates if c.expiration==expiry and c.option_type==side),key=lambda c:abs(c.strike-spot))[:count//2])
        return tuple(selected)
