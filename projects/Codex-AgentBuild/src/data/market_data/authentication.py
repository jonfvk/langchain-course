"""Restricted MU market-data readiness check; never returns bearer capabilities.

Only OAuth and quote-token acquisition are implemented. Tokens are not symbol
scoped by tastytrade. This interface accepts no symbols and fetches no quotes;
future subscriptions must separately enforce MU/MU-option underlying identity.
"""
from dataclasses import dataclass
from datetime import datetime
import http.client
import json
import ssl
import socket
import re
import certifi

from .keychain import load_production_market_data
from src.security.oauth_request import _serialize_refresh, build_refresh_request

__all__ = ['verify_mu_market_data_access', 'Verification', 'verify_mu_oauth']
_HOST = 'api.tastyworks.com'
_ALLOWED = frozenset({('POST', '/oauth/token'), ('GET', '/api-quote-tokens')})
_LIMIT = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Verification:
    oauth_succeeded: bool = False
    quote_tokens_succeeded: bool = False
    dxlink_url_returned: bool = False
    dxlink_token_nonempty: bool = False
    oauth_http_status: int | None = None
    quote_http_status: int | None = None
    oauth_lifetime_seconds: int | float | None = None
    quote_lifetime_seconds: int | float | None = None
    quote_expires_at: str | None = None
    error: str | None = None
    access_token_nonempty: bool = False
    oauth_diagnostics: dict | None = None


def _production_refresh_payload(credentials):
    return json.loads(build_refresh_request('production', credentials).body)


def _exchange(method, path, *, payload=None, bearer=None, diagnostics=None):
    """Private fixed-origin transport: TLS, no proxies, redirects, retries or logs."""
    chain_read = method == 'GET' and re.fullmatch(r'/option-chains/MU\?per-page=250&page-offset=(?:0|[1-9][0-9]?)', path) is not None
    if (method, path) not in _ALLOWED and not chain_read:
        return None, None, 'Operation not permitted'
    response_limit = 16 * 1024 * 1024 if chain_read else _LIMIT
    connection = None
    status = None
    try:
        if path == '/oauth/token' and bearer is not None:
            return None, None, 'Authorization forbidden on OAuth refresh'
        connection = http.client.HTTPSConnection(_HOST, timeout=30, context=ssl.create_default_context(cafile=certifi.where()))
        connection.set_debuglevel(0)
        headers = {'User-Agent': 'codex-agentbuild/0.1', 'Accept': 'application/json'}
        body = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload)
        if path == '/oauth/token':
            refresh = _serialize_refresh('production', payload)
            headers, body = refresh.headers, refresh.body
        if bearer is not None:
            headers['Authorization'] = 'Bearer ' + bearer
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        if not 200 <= status < 300:
            if path == '/oauth/token' and 400 <= status < 600:
                raw_error = response.read(_LIMIT + 1)
                info = _parse_error_response(raw_error, response.getheader('Content-Type'), payload)
                if diagnostics is not None:
                    diagnostics.update(info)
                return status, None, 'OAuth error code=' + str(info['error_code']) + '; message=' + str(info['error_message'])
            return status, None, 'HTTP request rejected (redirects are not followed)'
        raw = response.read(response_limit + 1)
        if diagnostics is not None:
            mime = response.getheader('Content-Type')
            mime = mime.split(';', 1)[0].strip().lower() if isinstance(mime, str) else ''
            diagnostics.update(content_type=mime if mime in ('application/json', 'text/html', 'text/plain') else 'other or missing', response_length=len(raw))
        if len(raw) > response_limit:
            return status, None, 'Response exceeded size limit'
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return status, None, 'Invalid JSON response schema'
        return status, parsed, None
    except Exception as exc:
        return status, None, _exception_diagnostic(exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


# Values are exposed only for diagnostic fields, never arbitrary server fields.
_VALUE_KEYS = frozenset({'error', 'code', 'message', 'detail', 'status', 'reason', 'errors', 'error_description'})
_CREDENTIAL_KEYS = frozenset({'client_id', 'client_secret', 'refresh_token', 'access_token', 'id_token', 'authorization', 'token', 'secret', 'password'})


def _safe_error_text(value, payload):
    if not isinstance(value, str):
        return '[not supplied]'
    # Remove known credentials before truncation, including escaped/URL forms.
    from urllib.parse import quote
    secrets = [v for k, v in (payload or {}).items() if k != 'grant_type' and isinstance(v, str) and v]
    for secret in sorted(secrets, key=len, reverse=True):
        for form in {secret, json.dumps(secret)[1:-1], quote(secret, safe='')}:
            value = value.replace(form, '<REDACTED>')
    value = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,;]+', '<REDACTED>', value)
    value = re.sub(r'(?i)https?://[^\s]+', '<REDACTED>', value)
    value = re.sub(r'(?i)(["\']?(?:client_secret|refresh_token|access_token|authorization|password)["\']?\s*[:=]\s*)["\']?[^\s,;}"\']+["\']?', r'\1<REDACTED>', value)
    # JWTs, long opaque strings, and short mixed alphanumeric token examples.
    value = re.sub(r'\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '<REDACTED>', value)
    value = re.sub(r'(?<![\w])[A-Za-z0-9_+/=-]{24,}(?:\.\.\.)?', '<REDACTED>', value)
    value = re.sub(r'\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{6,}(?:\.\.\.)?', '<REDACTED>', value)
    return ''.join(c if c.isprintable() else ' ' for c in value)[:1000]


def _json_structure(value, payload, field=None, depth=0):
    kind = ('null' if value is None else 'boolean' if isinstance(value, bool) else
            'object' if isinstance(value, dict) else 'list' if isinstance(value, list) else
            'string' if isinstance(value, str) else 'number')
    result = {'type': kind}
    if depth >= 4:
        return result
    if isinstance(value, dict):
        result['fields'] = [
            {'key': _safe_error_text(key, payload),
             **_json_structure(item, payload, key, depth + 1)}
            for key, item in list(value.items())[:20]]
        result['field_count'] = len(value)
    elif isinstance(value, list):
        result['length'] = len(value)
        result['items'] = [_json_structure(item, payload, field, depth + 1) for item in value[:10]]
    elif field in _VALUE_KEYS:
        if isinstance(value, str):
            result['sanitized_value'] = _safe_error_text(value, payload)
        elif field == 'status' and type(value) is int and 100 <= value <= 599:
            result['sanitized_value'] = value
    return result


def _parse_error_response(raw, content_type, payload):
    # Only known MIME types are reported; arbitrary header parameters are omitted.
    mime = content_type.split(';', 1)[0].strip().lower() if isinstance(content_type, str) else ''
    safe_mime = mime if mime in {'application/json', 'application/problem+json', 'text/html', 'text/plain'} else 'other or missing'
    result = dict(error_code=None, error_message=None, content_type=safe_mime,
                  json_error_envelope_parsed=False, response_length=len(raw),
                  response_length_is_lower_bound=len(raw) > _LIMIT,
                  response_characterization='oversized response' if len(raw) > _LIMIT else 'non-JSON response')
    if len(raw) > _LIMIT:
        return result
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeError):
        return result
    # Gather credential-labelled response values so echoed tokens are also removed
    # from diagnostic fields, without returning any such values.
    redactions = dict(payload or {})
    def collect(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key.lower() in _CREDENTIAL_KEYS and isinstance(item, str):
                    redactions['response_' + str(len(redactions))] = item
                collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)
    collect(parsed)
    payload = redactions
    if isinstance(parsed, dict):
        result['root_keys'] = [_safe_error_text(k, payload) for k in parsed]
    result['json_structure'] = _json_structure(parsed, payload)
    result['response_characterization'] = 'JSON without recognized error envelope'
    if not isinstance(parsed, dict):
        return result
    err = parsed.get('error')
    if isinstance(err, dict):
        code, message = err.get('code'), err.get('message')
    elif isinstance(err, str):
        code, message = err, parsed.get('error_description')
    else:
        return result
    result['json_error_envelope_parsed'] = True
    result['response_characterization'] = 'JSON error envelope'
    result['error_code'] = _safe_error_text(code, payload)
    result['error_message'] = _safe_error_text(message, payload)
    return result


def diagnose_minimal_oauth() -> dict:
    """One required-fields-only diagnostic exchange; discard token, never use it.

    Omitting scope may return the grant's full scope. This diagnostic cannot
    pass its token to market-data or any other endpoint.
    """
    result = dict(oauth_succeeded=False, access_token_nonempty=False,
                  http_status=None, expires_in=None, error_code=None,
                  error_message=None, content_type=None,
                  json_error_envelope_parsed=False)
    try:
        auth = load_production_market_data()
    except Exception:
        result['error_message'] = 'Keychain unavailable; no request made'
        return result
    payload = _production_refresh_payload(auth)
    status, data, error = _exchange('POST', '/oauth/token', payload=payload, diagnostics=result)
    result['http_status'] = status
    if error:
        if result['error_message'] is None:
            result['error_message'] = error
        return result
    result['access_token_nonempty'] = _nonempty(data.get('access_token'))
    result['oauth_succeeded'] = result['access_token_nonempty']
    result['expires_in'] = _lifetime(data)
    if not result['oauth_succeeded']:
        result['error_message'] = 'Successful HTTP response missing nonempty access token'
    return result


def _exception_diagnostic(exc):
    # Use known exception classes and safe attributes, never arbitrary str(exc).
    if isinstance(exc, ssl.SSLCertVerificationError):
        reason = {20: 'unable to get local issuer certificate', 10: 'certificate has expired',
                  62: 'hostname mismatch'}.get(exc.verify_code, 'certificate verification failed')
        return f'SSLCertVerificationError: {reason}; verify_code={exc.verify_code}'
    if isinstance(exc, socket.gaierror):
        return f'gaierror: DNS resolution failed; errno={exc.errno}'
    if isinstance(exc, TimeoutError):
        return 'TimeoutError: connection or response timed out'
    if isinstance(exc, ssl.SSLError):
        return 'SSLError: TLS handshake or protocol failure'
    if isinstance(exc, json.JSONDecodeError):
        return f'JSONDecodeError: invalid JSON; line={exc.lineno}, column={exc.colno}'
    if isinstance(exc, OSError):
        return f'OSError: network or local I/O failure; errno={exc.errno}'
    return 'Unexpected exception: details withheld to protect credentials'


def verify_mu_oauth() -> Verification:
    """Exactly one OAuth attempt; never request a quote token."""
    return verify_mu_market_data_access(_oauth_only=True)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _lifetime(data):
    value = data.get('expires_in', data.get('expires-in'))
    # Bound values to avoid emitting arbitrary server strings or nonfinite numbers.
    return value if type(value) in (int, float) and 0 <= value <= 315360000 else None


def _expiry(data):
    value = data.get('expires-at', data.get('expires_at', data.get('expiration')))
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()
    except ValueError:
        return None


def verify_mu_market_data_access(*, _oauth_only=False) -> Verification:
    """One OAuth attempt, then one quote-token attempt; sanitized metadata only.

    Loads solely through Keychain. No automatic refresh/retry, streaming, sandbox,
    orders, arbitrary URLs, symbol selection, or token-bearing public result.
    """
    try:
        auth = load_production_market_data()
    except Exception:
        return Verification(error='Keychain credentials unavailable; no HTTP requests made')
    diagnostics = {}
    status, oauth, error = _exchange('POST', '/oauth/token', payload=_production_refresh_payload(auth), diagnostics=diagnostics)
    del auth
    if error:
        return Verification(oauth_http_status=status, error=error, oauth_diagnostics=diagnostics)
    if not _nonempty(oauth.get('access_token')) or str(oauth.get('token_type', '')).lower() != 'bearer':
        return Verification(oauth_http_status=status, error='Invalid OAuth token response')
    # The configured grant is read-only; refuse explicitly broader returned scopes.
    if 'scope' in oauth and (not isinstance(oauth['scope'], str) or set(oauth['scope'].split()) != {'read'}):
        return Verification(oauth_http_status=status, error='OAuth returned an unexpected scope; token not used')
    lifetime = _lifetime(oauth)
    if _oauth_only:
        return Verification(oauth_succeeded=True, oauth_http_status=status, oauth_lifetime_seconds=lifetime, access_token_nonempty=True, oauth_diagnostics=diagnostics)
    quote_status, quote, error = _exchange('GET', '/api-quote-tokens', bearer=oauth['access_token'])
    del oauth
    common = dict(oauth_succeeded=True, oauth_http_status=status,
                  quote_http_status=quote_status, oauth_lifetime_seconds=lifetime)
    if error:
        return Verification(**common, error=error)
    data = quote.get('data')
    if not isinstance(data, dict):
        return Verification(**common, error='Invalid quote-token response schema')
    url_present = _nonempty(data.get('dxlink-url'))
    token_present = _nonempty(data.get('token'))
    return Verification(**common, quote_tokens_succeeded=url_present and token_present,
                        dxlink_url_returned=url_present, dxlink_token_nonempty=token_present,
                        quote_lifetime_seconds=_lifetime(data), quote_expires_at=_expiry(data),
                        error=None if url_present and token_present else 'Quote response missing URL or token')
