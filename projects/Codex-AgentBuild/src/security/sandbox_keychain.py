"""Sandbox-only types and loader. No production fallback or authentication."""
import json
from dataclasses import dataclass
from src.data.market_data import keychain as storage

SERVICE = 'com.codex-agentbuild.tastytrade.sandbox'
ACCOUNT = storage.ACCOUNT


@dataclass(frozen=True, slots=True)
class SandboxAuth:
    client_id: storage.Secret
    client_secret: storage.Secret
    refresh_token: storage.Secret


def _decode(payload):
    bundle = json.loads(payload)
    if bundle.get('environment') != 'sandbox':
        raise ValueError
    values = bundle['credentials']
    if set(values) != {'client_id', 'client_secret', 'refresh_token'}:
        raise ValueError
    return SandboxAuth(**{k: storage.Secret(v) for k, v in values.items()})


def load_sandbox() -> SandboxAuth:
    try:
        return _decode(storage._backend().get_password(SERVICE, ACCOUNT))
    except Exception:
        pass
    raise storage.KeychainError('Sandbox credentials NOT CONFIGURED or inaccessible.')


def migrate_mislabeled_sandbox() -> dict:
    """User-authorized reclassification of the legacy untagged bundle only.

    Refuse conflicting targets. Read back and compare all values before deleting
    the legacy source. Never accept a tagged production bundle for migration.
    """
    try:
        backend = storage._backend()
        source = backend.get_password(storage.SERVICE, ACCOUNT)
        if source is None:
            _decode(backend.get_password(SERVICE, ACCOUNT))
            return dict(migration_succeeded=True, production_entry_removed=True,
                        production_not_configured=True)
        values = json.loads(source)
        if set(values) != {'client_id', 'client_secret', 'refresh_token'}:
            raise ValueError
        auth = SandboxAuth(**{k: storage.Secret(v) for k, v in values.items()})
        target = json.dumps({'environment': 'sandbox', 'credentials': values})
        existing = backend.get_password(SERVICE, ACCOUNT)
        if existing is not None and json.loads(existing) != json.loads(target):
            raise ValueError
        if existing is None:
            backend.set_password(SERVICE, ACCOUNT, target)
        verified = _decode(backend.get_password(SERVICE, ACCOUNT))
        if any(getattr(verified, k)._for_auth() != getattr(auth, k)._for_auth() for k in values):
            raise ValueError
        # Refuse deleting a source changed during verification.
        if backend.get_password(storage.SERVICE, ACCOUNT) != source:
            raise ValueError
        backend.delete_password(storage.SERVICE, ACCOUNT)
        if backend.get_password(storage.SERVICE, ACCOUNT) is not None:
            raise ValueError
        return dict(migration_succeeded=True, production_entry_removed=True,
                    production_not_configured=True)
    except Exception:
        pass
    raise storage.KeychainError('Migration not confirmed; source deletion requires a verified matching sandbox copy.')
