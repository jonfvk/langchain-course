import json
import logging
import time
import unittest
from unittest.mock import Mock, patch
from src.data.market_data import mu_stream as m
from src.data.market_data.keychain import ProductionMarketDataAuth, Secret


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.close_code = None
    def send(self, message):
        self.sent.append(json.loads(message))
    def recv(self, timeout):
        if self.messages:
            return json.dumps(self.messages.pop(0))
        time.sleep(max(0, timeout))
        raise TimeoutError
    def close(self, code):
        self.close_code = code


def frames():
    return [
        {'type':'SETUP','channel':0},
        {'type':'AUTH_STATE','channel':0,'state':'UNAUTHORIZED'},
        {'type':'AUTH_STATE','channel':0,'state':'AUTHORIZED'},
        {'type':'CHANNEL_OPENED','channel':3,'service':'FEED'},
        {'type':'FEED_CONFIG','channel':3,'dataFormat':'COMPACT','eventFields':m._FIELDS},
        {'type':'FEED_DATA','channel':3,'data':[
            'Quote',['Quote','MU',100.1,100.2,10,20,1789574400000,1789574401000],
            'Trade',['Trade','MU',100.15,5,100000,1789574400000]]},
    ]


class MUStreamTests(unittest.TestCase):
    def test_symbols_rejected_before_credentials_or_subscription(self):
        for symbol in ('AAPL','NVDA','MSFT','SPY','mu',' MU','MU ','MU260918C100','MU:Q',None):
            with self.subTest(symbol=symbol), patch.object(m,'_fresh_credentials') as creds, patch.object(m,'connect') as connect:
                with self.assertRaises(ValueError):
                    m.subscription(symbol)
                with self.assertRaises(ValueError):
                    m.stream_mu_once(symbol)
                creds.assert_not_called()
                connect.assert_not_called()
        self.assertEqual({x['symbol'] for x in m.subscription('MU')['add']}, {'MU'})

    def run_fake(self, messages):
        ws = FakeSocket(messages)
        with patch.object(m,'_fresh_credentials',return_value=(Secret('wss://fake.invalid'),Secret('fake-dxlink-token'))), patch.object(m,'connect',return_value=ws) as connect, patch.object(m,'_DURATION',0.02):
            result = m.stream_mu_once()
            self.assertEqual(connect.call_count,1)
            self.assertIsNone(connect.call_args.kwargs['proxy'])
            self.assertEqual(connect.call_args.kwargs['close_timeout'],1)
            self.assertTrue(connect.call_args.kwargs['logger'].disabled)
        return ws,result

    def test_protocol_order_snapshot_and_clean_close(self):
        ws,r = self.run_fake(frames())
        self.assertEqual([x['type'] for x in ws.sent], ['SETUP','AUTH','CHANNEL_REQUEST','FEED_SETUP','FEED_SUBSCRIPTION','CHANNEL_CANCEL'])
        self.assertTrue(r['dxlink_auth_success'] and r['subscription_success'] and r['clean_disconnect'])
        self.assertEqual((r['bid'],r['ask'],r['last'],r['volume']), (100.1,100.2,100.15,100000))
        self.assertIsNotNone(r['trade_timestamp'])
        self.assertLess(r['connected_seconds'],1)
        self.assertNotIn('fake-dxlink-token',repr(r))

    def test_auth_rejection_no_subscription_no_token_output(self):
        messages=frames()[:2]+[{'type':'AUTH_STATE','channel':0,'state':'UNAUTHORIZED','message':'fake-dxlink-token'}]
        ws,r=self.run_fake(messages)
        self.assertFalse(r['dxlink_auth_success'])
        self.assertFalse(r['subscription_sent'])
        self.assertTrue(r['clean_disconnect'])
        self.assertNotIn('fake-dxlink-token',repr(r))

    def test_quiet_feed_timeout_is_bounded(self):
        ws,r=self.run_fake(frames()[:5])
        self.assertTrue(r['subscription_sent'])
        self.assertFalse(r['subscription_success'])
        self.assertTrue(r['clean_disconnect'])
        self.assertLess(r['connected_seconds'],1)

    def test_schema_reordering_multiple_rows_and_nonfinite(self):
        r=dict(quote_events=0,trade_events=0)
        cols=['eventSymbol','askPrice','bidPrice']
        m._consume(['Quote',['MU',2,1,'MU',3,2]],{'Quote':cols},r)
        self.assertEqual((r['bid'],r['ask'],r['quote_events']),(2,3,2))
        m._consume(['Quote',['MU','NaN',float('inf')]],{'Quote':cols},r)
        self.assertIsNone(r['bid'])
        self.assertIsNone(r['ask'])
        with self.assertRaises(ValueError):
            m._consume(['Quote',['SPY',2,1]],{'Quote':cols},r)
        with self.assertRaises(ValueError):
            m._consume(['Quote',['MU',2]],{'Quote':cols},r)

    def test_credentials_three_field_oauth_and_single_quote_request(self):
        creds=ProductionMarketDataAuth(*(Secret(v) for v in ('fake-id','fake-secret','fake-refresh')))
        result={}
        with patch.object(m,'load_production_market_data',return_value=creds), patch.object(m.auth,'_exchange',side_effect=[
            (200,{'access_token':'fake-access','token_type':'Bearer','scope':'read'},None),
            (200,{'data':{'dxlink-url':'wss://tasty-openapi-ws.dxfeed.com/realtime','token':'fake-dxlink-token'}},None)]) as exchange:
            url,token=m._fresh_credentials(result)
        self.assertEqual(exchange.call_count,2)
        self.assertEqual(exchange.call_args_list[0].args,('POST','/oauth/token'))
        self.assertEqual(set(exchange.call_args_list[0].kwargs['payload']),{'grant_type','refresh_token','client_secret'})
        self.assertEqual(exchange.call_args_list[1].args,('GET','/api-quote-tokens'))
        self.assertTrue(result['oauth_success'] and result['quote_token_success'])
        self.assertNotIn('fake-',repr((url,token,result)))

    def test_failure_stops_before_websocket(self):
        with patch.object(m,'load_production_market_data',side_effect=RuntimeError('fake-secret')), patch.object(m,'connect') as connect:
            result=m.stream_mu_once()
            connect.assert_not_called()
            self.assertNotIn('fake-secret',repr(result))

    def test_oauth_failure_never_fetches_quote_credentials(self):
        creds=ProductionMarketDataAuth(*(Secret(v) for v in ('fake-id','fake-secret','fake-refresh')))
        for oauth in ({'access_token':'fake-access','token_type':'Bearer','scope':'trade'}, {}):
            with patch.object(m,'load_production_market_data',return_value=creds), patch.object(m.auth,'_exchange',return_value=(200,oauth,None)) as exchange, patch.object(m,'connect') as connect:
                result=m.stream_mu_once()
                exchange.assert_called_once()
                connect.assert_not_called()
                self.assertFalse(result['oauth_success'])

    def test_wrong_websocket_destination_rejected(self):
        creds=ProductionMarketDataAuth(*(Secret(v) for v in ('fake-id','fake-secret','fake-refresh')))
        for url in ('ws://tasty-openapi-ws.dxfeed.com/realtime','wss://evil.invalid/realtime','wss://unrelated.example/realtime'):
            with patch.object(m,'load_production_market_data',return_value=creds), patch.object(m.auth,'_exchange',side_effect=[(200,{'access_token':'fake-access','token_type':'Bearer'},None),(200,{'data':{'dxlink-url':url,'token':'fake-token'}},None)]), patch.object(m,'connect') as connect:
                r=m.stream_mu_once()
                connect.assert_not_called()
                self.assertFalse(r['dxlink_connection_success'])
