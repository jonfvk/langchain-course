import json
import unittest
from unittest.mock import patch
from src.data.market_data import keychain as p
from src.data.market_data import authentication as a
from src.security import sandbox_keychain as s


class MemoryKeychain:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.corrupt = False
    def get_password(self, service, account):
        if self.corrupt and service == s.SERVICE:
            return '{}'
        return self.items.get((service, account))
    def set_password(self, service, account, value):
        self.items[service, account] = value
    def delete_password(self, service, account):
        self.deleted.append(service)
        del self.items[service, account]


class SeparationTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryKeychain()
        self.values = dict(client_id='fake-id', client_secret='fake-secret', refresh_token='fake-refresh')
        self.backend.items[p.SERVICE, p.ACCOUNT] = json.dumps(self.values)
        patcher = patch.object(p, '_backend', return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_migration_and_idempotency(self):
        self.assertTrue(s.migrate_mislabeled_sandbox()['migration_succeeded'])
        self.assertIsInstance(s.load_sandbox(), s.SandboxAuth)
        self.assertNotIn((p.SERVICE,p.ACCOUNT), self.backend.items)
        self.assertEqual(self.backend.deleted, [p.SERVICE])
        self.assertTrue(s.migrate_mislabeled_sandbox()['production_not_configured'])

    def test_production_clients_fail_closed_with_only_sandbox(self):
        s.migrate_mislabeled_sandbox()
        with patch.object(a.http.client, 'HTTPSConnection') as network:
            self.assertFalse(a.verify_mu_oauth().oauth_succeeded)
            self.assertFalse(a.diagnose_minimal_oauth()['oauth_succeeded'])
            self.assertFalse(a.verify_mu_market_data_access().oauth_succeeded)
            network.assert_not_called()
        with self.assertRaises(p.KeychainError):
            p.load_production_market_data()
        with self.assertRaises(p.KeychainError):
            p.store_production_market_data(s.load_sandbox())

    def test_sandbox_tag_in_production_namespace_rejected(self):
        self.backend.items[p.SERVICE,p.ACCOUNT] = json.dumps({'environment':'sandbox','credentials':self.values})
        with self.assertRaises(p.KeychainError):
            p.load_production_market_data()

    def test_untagged_production_rejected(self):
        with self.assertRaises(p.KeychainError):
            p.load_production_market_data()

    def test_conflict_preserves_source(self):
        self.backend.items[s.SERVICE,s.ACCOUNT] = '{}'
        with self.assertRaises(p.KeychainError):
            s.migrate_mislabeled_sandbox()
        self.assertEqual(self.backend.deleted, [])

    def test_verification_failure_preserves_source(self):
        original = self.backend.set_password
        def corrupt(*args):
            original(*args)
            self.backend.corrupt = True
        with patch.object(self.backend, 'set_password', side_effect=corrupt):
            with self.assertRaises(p.KeychainError):
                s.migrate_mislabeled_sandbox()
        self.assertEqual(self.backend.deleted, [])

    def test_tagged_production_never_migrated(self):
        p.store_production_market_data(p.ProductionMarketDataAuth(*(p.Secret(v) for v in self.values.values())))
        with self.assertRaises(p.KeychainError):
            s.migrate_mislabeled_sandbox()
        self.assertEqual(self.backend.deleted, [])
