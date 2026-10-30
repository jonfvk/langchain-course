# Sandbox Options Agent

## Intended architecture

- **OpenAI model (`src/agent/`)**: decision-making and strategy layer that
  suggests trades.
- **Python application**: infrastructure and orchestration (`src/agent/`),
  hard risk controls (`src/risk/`), data and state (`src/data/`), journaling
  (`src/journal/`), and configuration (`src/config/`).
- **tastytrade/MCP (`src/broker/`)**: brokerage and tool execution layer.
  The installed official MCP is confined to the sandbox API by a transport guard.

## Execution boundaries

- Execution is sandbox/paper only. Production is read-only MU market data.
- The minimum risk gate is implemented in Python, separate from strategy decisions.
  Every proposed order must pass this gate before broker submission. The model
  cannot override environment, bounded-risk, freshness, ownership or capital controls.
- No naked or unlimited-risk positions are permitted.
- Only broker-confirmed fills may be recorded as executions or used to update
  position state; a model suggestion or submitted order is not a fill.

The bounded paper runner supports long MU options, 70% deployment/30% reserve
controls, a SQLite journal, read-only authentication recovery, and broker fill
reconciliation. See [paper-session operation](docs/paper-session.md). Advanced
portfolio construction and a separate model-driven strategist remain future work.

## Production market-data credentials

Local macOS Keychain setup is available for production read-only market data,
separate from the sandbox broker. See [secure local setup](docs/keychain-setup.md).
Production market data supports bounded MU stock and instrument-discovered MU
option snapshots. Tokens remain internal and in memory. No production order
operations are implemented.

The normalized MU underlying and MU-options interface is documented in
[MU market data](docs/mu-stream.md). Production live verification is bounded and cannot access accounts or orders.
Sandbox execution runs separately; no background daemon is installed.

## Repository layout

- `src/` contains the sandbox agent, broker, market-data, risk, journal, and
  security packages.
- `scripts/` contains operational and verification entry points.
- `tests/` contains the main project's tests.
- `recruiter-finder/` is a separately installable application with its own
  package metadata, scripts, and tests. Its personal `config.json`, `.env`,
  state, and logs stay local and are excluded from Git.

## Optional LangChain strategy suggestions

The optional `src/agent/langchain_strategy.py` module uses LangChain's OpenAI
integration to produce a structured `OPEN` or `HOLD` suggestion from supplied
MU market snapshots. It has no tools, broker access, account balances, or order
submission capability. An `OPEN` suggestion is always one contract; the caller
must convert it to an intent and pass it through `RiskGate` and the existing
sandbox execution flow. Model output never overrides the Python risk controls.
The existing execution-proof runner does not invoke this strategy.

Install the project dependencies, copy `.env.example` to `.env`, and set
`OPENAI_API_KEY`. LangSmith tracing is enabled in the example configuration;
provide a valid `LANGSMITH_API_KEY` to send traces, or set
`LANGSMITH_TRACING=false` to disable tracing. Traces can contain the supplied
public MU market snapshots and model prompts, so review that data handling
before enabling remote tracing. Never commit `.env` or put credentials in
`.env.example`. Tavily is not used.
