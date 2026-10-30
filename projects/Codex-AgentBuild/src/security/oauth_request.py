"""Shared OAuth wire construction. No I/O, arbitrary destinations, or logging."""
import json
from dataclasses import dataclass, field
from src.data.market_data.keychain import ProductionMarketDataAuth
from src.security.sandbox_keychain import SandboxAuth

_HOSTS = {'production': 'api.tastyworks.com', 'sandbox': 'api.cert.tastyworks.com'}

@dataclass(frozen=True)
class RefreshRequest:
    host: str
    method: str = 'POST'
    path: str = '/oauth/token'
    headers: dict = field(default_factory=dict, repr=False)
    body: str = field(default='', repr=False)


def _serialize_refresh(environment, payload):
    if environment not in _HOSTS:
        raise ValueError('Explicit OAuth environment required')
    if not isinstance(payload, dict) or set(payload) != {'grant_type','refresh_token','client_secret'}:
        raise ValueError('Exactly three refresh fields required')
    if payload['grant_type'] != 'refresh_token' or any(
            not isinstance(payload[k],str) or not payload[k].strip()
            for k in ('refresh_token','client_secret')):
        raise ValueError('Invalid refresh field types')
    # Preserve production JSON serialization and header insertion order.
    ordered = {key:payload[key] for key in ('grant_type','refresh_token','client_secret')}
    return RefreshRequest(_HOSTS[environment], headers={
        'User-Agent':'codex-agentbuild/0.1', 'Accept':'application/json',
        'Content-Type':'application/json'}, body=json.dumps(ordered))


def build_refresh_request(environment, credentials):
    expected = {'production':ProductionMarketDataAuth,'sandbox':SandboxAuth}.get(environment)
    if expected is None or type(credentials) is not expected:
        raise ValueError('OAuth credential/environment mismatch')
    return _serialize_refresh(environment, {
        'grant_type':'refresh_token',
        'refresh_token':credentials.refresh_token._for_auth(),
        'client_secret':credentials.client_secret._for_auth()})
