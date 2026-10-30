# First bounded sandbox paper session

Run `.venv/bin/python scripts/run-paper-session.py` from this project only after
local tests pass and sandbox execution is authorized. No daemon is installed.
Production market data runs in a separate child process; its tokens never enter
the sandbox MCP process. Both MCP startup instructions and its runtime audit
must identify sandbox. A transport guard rejects every non-cert API origin.

The minimal implemented strategy set is long MU calls/puts, quantity-one opens,
with HOLD/CLOSE/PARTIAL_CLOSE support for owned positions. Spreads/rolls are not
enabled. Instrument membership, expiry, quote age, price, size, capital and
ownership are independently checked in Python. Unknown quote event time requires
a recent trade in addition to recent quote receipt. Missing/stale data fails closed.
Normal deployment ceiling is 70%, reserve floor 30%; no maximum-loss percentage
cap exists. This first session does not override reserve policy. Cash, equity,
premium deployment and defined premium-loss exposure are reported separately.
The gate accepts configurable ceiling/reserve values within today's authorized
70% ceiling and 30% floor. Create `config/PAPER_KILL_SWITCH` to block submissions.

SQLite `logs/paper-session.sqlite3` stores intent, market data, gate decision,
order request, curated MCP response and separately queried broker fills. Tokens
and confirmation tokens are never journaled. An intent is durably reserved before
submission. Any unknown outcome blocks all subsequent submission; there is no
automatic order retry. The official MCP does not expose external-identifier in
its simple order tool, so durable at-most-once local submission is used rather
than pretending the broker supports an idempotency key. An unresolved request
requires reconciliation before a future session; deleting the journal is unsafe.
Only orders created by this session may be canceled. A working test order is
canceled and re-queried; partial fills must match the resulting position.

After the execution proof there are at most three strategy observations and two
additional quantity-one entries. An entry requires measured MU momentum of at
least 0.2%, 30–60 delta, volume >=100 and spread <=10%, plus all hard checks.
Otherwise NO_TRADE. Owned positions are reassessed each observation; a 10%
market-midpoint move triggers a closing proposal. This is a minimal experimental
rule, not a validated profitable strategy. Every change passes RiskGate again.
The elapsed-time check prevents new cycles after eight minutes; in-flight calls
have finite timeouts, and reconciliation may extend beyond that check.

Sandbox fills are synthetic, not execution-quality or profitability evidence.
Actual broker fills and contemporaneous production bid/ask/mid are separate.
Market-referenced P&L uses observed midpoints and excludes slippage/fees; sandbox
net-equity changes are not evidence of strategy returns. Broker state resets
daily. Subsequent sessions currently require an empty baseline or explicit
reconciliation; they do not silently adopt pre-existing positions.

Read/OAuth transient recovery uses at most five recovery attempts after the
initial attempt, with 2/5/10/20/30-second delays. HTTP 429/502/503/504 and connection
failures qualify. Numeric or HTTP-date Retry-After is honored; delays longer than
five minutes leave the circuit degraded rather than retrying early. OAuth
400/401/403 is not retried. A downstream REST 401 invalidates MCP's cached bearer
and permits fresh OAuth within the same bounded budget. Writes never use this
retry policy. The non-submitting MCP dry-run may be retried safely.

BROKER_DEGRADED blocks submissions. Only completion of account/balance/position/
working-order reconciliation establishes BROKER_READY. No artificial 30-second
stability delay remains. A cooldown-gated `probe()` permits future bounded
half-open recovery. On exhausted recovery the bounded runner collects/journals
production market data if available and stops; it does not install a daemon.
OAuth/REST phases, statuses and expiry/margin metadata are journaled without
credentials or digests. The official cache renews 900-second tokens with a
60-second safety margin, verified against the installed code in mocked tests.

Only exact project sandbox-wrapper command lines qualify for stale cleanup;
bare MCP servers and production processes are never terminated. Ambiguous order
submissions trigger a read of broker order history, are marked UNKNOWN, and are
never automatically resubmitted. Candidate matching without an authoritative
broker ID is insufficient to clear that block.

Execution authorization is bound to the exact intent, reconciled portfolio,
normalized market objects, snapshot version, and broker recovery generation.
Any degradation/retry increments the generation and invalidates earlier quotes.
Preparation reconciles the broker first, then refreshes the selected contract via
a separate production market-data worker. It refreshes again after dry-run and
reruns all hard calculations. A limit outside the refreshed bid/ask requires a
new preview/confirmation; recovery during preview similarly restarts preparation.
No old confirmation can authorize a changed request. Five preparation rounds
bound persistent price instability; the 45-second quote freshness limit has not
been increased. Submitted results record the final production-market reference
separately from the broker's synthetic fill.
