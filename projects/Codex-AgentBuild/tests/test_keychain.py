import importlib.util
import io
import json
import logging
from pathlib import Path
import pickle
import traceback
import unittest
from unittest.mock import Mock, patch

from src.data.market_data import keychain as kc

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_keychain', ROOT / 'scripts/setup-production-keychain.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
FAKES = ['fake-id-123', 'fake-secret-456', 'fake-refresh-789']


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.mock_backend = patch.object(kc, '_backend', return_value=self.backend)
        self.mock_backend.start()
        self.addCleanup(self.mock_backend.stop)
        self.auth = kc.ProductionMarketDataAuth(*(kc.Secret(s) for s in FAKES))

    def test_round_trip_single_production_item(self):
        kc.store_production_market_data(self.auth)
        service, account, payload = self.backend.set_password.call_args.args
        self.assertEqual(service, kc.SERVICE)
        self.assertNotEqual(service, kc.SANDBOX_SERVICE)
        self.assertEqual(account, kc.ACCOUNT)
        self.backend.get_password.return_value = payload
        loaded = kc.load_production_market_data()
        self.assertEqual([loaded.client_id._for_auth(), loaded.client_secret._for_auth(), loaded.refresh_token._for_auth()], FAKES)
        self.backend.get_password.assert_called_once_with(kc.SERVICE, kc.ACCOUNT)

    def test_logging_redacts_objects_and_fields(self):
        stream = io.StringIO()
        logger = logging.Logger('test')
        logger.addHandler(logging.StreamHandler(stream))
        logger.warning('%s %r %s %r', self.auth, self.auth, self.auth.client_secret, self.auth.refresh_token)
        for value in FAKES:
            self.assertNotIn(value, stream.getvalue())
        self.assertIn('REDACTED', stream.getvalue())
        with self.assertRaises(TypeError):
            pickle.dumps(self.auth)
        with self.assertRaises(TypeError):
            json.dumps(self.auth)

    def test_failures_omit_backend_exception_and_traceback(self):
        for operation in (kc.load_production_market_data, lambda: kc.store_production_market_data(self.auth)):
            self.backend.get_password.side_effect = ValueError(FAKES[1])
            self.backend.set_password.side_effect = ValueError(FAKES[1])
            try:
                operation()
            except kc.KeychainError:
                self.assertNotIn(FAKES[1], traceback.format_exc())
            else:
                self.fail('Expected safe error')

    def test_missing_malformed_incomplete_and_empty_fail_closed(self):
        for payload in (None, 'bad-json', '{}', '[]', json.dumps(dict(client_id='', client_secret='x', refresh_token='y'))):
            self.backend.get_password.return_value = payload
            with self.assertRaises(kc.KeychainError):
                kc.load_production_market_data()

    def test_setup_hides_all_fields(self):
        with patch.object(setup.sys, 'argv', ['setup']), patch.object(setup.sys.stdin, 'isatty', return_value=True), patch.object(setup.sys.stderr, 'isatty', return_value=True), patch.object(setup.getpass, 'getpass', side_effect=FAKES) as prompt, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(setup.main(), 0)
            self.assertEqual(prompt.call_count, 3)
            for value in FAKES:
                self.assertNotIn(value, output.getvalue())
        self.backend.set_password.assert_called_once()

    def test_setup_cancellation_and_echo_fallback_do_not_write(self):
        import getpass
        for error in (KeyboardInterrupt, EOFError, getpass.GetPassWarning):
            with patch.object(setup.sys, 'argv', ['setup']), patch.object(setup.sys.stdin, 'isatty', return_value=True), patch.object(setup.sys.stderr, 'isatty', return_value=True), patch.object(setup.getpass, 'getpass', side_effect=error), patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as err:
                # stderr was replaced; explicitly restore the TTY condition.
                with patch.object(err, 'isatty', return_value=True):
                    self.assertEqual(setup.main(), 1)
        self.backend.set_password.assert_not_called()

    def test_noninteractive_and_arguments_rejected(self):
        for argv, tty in ((['setup', 'forbidden'], True), (['setup'], False)):
            with patch.object(setup.sys, 'argv', argv), patch.object(setup.sys.stdin, 'isatty', return_value=tty), patch('sys.stderr', new_callable=io.StringIO), patch.object(setup.getpass, 'getpass') as prompt:
                self.assertEqual(setup.main(), 1)
                prompt.assert_not_called()
        self.backend.set_password.assert_not_called()

    def test_broker_has_no_production_loader_dependency(self):
        for path in (ROOT / 'src/broker').rglob('*.py'):
            text = path.read_text()
            self.assertNotIn('market_data', text)
            self.assertNotIn('load_production_market_data', text)
            self.assertNotIn(kc.SERVICE, text)
            # Sandbox-only Keychain imports are permitted for authorized MCP.
            for line in text.splitlines():
                if 'import' in line and 'keychain' in line:
                    self.assertIn('src.security.sandbox_keychain', line)
        launcher = (ROOT / 'scripts/tastytrade-mcp-readonly.sh').read_text()
        self.assertNotIn(kc.SERVICE, launcher)
        self.assertIn('TASTYTRADE_ENV=sandbox', launcher)


class BackendTests(unittest.TestCase):
    def test_unsupported_platform_has_no_fallback(self):
        with patch.object(kc.sys, 'platform', 'linux'):
            with self.assertRaises(kc.KeychainError):
                kc._backend()

    def test_explicit_macos_backend_without_keychain_access(self):
        with patch.object(kc.sys, 'platform', 'darwin'):
            from keyring.backends.macOS import Keyring
            self.assertIsInstance(kc._backend(), Keyring)


if __name__ == '__main__':
    unittest.main()
