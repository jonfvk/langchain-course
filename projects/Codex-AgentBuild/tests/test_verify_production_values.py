import getpass
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from src.data.market_data.keychain import ProductionMarketDataAuth, Secret

spec = importlib.util.spec_from_file_location('verify_values', Path(__file__).resolve().parents[1] / 'scripts/verify-production-credentials.py')
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class VerificationTests(unittest.TestCase):
    def test_exact_bytes_and_whitespace(self):
        self.assertTrue(script.compare('fake-value', 'fake-value')['matches'])
        result = script.compare('fake-value', ' fake-value\r\n')
        self.assertFalse(result['matches'])
        for name in ('edge_whitespace_difference', 'cr_difference', 'newline_difference'):
            self.assertTrue(result[name])
        self.assertFalse(script.compare('é', 'e\u0301')['matches'])
        self.assertEqual(script.compare('é', 'é')['stored_length'], 2)
        self.assertTrue(script.compare(' x', 'x ')['edge_whitespace_difference'])

    def run_script(self, inputs, tty=True, argv=None):
        auth = ProductionMarketDataAuth(*(Secret(x) for x in ('fake-id', 'fake-secret', 'fake-refresh')))
        with patch.object(script, 'load_production_market_data', return_value=auth) as loader, patch.object(script.sys, 'argv', argv or ['verify']), patch.object(script.sys.stdin, 'isatty', return_value=tty), patch.object(script.sys.stderr, 'isatty', return_value=tty), patch.object(script.getpass, 'getpass', side_effect=inputs) as prompt, patch('sys.stdout', new_callable=io.StringIO) as output:
            status = script.main()
            return status, output.getvalue(), prompt.call_count, loader.call_count

    def test_hidden_prompts_and_safe_output(self):
        status, output, prompts, _ = self.run_script(['fake-secret', 'fake-refresh'])
        self.assertEqual(status, 0)
        self.assertEqual(prompts, 2)
        self.assertIn('Client Secret matches Keychain: YES', output)
        self.assertIn('Refresh Token matches Keychain: YES', output)
        self.assertNotIn('fake-secret', output)
        self.assertNotIn('fake-refresh', output)
        self.assertIn('stored=11, entered=11', output)

    def test_mismatch(self):
        status, output, _, _ = self.run_script(['different', ' fake-refresh'])
        self.assertEqual(status, 0)
        self.assertIn('Client Secret matches Keychain: NO', output)
        self.assertIn('Refresh Token matches Keychain: NO', output)
        self.assertNotIn('different', output)

    def test_abort_echo_warning_and_noninteractive(self):
        for error in (KeyboardInterrupt, EOFError, getpass.GetPassWarning, RuntimeError('fake-secret')):
            status, output, _, _ = self.run_script(error)
            self.assertEqual(status, 1)
            self.assertEqual(output, '')
        for kwargs in ({'tty': False}, {'argv': ['verify', 'forbidden']}):
            status, output, prompts, loads = self.run_script([], **kwargs)
            self.assertEqual((status, output, prompts, loads), (1, '', 0, 0))
