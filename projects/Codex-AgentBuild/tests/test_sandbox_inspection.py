import os
import unittest
from unittest.mock import patch
from src.broker.sandbox_inspection import (SandboxInspection, PreflightError,
    child_environment, verify_instructions, READS)
from src.security.sandbox_keychain import SandboxAuth
from src.data.market_data.keychain import Secret, ProductionMarketDataAuth
from src.risk.capital import CapitalPolicy


class SandboxPreflightTests(unittest.TestCase):
    def auth(self):
        return SandboxAuth(Secret('fake-id'), Secret('fake-secret'), Secret('fake-refresh'))

    def test_environment_required(self):
        for value in (None, '', 'production', 'SANDBOX', 'sandbx'):
            with self.subTest(value=value), self.assertRaises(PreflightError):
                SandboxInspection(value)

    def test_clean_child_environment(self):
        with patch.dict(os.environ, {'TASTYTRADE_API_URL': 'https://api.tastyworks.com',
                'NODE_OPTIONS': 'unsafe', 'HTTPS_PROXY': 'unsafe',
                'TASTYTRADE_REFRESH_TOKEN': 'fake-production-refresh'}):
            env = child_environment('sandbox', self.auth())
        self.assertEqual(env['TASTYTRADE_ENV'], 'sandbox')
        self.assertEqual(env['TASTYTRADE_READ_ONLY'], '1')
        self.assertEqual(env['TASTYTRADE_REFRESH_TOKEN'], 'fake-refresh')
        for key in ('TASTYTRADE_API_URL','NODE_OPTIONS','HTTPS_PROXY'):
            self.assertNotIn(key, env)

    def test_production_type_rejected(self):
        auth = ProductionMarketDataAuth(Secret('fake-id'), Secret('fake-secret'), Secret('fake-refresh'))
        with self.assertRaises(PreflightError): child_environment('sandbox', auth)

    def test_server_must_prove_sandbox_and_readonly(self):
        good='SANDBOX (https://api.cert.tastyworks.com). No real money. Read-only mode is ON'
        verify_instructions({'instructions': good})
        for text in ('', 'PRODUCTION (https://api.tastyworks.com).',
                     good.replace('api.cert.tastyworks.com','evil.test'),
                     good.replace('Read-only mode is ON','')):
            with self.assertRaises(PreflightError): verify_instructions({'instructions': text})

    def test_no_write_dispatch(self):
        client=SandboxInspection('sandbox');client.sandbox_verified=True
        for name in ('tastytrade_place_order','tastytrade_dry_run_order',
                     'tastytrade_cancel_order','tastytrade_get_quote_token'):
            with self.assertRaises(PreflightError):client.read(name)
        self.assertEqual(len(READS), 4)

    def test_error_body_never_reported(self):
        client=SandboxInspection('sandbox'); client.sandbox_verified=True
        with patch.object(client, '_rpc', return_value={'isError':True,
            'structuredContent': {'code':'auth_failed','message':'fake-secret',
                                  'upstream':{'status':400,'body':'fake-refresh'}}}):
            with self.assertRaises(PreflightError) as caught:
                client.read('tastytrade_get_accounts')
        self.assertEqual(str(caught.exception), 'MCP_auth_failed; HTTP_STATUS=400')

    def test_no_percentage_loss_caps(self):
        policy=CapitalPolicy.from_file('config/capital-policy.json')
        self.assertEqual(str(policy.target_capital_deployment),'0.70')
        self.assertFalse(any('loss_fraction' in key for key in vars(policy)))

    def test_actual_child_environment_audit_with_fake_credentials(self):
        import hashlib
        import json
        import subprocess
        from src.broker.sandbox_inspection import ROOT, NODE
        env=child_environment('sandbox',self.auth())
        process=subprocess.Popen([str(NODE),str(ROOT/'scripts/sandbox-mcp-audit.mjs')],
            cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                self.assertTrue(selector.select(10),'MCP audit startup timeout')
            message=json.loads(process.stdout.readline())['params']
            self.assertEqual(message['environment'],'sandbox')
            self.assertEqual(message['read_only'],'1')
            self.assertEqual(message['destination'],'https://api.cert.tastyworks.com')
            for key in ('CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN'):
                expected=hashlib.sha256(env['TASTYTRADE_'+key].encode()).hexdigest()
                self.assertEqual(message['credentials'][key]['digest'],expected)
        finally:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            process.stdin.close();process.stdout.close()
