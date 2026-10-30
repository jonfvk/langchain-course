import json
import unittest
from unittest.mock import patch
from src.data.market_data.quote_validation import inspect_quote
from src.data.market_data import mu_stream as m


class QuoteValidationTests(unittest.TestCase):
    def quote(self,url='wss://tasty-openapi-ws.dxfeed.com/realtime'):
        return {'data':{'dxlink-url':url,'token':'fake-secret-token','expires-at':'2026-09-18T12:00:00Z','issued-at':'2026-09-17T12:00:00Z'},'context':'/api-quote-tokens'}

    def test_documented_structure(self):
        r=inspect_quote(self.quote(),200)
        self.assertTrue(r['accepted'])
        self.assertEqual(r['root_keys'],['data','context'])
        self.assertEqual(r['url_hostname'],'tasty-openapi-ws.dxfeed.com')
        self.assertEqual(r['url_path'],'/realtime')
        self.assertEqual(r['token_length'],17)
        self.assertIsNotNone(r['expires_at'])
        self.assertNotIn('fake-secret-token',json.dumps(r))

    def test_exact_rejections(self):
        for url,rule in [
            ('ws://tasty-openapi-ws.dxfeed.com/realtime','malformed_or_non_wss_url'),
            ('wss://user:password@tasty-openapi-ws.dxfeed.com/realtime','embedded_userinfo_forbidden'),
            ('wss://tasty-openapi-ws.dxfeed.com.evil.test/realtime','hostname_must_be_dxfeed_domain'),
            ('wss://tasty-openapi-ws.dxfeed.com:444/realtime','port_must_be_default_or_443'),
            ('wss://tasty-openapi-ws.dxfeed.com/realtime#secret','fragment_forbidden')]:
            self.assertEqual(inspect_quote(self.quote(url),200)['rejection_rule'],rule)

    def test_secrets_in_query_and_path_not_displayed(self):
        r=inspect_quote(self.quote('wss://tasty-openapi-ws.dxfeed.com/fake-secret-token?q=fake-secret-token'),200)
        self.assertTrue(r['query_parameters_exist'])
        self.assertNotIn('fake-secret-token',json.dumps(r))
        self.assertNotIn('?q=',json.dumps(r))

    def test_wrong_schema(self):
        for value,rule in [([], 'root_must_be_object'),({},'data_must_be_object'),({'data':{}},'data.dxlink-url_must_be_nonempty_string')]:
            self.assertEqual(inspect_quote(value,200)['rejection_rule'],rule)
        value=self.quote();value['data']['token']=''
        self.assertEqual(inspect_quote(value,200)['rejection_rule'],'data.token_must_be_nonempty_string')

    def test_diagnostic_entry_never_connects(self):
        with patch.object(m,'_fresh_credentials',return_value=('fake-url','fake-token')),patch.object(m,'connect') as connect:
            r=m.inspect_quote_once()
            connect.assert_not_called()
            self.assertNotIn('fake-token',repr(r))

    def test_dynamic_dxfeed_destinations(self):
        for host in ('dxfeed.com','tasty-openapi-dxlink-md-ws.dxfeed.com'):
            self.assertTrue(inspect_quote(self.quote('wss://' + host + '/realtime'),200)['accepted'])
        for url in ('ws://dxfeed.com/a','http://dxfeed.com','https://dxfeed.com','wss://dxfeed.com.evil.com/a','wss://evildxfeed.com/a','wss://127.0.0.1/a','wss://[::1]/a','wss://a..dxfeed.com/a','wss://u:p@dxfeed.com/a','wss://dxfeed.com/\n','garbage'):
            self.assertFalse(inspect_quote(self.quote(url),200)['accepted'])
