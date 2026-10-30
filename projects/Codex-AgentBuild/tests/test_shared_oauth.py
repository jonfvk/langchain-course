import json
import unittest
from unittest.mock import Mock, patch
from src.security.oauth_request import build_refresh_request
from src.security.sandbox_keychain import SandboxAuth
from src.data.market_data.keychain import ProductionMarketDataAuth, Secret
from src.security.sandbox_oauth import diagnose_sandbox_oauth
from src.data.market_data.authentication import _exchange

class SharedOAuthTests(unittest.TestCase):
    def credentials(self, cls):
        return cls(*(Secret(x) for x in ('fake-id','fake-secret','fake-refresh')))

    def test_identical_wire_except_destination(self):
        requests=[]
        for env,cls,host in (('production',ProductionMarketDataAuth,'api.tastyworks.com'),
                            ('sandbox',SandboxAuth,'api.cert.tastyworks.com')):
            r=build_refresh_request(env,self.credentials(cls));requests.append(r)
            self.assertEqual((r.method,r.host,r.path),('POST',host,'/oauth/token'))
            self.assertEqual(r.headers,{'User-Agent':'codex-agentbuild/0.1',
                'Content-Type':'application/json','Accept':'application/json'})
            self.assertNotIn('authorization', {x.lower() for x in r.headers})
            self.assertRegex(r.headers['User-Agent'],r'^[\w-]+/[\d.]+$')
            self.assertEqual(list(json.loads(r.body)),['grant_type','refresh_token','client_secret'])
            self.assertNotIn('fake-secret',repr(r))
        self.assertEqual(requests[0].body,requests[1].body)
        self.assertEqual(requests[0].headers,requests[1].headers)

    def test_cross_environment_rejected(self):
        for env,cls in (('sandbox',ProductionMarketDataAuth),('production',SandboxAuth),
                        ('unknown',SandboxAuth)):
            with self.assertRaises(ValueError):build_refresh_request(env,self.credentials(cls))

    def test_production_no_authorization_on_refresh(self):
        with patch('http.client.HTTPSConnection') as factory:
            result=_exchange('POST','/oauth/token',bearer='fake-access')
        factory.assert_not_called()
        self.assertIsNone(result[0])

    def test_sandbox_single_attempt_sanitized_error(self):
        connection=Mock();response=connection.getresponse.return_value
        response.status=400;response.getheader.return_value='application/json'
        response.read.return_value=b'{"error":"invalid_grant","error_description":"fake-refresh Invalid JWT"}'
        with patch('src.security.sandbox_oauth.load_sandbox',return_value=self.credentials(SandboxAuth)), patch('http.client.HTTPSConnection',return_value=connection) as factory:
            result=diagnose_sandbox_oauth()
        self.assertEqual(factory.call_args.args,('api.cert.tastyworks.com',))
        connection.request.assert_called_once()
        self.assertEqual(result['http_status'],400)
        self.assertNotIn('fake-refresh',repr(result))
        self.assertIn('Invalid JWT',result['error_message'])
        self.assertEqual(set(json.loads(connection.request.call_args.kwargs['body'])),{'grant_type','refresh_token','client_secret'})
        self.assertNotIn('Authorization',connection.request.call_args.kwargs['headers'])

    def test_sandbox_rejects_production_before_connection(self):
        with patch('src.security.sandbox_oauth.load_sandbox',return_value=self.credentials(ProductionMarketDataAuth)), patch('http.client.HTTPSConnection') as factory:
            result=diagnose_sandbox_oauth()
        factory.assert_not_called()
        self.assertFalse(result['oauth_succeeded'])

    def test_standalone_error_description_is_preserved_safely(self):
        connection=Mock();response=connection.getresponse.return_value
        response.status=400;response.getheader.return_value='application/json'
        response.read.return_value=b'{"error_description":"fake-refresh Invalid JWT","unknown":"never-print-this"}'
        with patch('src.security.sandbox_oauth.load_sandbox',return_value=self.credentials(SandboxAuth)), patch('http.client.HTTPSConnection',return_value=connection):
            result=diagnose_sandbox_oauth()
        self.assertEqual(result['error_description'],'<REDACTED> Invalid JWT')
        self.assertEqual(result['error_message'],'<REDACTED> Invalid JWT')
        self.assertNotIn('fake-refresh',repr(result))
        self.assertNotIn('never-print-this',repr(result))
        connection.request.assert_called_once()
