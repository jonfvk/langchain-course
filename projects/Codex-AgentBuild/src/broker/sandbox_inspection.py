"""Private, read-only stdio MCP preflight. No production credentials or writes."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from src.security.sandbox_keychain import load_sandbox, SandboxAuth

ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / '.tools/node-v24.21.0-darwin-x64/bin/node'
SERVER = ROOT / '.tools/tastytrade-mcp/dist/index.js'
READS = frozenset({'tastytrade_get_accounts', 'tastytrade_get_balances',
                  'tastytrade_get_positions', 'tastytrade_get_live_orders'})


class PreflightError(RuntimeError):
    """Contains only locally authored diagnostic codes, never upstream prose."""
    def __init__(self, message, retry_after_seconds=0):
        super().__init__(message)
        self.retry_after_seconds=retry_after_seconds


def child_environment(environment, auth):
    if environment != 'sandbox':
        raise PreflightError('SANDBOX_ENVIRONMENT_REQUIRED')
    if type(auth) is not SandboxAuth:
        raise PreflightError('SANDBOX_CREDENTIAL_TYPE_REQUIRED')
    # Explicit allowlist: no inherited tokens, API override, proxies, Node preload
    # hooks, TLS bypass, or production environment variables.
    result = {'PATH': '/usr/bin:/bin', 'TASTYTRADE_ENV': 'sandbox',
              'TASTYTRADE_READ_ONLY': '1', 'TASTYTRADE_HTTP_TIMEOUT_MS': '15000'}
    for key in ('HOME', 'TMPDIR'):
        if key in os.environ:
            result[key] = os.environ[key]
    for field in ('client_id', 'client_secret', 'refresh_token'):
        result['TASTYTRADE_' + field.upper()] = getattr(auth, field)._for_auth()
    return result


def verify_instructions(result, read_only=True):
    text = result.get('instructions', '')
    if not isinstance(text, str) or not text.startswith(
            'SANDBOX (https://api.cert.tastyworks.com).'):
        raise PreflightError('MCP_SANDBOX_NOT_PROVEN')
    if read_only and 'Read-only mode is ON' not in text:
        raise PreflightError('MCP_READ_ONLY_NOT_PROVEN')


class SandboxInspection:
    def __init__(self, environment):
        if environment != 'sandbox':
            raise PreflightError('SANDBOX_ENVIRONMENT_REQUIRED')
        self._read_only = True
        self.environment = environment
        self.process = None
        self.serial = 0
        self.buffer = b''
        self.sandbox_verified = False
        self.audit = []

    def __enter__(self):
        auth = load_sandbox()
        env = child_environment(self.environment, auth)
        if not self._read_only: env['TASTYTRADE_READ_ONLY'] = '0'
        try:
            self.process = subprocess.Popen([str(NODE), str(ROOT / 'scripts/sandbox-mcp-audit.mjs')], cwd=ROOT,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        finally:
            env.clear()
            del auth
        try:
            response = self._rpc('initialize', {
                'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'codex-agentbuild-sandbox-preflight', 'version': '0.1'}})
            verify_instructions(response, self._read_only)
            self.sandbox_verified = True
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        self.sandbox_verified = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
            self.process.stdin.close(); self.process.stdout.close()
            self.process = None
        self.buffer = b''

    def _send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())

    def _rpc(self, method, params):
        self.serial += 1
        request_id = self.serial
        self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + 25
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if b'\n' not in self.buffer:
                    if not selector.select(max(0, deadline-time.monotonic())):
                        break
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk: raise PreflightError('MCP_PROCESS_CLOSED')
                    self.buffer += chunk
                    if len(self.buffer) > 4*1024*1024:
                        raise PreflightError('MCP_RESPONSE_TOO_LARGE')
                    continue
                line, self.buffer = self.buffer.split(b'\n', 1)
                try: response = json.loads(line)
                except (ValueError, UnicodeError):
                    raise PreflightError('MCP_INVALID_JSON') from None
                if response.get('method') == 'notifications/sandboxAudit':
                    self.audit.append(response.get('params', {}))
                    continue
                if response.get('id') != request_id:
                    continue
                if 'error' in response:
                    raise PreflightError('MCP_PROTOCOL_ERROR')
                return response['result']
        raise PreflightError('MCP_TIMEOUT_NO_RETRY')

    def read(self, name, arguments=None):
        if not self.sandbox_verified or name not in READS:
            raise PreflightError('MCP_READ_NOT_ALLOWED')
        return self._call(name, arguments)

    def _call(self, name, arguments=None):
        result = self._rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        payload = result.get('structuredContent')
        if payload is None:
            try:
                payload = json.loads(next(x['text'] for x in result.get('content', [])
                                          if x.get('type') == 'text'))
            except (ValueError, KeyError, StopIteration):
                raise PreflightError('MCP_UNRECOGNIZED_RESULT') from None
        if result.get('isError'):
            code = payload.get('code') if isinstance(payload, dict) else None
            # Never relay arbitrary body/message/error values.
            safe = code if code in {'auth_failed','network','upstream_error','validation',
                                    'rate_limit_exceeded','not_found'} else 'unrecognized_error'
            upstream = payload.get('upstream', {}) if isinstance(payload, dict) else {}
            status = upstream.get('status') if isinstance(upstream, dict) else None
            status = status if type(status) is int and 100 <= status <= 599 else 'unknown'
            delay=payload.get('retry_after_ms',0) if isinstance(payload,dict) else 0
            delay=delay/1000 if type(delay) in (int,float) and 0<=delay<=86400000 else 0
            raise PreflightError(f'MCP_{safe}; HTTP_STATUS={status}',delay)
        return payload
