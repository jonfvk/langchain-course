"""One bounded production MU stock stream. No options, orders, retries or logs."""
import json
import logging
import math
import ssl
import time
from datetime import datetime, timezone

import certifi
from websockets.sync.client import connect

from . import authentication as auth
from .keychain import Secret, load_production_market_data
from .quote_validation import inspect_quote

_FIELDS = {
    'Quote': ['eventType', 'eventSymbol', 'bidPrice', 'askPrice', 'bidSize', 'askSize', 'bidTime', 'askTime'],
    'Trade': ['eventType', 'eventSymbol', 'price', 'size', 'dayVolume', 'time'],
}
# Dedicated disconnected logger: WebSocket DEBUG logging must never emit AUTH.
_LOGGER = logging.Logger('mu-stream-private')
_LOGGER.disabled = True
_LOGGER.propagate = False
_DURATION = 14.0  # Reserve up to one additional second for WebSocket close.


def validate_symbol(symbol):
    if type(symbol) is not str or symbol != 'MU':
        raise ValueError('Only MU common stock is permitted.')


def subscription(symbol):
    validate_symbol(symbol)  # Must run before constructing subscription contents.
    return {'type': 'FEED_SUBSCRIPTION', 'channel': 3, 'reset': True,
            'add': [{'type': kind, 'symbol': symbol} for kind in ('Quote', 'Trade')]}


def _fresh_credentials(result):
    """Refresh on each invocation; return wrapped values only within this module."""
    stored = load_production_market_data()
    status, oauth, error = auth._exchange('POST', '/oauth/token', payload=auth._production_refresh_payload(stored))
    del stored
    result['oauth_http_status'] = status
    if error or not oauth or not auth._nonempty(oauth.get('access_token')):
        raise ValueError('OAuth failed')
    if str(oauth.get('token_type', '')).lower() != 'bearer':
        raise ValueError('OAuth response invalid')
    if 'scope' in oauth and (not isinstance(oauth['scope'], str) or set(oauth['scope'].split()) != {'read'}):
        raise ValueError('OAuth scope invalid')
    result['oauth_success'] = True
    status, quote, error = auth._exchange('GET', '/api-quote-tokens', bearer=oauth['access_token'])
    del oauth
    result['quote_http_status'] = status
    result['quote_diagnostics'] = inspect_quote(quote, status)
    if error:
        result['quote_diagnostics']['accepted'] = False
        result['quote_diagnostics']['rejection_rule'] = 'quote_http_or_json_transport_failure'
        raise ValueError('Quote credentials unavailable')
    if not result['quote_diagnostics']['accepted']:
        raise ValueError('Quote response validation rejected')
    data = quote['data']
    result['quote_token_success'] = True
    return Secret(data['dxlink-url']), Secret(data['token'])


def inspect_quote_once():
    """One production OAuth/quote sequence, diagnostics only; never open WebSocket."""
    result = dict(oauth_success=False, oauth_http_status=None, quote_http_status=None,
                  quote_token_success=False)
    credentials = None
    try:
        credentials = _fresh_credentials(result)
    except Exception:
        result['error'] = 'Sequence failed; see status and validation rule'
    finally:
        credentials = None
    return result


def _number(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def _timestamp(value):
    number = _number(value)
    if number is None or number == 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _consume(data, fields, result):
    """COMPACT consists of event-type/flattened-values pairs; use negotiated order."""
    if not isinstance(data, list) or len(data) % 2:
        raise ValueError('Invalid feed data')
    for i in range(0, len(data), 2):
        kind, values = data[i:i + 2]
        if kind not in _FIELDS or kind not in fields or not isinstance(values, list):
            raise ValueError('Unexpected feed event')
        columns = fields[kind]
        if not columns or len(values) % len(columns):
            raise ValueError('Incomplete compact row')
        for j in range(0, len(values), len(columns)):
            row = dict(zip(columns, values[j:j + len(columns)]))
            validate_symbol(row.get('eventSymbol'))
            if 'eventType' in row and row['eventType'] != kind:
                raise ValueError('Event type mismatch')
            if kind == 'Quote':
                for source, dest in [('bidPrice', 'bid'), ('askPrice', 'ask'), ('bidSize', 'bid_size'), ('askSize', 'ask_size')]:
                    result[dest] = _number(row.get(source))
                result['bid_timestamp'] = _timestamp(row.get('bidTime'))
                result['ask_timestamp'] = _timestamp(row.get('askTime'))
                result['quote_events'] += 1
            else:
                for source, dest in [('price', 'last'), ('size', 'last_size'), ('dayVolume', 'volume')]:
                    result[dest] = _number(row.get(source))
                result['trade_timestamp'] = _timestamp(row.get('time'))
                result['trade_events'] += 1
            result['received_at'] = datetime.now(timezone.utc).isoformat()
            result['subscription_success'] = True  # Confirmed by MU event delivery.


def _session(ws, token, symbol, result, deadline):
    def send(message):
        ws.send(json.dumps(message))
    send({'type': 'SETUP', 'channel': 0, 'version': '0.1-DXF-JS/0.3.0',
          'keepaliveTimeout': 60, 'acceptKeepaliveTimeout': 60})
    setup = authenticated = auth_sent = channel_requested = feed_requested = subscribed = False
    fields = {}
    next_keepalive = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = min(deadline - time.monotonic(), max(0.001, next_keepalive - time.monotonic()))
        if remaining <= 0:
            break
        try:
            message = json.loads(ws.recv(timeout=remaining))
        except TimeoutError:
            if time.monotonic() < deadline:
                send({'type': 'KEEPALIVE', 'channel': 0})
                next_keepalive = time.monotonic() + 5
            continue
        if not isinstance(message, dict):
            raise ValueError('Invalid protocol message')
        kind, channel = message.get('type'), message.get('channel')
        if kind == 'ERROR' or kind == 'CHANNEL_CLOSED':
            raise ValueError('Server rejected or closed stream')
        if kind == 'SETUP' and channel == 0:
            setup = True
        elif kind == 'AUTH_STATE' and channel == 0:
            if message.get('state') == 'UNAUTHORIZED':
                if not setup or auth_sent:
                    raise ValueError('DXLink authentication rejected')
                send({'type': 'AUTH', 'channel': 0, 'token': token._for_auth()})
                auth_sent = True
            elif message.get('state') == 'AUTHORIZED' and auth_sent:
                authenticated = True
                result['dxlink_auth_success'] = True
                if not channel_requested:
                    send({'type': 'CHANNEL_REQUEST', 'channel': 3, 'service': 'FEED', 'parameters': {'contract': 'AUTO'}})
                    channel_requested = True
        elif kind == 'CHANNEL_OPENED' and channel == 3 and authenticated:
            if not feed_requested:
                send({'type': 'FEED_SETUP', 'channel': 3, 'acceptAggregationPeriod': 0.1,
                      'acceptDataFormat': 'COMPACT', 'acceptEventFields': _FIELDS})
                feed_requested = True
                send(subscription(symbol))
                result['subscription_sent'] = True
                subscribed = True
        elif kind == 'FEED_CONFIG' and channel == 3 and feed_requested:
            # CHANNEL_OPENED may be followed by the channel's default config.
            # Wait for the requested COMPACT configuration before subscribing.
            if message.get('dataFormat') != 'COMPACT':
                continue
            configured = message.get('eventFields', {})
            for event in configured:
                if event not in _FIELDS:
                    continue
                cols = configured.get(event)
                if (not isinstance(cols, list) or not all(isinstance(c, str) for c in cols)
                        or 'eventSymbol' not in cols or len(cols) != len(set(cols))):
                    raise ValueError('Invalid negotiated schema')
                fields[event] = cols
            if not subscribed:
                send(subscription(symbol))
                result['subscription_sent'] = True
                subscribed = True
        elif kind == 'FEED_DATA' and channel == 3 and subscribed:
            _consume(message.get('data'), fields, result)
    if channel_requested:
        send({'type': 'CHANNEL_CANCEL', 'channel': 3})


def stream_mu_once(symbol='MU'):
    validate_symbol(symbol)  # Before Keychain, OAuth, socket, or subscription.
    result = dict(symbol='MU', oauth_success=False, oauth_http_status=None,
                  quote_token_success=False, quote_http_status=None,
                  dxlink_connection_success=False, dxlink_auth_success=False,
                  subscription_sent=False, subscription_success=False,
                  clean_disconnect=False, quote_events=0, trade_events=0,
                  bid=None, ask=None, last=None, bid_size=None, ask_size=None,
                  last_size=None, volume=None, bid_timestamp=None, ask_timestamp=None,
                  trade_timestamp=None, received_at=None, connected_seconds=None, error=None)
    ws = url = token = None
    started = None
    stage = 'production credentials'
    try:
        url, token = _fresh_credentials(result)
        stage = 'WebSocket connection'
        ws = connect(url._for_auth(), ssl=ssl.create_default_context(cafile=certifi.where()),
                     proxy=None, open_timeout=10, close_timeout=1, ping_interval=None,
                     max_size=1024 * 1024, compression=None, logger=_LOGGER,
                     user_agent_header='codex-agentbuild/0.1')
        result['dxlink_connection_success'] = True
        started = time.monotonic()
        stage = 'DXLink handshake or feed'
        _session(ws, token, symbol, result, started + _DURATION)
        if not result['subscription_success']:
            result['error'] = 'No MU events received within the connection window'
    except Exception:
        result['error'] = 'Failure during ' + stage + '; details withheld to protect tokens; no retry'
    finally:
        if ws is not None:
            try:
                ws.close(code=1000)
                result['clean_disconnect'] = ws.close_code in (1000, 1001)
            except Exception:
                result['clean_disconnect'] = False
        if started is not None:
            result['connected_seconds'] = round(time.monotonic() - started, 3)
        url = token = ws = None
    return result
