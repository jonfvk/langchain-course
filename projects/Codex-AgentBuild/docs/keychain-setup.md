# Production market-data Keychain setup

Run from your own macOS Terminal (not Codex chat), in the project directory:

```sh
.venv/bin/python scripts/setup-production-keychain.py
```

Enter Production Client ID, Production Client Secret, then Production
Grant/Refresh Token. All three prompts disable echo. Do not pass values as
arguments, environment variables, shell commands, or files. The script refuses
noninteractive input and aborts if hidden input is unavailable. A macOS Keychain
access prompt may appear. No value is displayed after entry. Run again to replace
the complete bundle. Cancellation before storage leaves the previous item alone.
Storage failures report no success; rerun setup with all three values.

The dependency is `keyring>=25,<26` in the project virtual environment. If creating
a fresh environment, install it with `.venv/bin/python -m pip install 'keyring>=25,<26'`.
The explicit macOS backend uses native Security APIs, not a subprocess with secrets
in arguments. There is no backend discovery, plaintext fallback, or `.env` support.

## Keychain identity

- Service: `com.codex-agentbuild.tastytrade.production.market-data`
- Account: `oauth-client-v1`
- Contents: an environment-tagged JSON bundle with a `credentials` object holding
  `client_id`, `client_secret`, and `refresh_token`, stored only in Keychain.
- Separate sandbox service: `com.codex-agentbuild.tastytrade.sandbox`.
  Account: `oauth-client-v1`. Its bundle carries `environment=sandbox`.

## Runtime interface and boundaries

Only the production market-data/authentication component may use:

```python
from src.data.market_data.keychain import load_production_market_data

auth = load_production_market_data()
```

The result has `client_id`, `client_secret`, and `refresh_token` wrapped as `Secret`.
Normal `str`, `repr`, formatted output, and logging of these objects redact values.
Pickle serialization is refused. Backend exceptions are replaced with fixed safe
messages, without chained backend tracebacks. The private `_for_auth()` method is
reserved for constructing authentication requests within the market-data boundary;
never log its plaintext result, HTTP bodies/headers, or debugger local variables.
Redaction does not make deliberately extracted plaintext safe to log. Python
cannot guarantee memory zeroization or stop arbitrary code running as the same
macOS user from requesting Keychain access. Use separate OS identities/process
permissions if an adversarial broker isolation boundary is needed.

No production secrets are exported into environment variables, shared configuration,
MCP, or `src/broker`. The sandbox launcher stays separate and disabled in the staged
configuration. Future sandbox setup must use its own service and its own credentials;
never supply production values to that launcher. Storage does not validate server
scopes or confer read-only permissions; use a production grant with only `read`
permission. The verified refresh payload omits optional scope/client_id fields.

## Verified OAuth terminology and next step

Verified against official tastytrade documentation on 2026-09-16:
[OAuth2 guide](https://developer.tastytrade.com/docs/authentication/oauth2/),
Step 2, says Create Grant displays the grant's refresh token. That displayed token
is the `refresh_token` in Step 3, not a grant name or identifier.

The restricted authentication transport is `src/data/market_data/authentication.py`.
It uses the verified three-field OAuth payload (grant_type, refresh_token,
client_secret), never requests trade scope, and rejects explicitly broader
returned scopes. The only added production REST read is the hard-coded MU option
chain endpoint with bounded pagination. Tokens remain internal and in memory.

The normalized `MUMarketData` interface in `src/data/market_data/mu_data.py` now
supports bounded MU underlying and catalog-validated MU option streams. See
[MU market data](mu-stream.md) for endpoint limits, field provenance, token refresh,
stale-data semantics and live usage. Neither the authentication transport nor the
market-data interface exposes production account or order operations.

Protocol sources: [OAuth](https://developer.tastytrade.com/docs/authentication/oauth2/)
and [quote-token guide](https://developer.tastytrade.com/docs/guides/stream-market-data/),
checked 2026-09-16. Fake-secret tests make no network or real Keychain calls:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Environment reclassification

The original untagged bundle was identified by the owner as sandbox credentials.
`src/security/sandbox_keychain.py` provides `SandboxAuth`, `load_sandbox()`, and
an explicit maintenance migration. Migration reads the original bundle directly
from Keychain, writes an environment-tagged sandbox bundle, verifies all three
values, then deletes the mislabeled production entry. Conflicting sandbox items
or failed verification prevent source deletion. No secrets enter files or output.

Production uses `ProductionMarketDataAuth` and `load_production_market_data()`;
it accepts only a `production`-tagged bundle in the production service. It never
falls back to sandbox or accepts legacy untagged entries. Production storage
rejects a `SandboxAuth` object. Production must be configured separately using
the hidden-input setup script with actual production application/grant values.
Environment tags record declared provenance; they cannot independently validate
which environment issued an opaque credential. No authentication is performed
by credential migration or loading.
