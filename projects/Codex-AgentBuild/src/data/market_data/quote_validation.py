"""Quote response structure diagnostics. Never return token values or full URLs."""
from datetime import datetime
import re
from urllib.parse import urlsplit, unquote
from . import authentication as auth


def inspect_quote(quote, status):
    def kind(value):
        return ('object' if isinstance(value, dict) else 'list' if isinstance(value, list)
                else 'string' if isinstance(value, str) else 'null' if value is None
                else 'boolean' if isinstance(value, bool) else 'number')
    result = dict(http_status=status, root_type=kind(quote), root_keys=[], nested_keys={},
                  url_nonempty=False, url_scheme=None, url_hostname=None, url_path=None,
                  query_parameters_exist=False, token_nonempty=False, token_length=0,
                  expires_at=None, issued_at=None, accepted=False, rejection_rule=None)
    # Protect even pathological keys/paths containing a returned credential.
    secrets = {}
    def gather(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in auth._CREDENTIAL_KEYS and isinstance(item, str) and item:
                    secrets[str(len(secrets))] = item
                gather(item)
        elif isinstance(value, list):
            for item in value:
                gather(item)
    gather(quote)
    def safe(value):
        return auth._safe_error_text(value, secrets)
    def safe_component(value):
        # Domain names contain dots but are not JWTs. Redact exact returned
        # secrets and long opaque components without hiding normal DNS labels.
        for secret in secrets.values():
            value = value.replace(secret, '<REDACTED>')
        value = re.sub(r'[A-Za-z0-9_+=-]{32,}', '<REDACTED>', value)
        return ''.join(c if c.isprintable() else ' ' for c in value)[:500]
    def keys(value, path=''):
        if isinstance(value, dict):
            result['nested_keys'][path or '$'] = [safe(k) for k in value]
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    keys(item, path + '/' + safe(key))
        elif isinstance(value, list):
            for i, item in enumerate(value[:20]):
                keys(item, path + '/' + str(i))
    keys(quote)
    if isinstance(quote, dict):
        result['root_keys'] = [safe(k) for k in quote]
    def reject(rule):
        result['rejection_rule'] = rule
        return result
    if not isinstance(quote, dict):
        return reject('root_must_be_object')
    data = quote.get('data')
    if not isinstance(data, dict):
        return reject('data_must_be_object')
    url, token = data.get('dxlink-url'), data.get('token')
    result['url_nonempty'] = auth._nonempty(url)
    result['token_nonempty'] = auth._nonempty(token)
    result['token_length'] = len(token) if isinstance(token, str) else 0
    for source, target in (('expires-at','expires_at'),('issued-at','issued_at')):
        value = data.get(source)
        if isinstance(value, str) and len(value) <= 40:
            try:
                result[target] = datetime.fromisoformat(value.replace('Z','+00:00')).isoformat()
            except ValueError:
                pass
    result['lifetime_seconds'] = auth._lifetime(data)
    if not result['url_nonempty']:
        return reject('data.dxlink-url_must_be_nonempty_string')
    try:
        if not url.startswith('wss://') or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url:
            return reject('malformed_or_non_wss_url')
        parts = urlsplit(url)
        result.update(url_scheme=safe(parts.scheme), url_hostname=safe_component(parts.hostname or ''),
                      url_path=safe_component(unquote(parts.path)), query_parameters_exist=bool(parts.query))
        if parts.scheme != 'wss':
            return reject('scheme_must_be_wss')
        if parts.username is not None or parts.password is not None:
            return reject('embedded_userinfo_forbidden')
        host = parts.hostname or ''
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*', host):
            return reject('invalid_dns_hostname')
        if not (host == 'dxfeed.com' or host.endswith('.dxfeed.com')):
            return reject('hostname_must_be_dxfeed_domain')
        if parts.port not in (None,443):
            return reject('port_must_be_default_or_443')
        if parts.fragment:
            return reject('fragment_forbidden')
    except ValueError:
        return reject('url_parse_or_port_invalid')
    if not result['token_nonempty']:
        return reject('data.token_must_be_nonempty_string')
    result['accepted'] = True
    return result
