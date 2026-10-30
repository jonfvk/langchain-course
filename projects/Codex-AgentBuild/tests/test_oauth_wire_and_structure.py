"""Offline checks: real stdlib HTTP serialization, fake socket and credentials."""
import http.client
import json
import unittest
from unittest.mock import Mock, patch
from src.data.market_data import authentication as a
from src.data.market_data.keychain import ProductionMarketDataAuth, Secret


class DiagnosticsTests(unittest.TestCase):
    def test_structure_and_safe_values(self):
        data = {'message': 'Invalid refresh token', 'status': 400,
                'errors': [{'code': 'invalid_grant'}], 'access_token': 'fake-access',
                'request': {'client_secret': 'fake-secret'}, 'fake-key-secret': 'hidden'}
        result = a._parse_error_response(json.dumps(data).encode(), 'application/json', {})
        structure = result['json_structure']
        self.assertEqual(structure['type'], 'object')
        self.assertIn('Invalid refresh token', repr(structure))
        self.assertIn('invalid_grant', repr(structure))
        for secret in ('fake-access', 'fake-secret', 'hidden'):
            self.assertNotIn(secret, repr(result))

    def test_root_types_and_hostile_safe_fields(self):
        for value, kind in (([], 'list'), ('fake-secret', 'string'), (None, 'null'), (123, 'number')):
            self.assertEqual(a._json_structure(value, {})['type'], kind)
        for value in ('Bearer abc', '{"client_secret":"abc"}', 'https://example.com/token', 'eyJhbGciOiJIUzI1NiJ9.abc.xyz'):
            result = a._json_structure({'message': value}, {})
            self.assertNotIn(value, repr(result))
        self.assertNotIn('invalid', a._safe_error_text('invalid', {'client_secret': 'invalid'}))

    def test_preserves_diagnostic_words_and_root_keys(self):
        fake = 'ABC123fakeRefreshToken789'
        message = 'Refresh token ' + fake + ' is invalid or expired; grant revoked, malformed, not found, does not exist for client'
        result = a._parse_error_response(json.dumps({'error_type': 'unknown-value', 'error_description': message, 'error': 'invalid_grant'}).encode(), 'application/json', {'refresh_token': fake})
        self.assertEqual(result['root_keys'], ['error_type', 'error_description', 'error'])
        text = result['error_message']
        self.assertNotIn(fake, repr(result))
        self.assertEqual(text, message.replace(fake, '<REDACTED>'))
        self.assertNotIn('unknown-value', repr(result))
        self.assertEqual(a._safe_error_text('Refresh token ABC123... is invalid', {}), 'Refresh token <REDACTED> is invalid')

    def test_response_tokens_and_embedded_request_are_redacted(self):
        data = {'access_token': 'shortfake', 'error_description': 'Token shortfake expired', 'error': 'invalid_grant'}
        result = a._parse_error_response(json.dumps(data).encode(), 'application/json', {})
        self.assertNotIn('shortfake', repr(result))
        self.assertIn('expired', repr(result))
        self.assertNotIn('fake-secret', a._safe_error_text('client_secret=fake-secret is invalid', {'client_secret': 'fake-secret'}))

    def test_actual_http_wire_serialization_offline(self):
        connection_type = http.client.HTTPSConnection
        chunks = []
        fake_socket = Mock()
        fake_socket.sendall.side_effect = lambda data: chunks.append(bytes(data))
        def connect(conn):
            conn.sock = fake_socket
        response = Mock(status=200)
        response.read.return_value = b'{"access_token":"fake-access","expires_in":900}'
        auth = ProductionMarketDataAuth(*(Secret(v) for v in ('fake-id','fake-secret','fake-refresh')))
        with patch.object(a, 'load_production_market_data', return_value=auth), patch.object(connection_type, 'connect', connect), patch.object(connection_type, 'getresponse', return_value=response), patch('socket.create_connection', side_effect=AssertionError('Network forbidden')):
            result = a.diagnose_minimal_oauth()
        self.assertTrue(result['oauth_succeeded'])
        headers, body = b''.join(chunks).split(b'\r\n\r\n', 1)
        lines = headers.decode('ascii').split('\r\n')
        self.assertEqual(lines[0], 'POST /oauth/token HTTP/1.1')
        fields = dict(line.split(': ', 1) for line in lines[1:])
        self.assertEqual(fields['Host'], 'api.tastyworks.com')
        self.assertEqual(fields['User-Agent'], 'codex-agentbuild/0.1')
        self.assertEqual(fields['Content-Type'], 'application/json')
        self.assertEqual(fields['Accept'], 'application/json')
        self.assertEqual(int(fields['Content-Length']), len(body))
        self.assertNotIn('Authorization', fields)
        obj = json.loads(body)
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj), {'grant_type', 'refresh_token', 'client_secret'})
        self.assertEqual(obj['grant_type'], 'refresh_token')
        self.assertEqual(obj['refresh_token'], 'fake-refresh')
        self.assertEqual(obj['client_secret'], 'fake-secret')
