from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch
from src.data.market_data import mu_data as m
from src.data.market_data import authentication as a
from src.data.market_data.models import MUOptionContract, freshness
from src.data.market_data.keychain import Secret, ProductionMarketDataAuth
from test_mu_stream import FakeSocket


def contract(side='C',strike=100):
    return MUOptionContract('MU',f'MU    261218{side}{strike*1000:08}',f'.MU261218{side}{strike}','2026-12-18',strike,side)


class MUDataTests(unittest.TestCase):
    def test_non_mu_entrypoints_and_options_blocked(self):
        with m.MUMarketData() as data, patch.object(m,'_stream') as stream, patch.object(data._session,'chain') as chain:
            for symbol in ('AAPL','NVDA','SPY','MSFT'):
                with self.assertRaises(ValueError): data.underlying_snapshot(symbol)
                with self.assertRaises(ValueError): data.discover_options(symbol)
                with self.assertRaises(ValueError): MUOptionContract(symbol,'x','x','2026-12-18',100,'C')
            stream.assert_not_called();chain.assert_not_called()
        for symbol,streamer in [('AAPL  261218C00100000','.AAPL261218C100'),('MU    261218C00100000','.NVDA261218C100')]:
            with self.assertRaises(ValueError): MUOptionContract('MU',symbol,streamer,'2026-12-18',100,'C')
        with self.assertRaises(m.MarketDataError): m._subscription([contract()],{})
        with self.assertRaises(ValueError): MUOptionContract('MU','MU    261218C00100000','.MU261218C110','2026-12-18',100,'C')

    def test_discovery_uses_returned_streamer_and_pagination(self):
        c=contract()
        row={'underlying-symbol':'MU','root-symbol':'MU','symbol':c.symbol,'streamer-symbol':c.streamer_symbol,'expiration-date':c.expiration,'strike-price':str(c.strike),'option-type':c.option_type}
        session=m._Session()
        with patch.object(session,'_read',side_effect=[{'data':{'items':[row]},'pagination':{'total-pages':2}},{'data':{'items':[]},'pagination':{'total-pages':2}}]) as read,patch.object(m.time,'sleep'):
            self.assertEqual(session.chain(),(c,))
            self.assertEqual(read.call_count,2)
            self.assertEqual(read.call_args.args[0],'/option-chains/MU?per-page=250&page-offset=1')
        row['underlying-symbol']='SPY'
        with patch.object(session,'_read',return_value={'data':{'items':[row]}}):
            with self.assertRaises(ValueError): session.chain()

    def test_normalization_signed_greeks_missing_and_mid(self):
        now=datetime.now(timezone.utc).isoformat()
        events={'Quote':({'bidPrice':1,'askPrice':3,'bidSize':5,'askSize':8,'bidTime':now,'askTime':now},now),
                'Greeks':({'volatility':0.5,'delta':-0.4,'gamma':0.02,'theta':-0.1,'vega':0.3,'rho':-0.01,'time':now},now),
                'Summary':({'openInterest':123,'dayId':20000},now)}
        snap=m._normalize('.MU261218P100',events,contract('P'))
        self.assertEqual(snap.mid,2)
        self.assertEqual(snap.mark_source,'bid_ask_midpoint')
        self.assertEqual(snap.theta,-0.1)
        self.assertEqual(snap.open_interest,123)
        self.assertEqual(snap.freshness['Quote'],'FRESH')
        self.assertEqual(snap.freshness['Summary'],'UNKNOWN_EVENT_TIME')
        self.assertIn('last',snap.missing_fields)
        events['Quote'][0]['bidPrice']=4
        self.assertIsNone(m._normalize('MU',{'Quote':events['Quote']}).mid)

    def test_freshness_does_not_confuse_receipt_with_event(self):
        now=datetime.now(timezone.utc)
        old=(now-timedelta(hours=3)).isoformat()
        self.assertEqual(freshness(old,now.isoformat(),now),'STALE')
        self.assertEqual(freshness(None,now.isoformat(),now),'UNKNOWN_EVENT_TIME')
        self.assertEqual(freshness(None,None,now),'MISSING')
        self.assertEqual(freshness(now.isoformat(),old,now),'STALE')

    def test_decode_reordered_batched_and_unsubscribed(self):
        events={};fields={'Quote':['bidPrice','eventSymbol','askPrice']}
        m._decode(['Quote',[1,'MU',2,2,'MU',3]],fields,{'MU':{'Quote'}},events)
        self.assertEqual(events['MU']['Quote'][0]['bidPrice'],2)
        with self.assertRaises(m.MarketDataError):m._decode(['Quote',[1,'SPY',2]],fields,{'MU':{'Quote'}},events)
        with self.assertRaises(m.MarketDataError):m._decode(['Quote',[1,'MU']],fields,{'MU':{'Quote'}},events)

    def test_oauth_expiry_refresh_and_three_fields(self):
        session=m._Session()
        stored=ProductionMarketDataAuth(*(Secret(x) for x in ('fake-id','fake-secret','fake-refresh')))
        with patch.object(m,'load_production_market_data',return_value=stored),patch.object(a,'_exchange',return_value=(200,{'access_token':'fake-access','token_type':'Bearer','expires_in':900},None)) as exchange:
            session._login();session._login();self.assertEqual(exchange.call_count,1)
            self.assertEqual(set(exchange.call_args.kwargs['payload']),{'grant_type','refresh_token','client_secret'})
            session._deadline=0;session._login();self.assertEqual(exchange.call_count,2)
            self.assertNotIn('fake-access',repr(session))
            session.close();self.assertIsNone(session._access)

    def test_quote_expiration_stops(self):
        session=m._Session()
        with patch.object(session,'_read',return_value={'data':{'dxlink-url':'wss://a.dxfeed.com/realtime','token':'fake-token','expires-at':'2020-01-01T00:00:00Z'}}):
            session.status['last_read_http_status']=200
            with self.assertRaises(m.MarketDataError):session.quote_credentials()

    def test_bounded_reconnect_refreshes_quote_token_and_closes(self):
        session=Mock()
        session.quote_credentials.return_value=(Secret('wss://a.dxfeed.com/realtime'),Secret('fake-token'),99999999999)
        with patch.object(m,'connect',side_effect=OSError('fake-token')) as connect,patch.object(m.time,'sleep'):
            with self.assertRaises(m.MarketDataError):m._stream(session,seconds=.01)
            self.assertEqual(connect.call_count,2)
            self.assertEqual(session.quote_credentials.call_count,2)

    def test_protocol_partial_configs_and_options(self):
        c=contract();messages=[{'type':'SETUP','channel':0,'keepaliveTimeout':60},
            {'type':'AUTH_STATE','channel':0,'state':'UNAUTHORIZED'},
            {'type':'AUTH_STATE','channel':0,'state':'AUTHORIZED'},
            {'type':'CHANNEL_OPENED','channel':3},
            {'type':'FEED_CONFIG','channel':3,'dataFormat':'COMPACT','eventFields':{}},
            {'type':'FEED_CONFIG','channel':3,'dataFormat':'COMPACT','eventFields':{'Quote':['eventSymbol','bidPrice','askPrice']}},
            {'type':'FEED_DATA','channel':3,'data':['Quote',['MU',99,101,c.streamer_symbol,1,2]]}]
        ws=FakeSocket(messages);session=Mock();session.status={}
        session.quote_credentials.return_value=(Secret('wss://a.dxfeed.com/realtime'),Secret('fake-token'),99999999999)
        with patch.object(m,'connect',return_value=ws):
            under,options,status=m._stream(session,[c],{c.streamer_symbol:c},seconds=.02)
        self.assertEqual(under.mid,100);self.assertEqual(options[0].mid,1.5)
        self.assertTrue(status['clean_disconnect'])
        subs=next(x for x in ws.sent if x['type']=='FEED_SUBSCRIPTION')
        self.assertEqual({x['symbol'] for x in subs['add']},{'MU',c.streamer_symbol})
        self.assertNotIn('fake-token',repr((under,options,status)))

    def test_transport_allowlist_never_allows_trading_or_other_underlyings(self):
        with patch.object(a.http.client,'HTTPSConnection') as connect:
            for method,path in [('POST','/orders'),('GET','/accounts'),('GET','/option-chains/AAPL?per-page=250&page-offset=0'),('GET','/option-chains/MU?per-page=250&page-offset=0&extra=1')]:
                self.assertIsNotNone(a._exchange(method,path)[2])
            connect.assert_not_called()

    def test_sample_skips_expired_stops_trading_time(self):
        from dataclasses import replace
        with m.MUMarketData() as data:
            past=replace(contract(),stops_trading_at='2020-01-01T00:00:00+00:00')
            future=replace(contract('P'),stops_trading_at='2099-01-01T00:00:00+00:00')
            data._catalog={past.streamer_symbol:past,future.streamer_symbol:future}
            self.assertEqual(data.near_money_sample(100,2),(future,))

    def test_chain_response_has_separate_bounded_size_limit(self):
        raw=json.dumps({'data':{'items':[]},'padding':'x'*(a._LIMIT+1)}).encode()
        conn=Mock();conn.getresponse.return_value.status=200
        conn.getresponse.return_value.read.return_value=raw
        with patch.object(a.http.client,'HTTPSConnection',return_value=conn):
            status,data,error=a._exchange('GET','/option-chains/MU?per-page=250&page-offset=0')
            self.assertEqual(status,200);self.assertIsNone(error)
            conn.getresponse.return_value.read.assert_called_once_with(16*1024*1024+1)
            status,data,error=a._exchange('GET','/api-quote-tokens')
            self.assertEqual(error,'Response exceeded size limit')

    def test_all_subscriptions_enforce_eight_contract_cap(self):
        c=contract()
        with self.assertRaises(m.MarketDataError):m._subscription([c]*9,{c.streamer_symbol:c})

    def test_unpaginated_full_chain_does_not_repeat_read(self):
        c=contract()
        row={'underlying-symbol':'MU','root-symbol':'MU','symbol':c.symbol,'streamer-symbol':c.streamer_symbol,'expiration-date':c.expiration,'strike-price':str(c.strike),'option-type':c.option_type}
        session=m._Session()
        with patch.object(session,'_read',return_value={'data':{'items':[row]*251}}) as read:
            self.assertEqual(session.chain(),(c,));read.assert_called_once()
