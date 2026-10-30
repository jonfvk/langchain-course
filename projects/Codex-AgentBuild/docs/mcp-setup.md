# Sandbox read-only MCP setup

Status: prepared, disabled, and not authenticated. No server has been launched
as part of this configuration step.

## Codex configuration

`config/codex-mcp.toml` is a staged Codex configuration template. Codex does not
automatically load this location. In a later authorized setup step, merge its
table into this project's `.codex/config.toml`, keeping `enabled = false` until
credentials and connection approval are ready. Project configuration requires a
trusted workspace. Never add secret values to either TOML file.

Codex's VS Code extension supports MCP configuration through its gear menu,
MCP servers, and through project configuration. CLI and IDE share the Codex
configuration format. See the [official MCP documentation](https://developers.openai.com/codex/mcp).
The extension is installed on this Mac; configuration loading and connection
have not been tested in this step. VS Code's other MCP clients may use different
configuration formats.

## Launch contract

The client launches `/bin/sh` with the project-relative script
`scripts/tastytrade-mcp-readonly.sh`, using the project directory as its
working directory. Replace `/path/to/Codex-AgentBuild` in the TOML template
with the local checkout path. The launcher replaces itself with the MCP
process, preserving stdin/stdout:

```sh
<project>/.tools/node-v24.21.0-darwin-x64/bin/node \
  <project>/.tools/tastytrade-mcp/dist/index.js
```

Use the launcher rather than invoking the entry point directly: it overwrites
inherited mode settings with `TASTYTRADE_ENV=sandbox` and
`TASTYTRADE_READ_ONLY=1`, and unsets `TASTYTRADE_API_URL`. The server uses these
settings to select sandbox and withhold/refuse write and destructive tools.
The launcher accepts no forwarded server arguments. These controls apply to
this launch path; they do not prevent someone from editing or bypassing it.

## Future credentials

The client must eventually receive these environment variables from a secure
local source, with values belonging only to a sandbox OAuth application:

- `TASTYTRADE_CLIENT_ID`: sandbox OAuth client ID.
- `TASTYTRADE_CLIENT_SECRET`: matching sandbox OAuth client secret.
- `TASTYTRADE_REFRESH_TOKEN`: matching sandbox refresh token.

The template forwards names only; no credential values or dummy credentials
are stored. Do not paste secrets into chat, tracked configuration, command
arguments, or shell history. Environment variables must reach the Codex/IDE
process itself; exporting them in an already-open integrated terminal does not
update its parent VS Code process. This stdio server uses environment-based
broker authentication, not Codex's MCP OAuth login flow. It does not auto-load
`.env`. Do not supply credentials or enable the server during this step.

## Risk and limitations

`MAX_ORDER_NOTIONAL_USD` is an optional upstream buying-power-impact ceiling
(upstream default: 50000). It is deliberately not configured here and is not a
permission to trade or a maximum-loss guarantee. Read-only mode remains enabled.
Any future trading design must retain a separate hard-coded Python risk gate
that the AI cannot override; this step does not implement that gate.

Sandbox market data is unavailable and trading state resets daily. No account,
market-data, dry-run, or order calls are authorized by this setup.

## Sandbox Keychain storage

Sandbox OAuth values are stored together under service
`com.codex-agentbuild.tastytrade.sandbox`, account `oauth-client-v1`.
The Python-only `src.security.sandbox_keychain.load_sandbox()` interface returns
`SandboxAuth`. This does not enable or launch MCP, export secrets into its
environment, or authenticate. Production storage is separate and has no fallback.
