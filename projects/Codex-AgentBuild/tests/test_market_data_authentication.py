import json
import unittest
from unittest.mock import Mock, patch

from src.data.market_data import authentication as a
from src.data.market_data.keychain import ProductionMarketDataAuth, Secret


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.loader = patch.object(a, 'load_production_market_data', return_value=ProductionMarketDataAuth(*(Secret(v) for v in ('fake-id', 'fake-secret', 'fake-refresh'))))
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.transport = patch.object(a.http.client, 'HTTPSConnection')
        self.factory = self.transport.start()
        self.addCleanup(self.transport.stop)

    def responses(self, *items):
        connections = []
        for status, body in items:
            conn = Mock()
            response = conn.getresponse.return_value
            response.status = status
            response.read.return_value = json.dumps(body).encode()
            connections.append(conn)
        self.factory.side_effect = connections
        return connections

    def oauth(self, **extra):
        return dict(access_token='fake-access', token_type='Bearer', expires_in=900, **extra)

    def test_success_and_fixed_requests(self):
        connections = self.responses((200, self.oauth(scope='read')), (200, {'data': {'token': 'fake-dxlink', 'dxlink-url': 'wss://fake.invalid/?secret=fake-dxlink', 'expires-in': 86400}}))
        result = a.verify_mu_market_data_access()
        self.assertTrue(result.oauth_succeeded and result.quote_tokens_succeeded)
        self.assertEqual(result.oauth_lifetime_seconds, 900)
        self.assertEqual(result.quote_lifetime_seconds, 86400)
        self.assertEqual(self.factory.call_count, 2)
        for call in self.factory.call_args_list:
            self.assertEqual(call.args, ('api.tastyworks.com',))
        request = connections[0].request.call_args
        self.assertEqual(request.args, ('POST', '/oauth/token'))
        payload = json.loads(request.kwargs['body'])
        self.assertEqual(set(payload), {'grant_type', 'refresh_token', 'client_secret'})
        self.assertNotIn('scope', payload)
        self.assertNotIn('client_id', payload)
        self.assertEqual(payload['grant_type'], 'refresh_token')
        self.assertEqual(payload['refresh_token'], 'fake-refresh')
        self.assertEqual(connections[1].request.call_args.args, ('GET', '/api-quote-tokens'))
        self.assertEqual(connections[1].request.call_args.kwargs['headers']['Authorization'], 'Bearer fake-access')
        self.assertNotIn('fake-', repr(result))
        for conn in connections:
            conn.close.assert_called_once()
            conn.set_debuglevel.assert_called_once_with(0)

    def test_http_failure_or_redirect_never_retries_or_reads_body(self):
        for status in (301, 302, 307, 401, 403, 429, 500):
            connections = self.responses((status, {'error': 'fake-secret'}))
            result = a.verify_mu_market_data_access()
            self.assertFalse(result.oauth_succeeded)
            self.assertEqual(result.oauth_http_status, status)
            if status < 400:
                connections[0].getresponse.return_value.read.assert_not_called()
            self.assertNotIn('fake-secret', repr(result))

    def test_invalid_or_overprivileged_oauth_stops(self):
        for body in ({}, self.oauth(scope='read trade'), self.oauth(scope=['read']), dict(access_token='x', token_type='Basic')):
            connections = self.responses((200, body))
            self.assertFalse(a.verify_mu_market_data_access().oauth_succeeded)
            connections[0].request.assert_called_once()

    def test_quote_failure_and_missing_fields(self):
        for status, body in ((403, {}), (200, {}), (200, {'data': {'token': ''}})):
            self.responses((200, self.oauth()), (status, body))
            result = a.verify_mu_market_data_access()
            self.assertTrue(result.oauth_succeeded)
            self.assertFalse(result.quote_tokens_succeeded)
            self.assertEqual(result.quote_http_status, status)

    def test_keychain_failure_never_connects(self):
        with patch.object(a, 'load_production_market_data', side_effect=RuntimeError('fake-secret')):
            result = a.verify_mu_market_data_access()
        self.factory.assert_not_called()
        self.assertNotIn('fake-secret', repr(result))

    def test_transport_error_sanitized(self):
        self.factory.side_effect = RuntimeError('fake-secret')
        self.assertNotIn('fake-secret', repr(a.verify_mu_market_data_access()))
        self.factory.assert_called_once()

    def test_disallowed_paths_never_connect(self):
        for method, path in (('POST', '/orders'), ('GET', '/accounts'), ('GET', 'https://evil.invalid'), ('DELETE', '/api-quote-tokens')):
            self.assertIsNotNone(a._exchange(method, path)[2])
        self.factory.assert_not_called()

    def test_invalid_and_oversize_json(self):
        for raw in (b'not-json fake-secret', b'[]', b'x' * (a._LIMIT + 1)):
            connections = self.responses((200, {}))
            connections[0].getresponse.return_value.read.return_value = raw
            result = a.verify_mu_market_data_access()
            self.assertFalse(result.oauth_succeeded)
            self.assertNotIn('fake-secret', repr(result))

    def test_oauth_only_never_fetches_quote_token(self):
        self.responses((200, self.oauth(scope='read')))
        result = a.verify_mu_oauth()
        self.assertTrue(result.oauth_succeeded)
        self.assertIsNone(result.quote_http_status)
        self.factory.assert_called_once()

    def test_server_error_redacts_credentials(self):
        self.responses((400, {'error': {'code': 'invalid_grant', 'message': 'Rejected fake-refresh fake-secret'}}))
        result = a.verify_mu_oauth()
        self.assertIn('invalid_grant', result.error)
        self.assertNotIn('fake-refresh', result.error)
        self.assertNotIn('fake-secret', result.error)

    def test_certificate_error_diagnostic(self):
        import ssl
        exc = ssl.SSLCertVerificationError(1, 'fake-secret')
        exc.verify_code = 20
        self.factory.side_effect = exc
        result = a.verify_mu_oauth()
        self.assertIn('SSLCertVerificationError', result.error)
        self.assertIn('unable to get local issuer certificate', result.error)
        self.assertNotIn('fake-secret', result.error)

    def test_minimal_diagnostic_request_and_success(self):
        connections = self.responses((200, self.oauth(scope='read trade')))
        result = a.diagnose_minimal_oauth()
        self.assertTrue(result['oauth_succeeded'])
        self.assertTrue(result['access_token_nonempty'])
        self.assertEqual(result['expires_in'], 900)
        self.assertNotIn('fake-access', repr(result))
        self.factory.assert_called_once()
        request = connections[0].request.call_args
        self.assertEqual(request.args, ('POST', '/oauth/token'))
        self.assertEqual(json.loads(request.kwargs['body']), {
            'grant_type': 'refresh_token', 'refresh_token': 'fake-refresh', 'client_secret': 'fake-secret'})
        self.assertEqual(request.kwargs['headers'], {
            'User-Agent': 'codex-agentbuild/0.1', 'Content-Type': 'application/json', 'Accept': 'application/json'})

    def test_nested_error_diagnostics(self):
        connections = self.responses((400, {'error': {'code': 'invalid_grant', 'message': 'Rejected fake-secret fake-refresh'}}))
        connections[0].getresponse.return_value.getheader.return_value = 'application/json; charset=utf-8'
        result = a.diagnose_minimal_oauth()
        self.assertEqual(result['http_status'], 400)
        self.assertTrue(result['json_error_envelope_parsed'])
        self.assertEqual(result['content_type'], 'application/json')
        self.assertEqual(result['error_code'], 'invalid_grant')
        self.assertNotIn('fake-secret', repr(result))
        self.assertNotIn('fake-refresh', repr(result))
        self.factory.assert_called_once()

    def test_non_json_diagnostics_never_dump_body(self):
        raw = b'<html>fake-secret fake-refresh</html>'
        result = a._parse_error_response(raw, 'text/html', {})
        self.assertFalse(result['json_error_envelope_parsed'])
        self.assertEqual(result['response_length'], len(raw))
        self.assertEqual(result['response_characterization'], 'non-JSON response')
        self.assertNotIn('fake-secret', repr(result))
        for raw in (b'[]', b'{}', b'null', b'bad json'):
            self.assertFalse(a._parse_error_response(raw, 'application/json', {})['json_error_envelope_parsed'])

    def test_expiry_sanitization(self):
        self.assertEqual(a._expiry({'expires-at': '2026-09-17T12:00:00Z'}), '2026-09-17T12:00:00+00:00')
        self.assertIsNone(a._expiry({'expires-at': 'fake-secret'}))
        for value in ('fake-secret', float('inf'), float('nan'), True):
            self.assertIsNone(a._lifetime({'expires_in': value}))


if __name__ == '__main__':
    unittest.main()
