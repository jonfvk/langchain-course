# Bounded production MU market-data layer

Run an explicitly authorized bounded live verification:

```sh
.venv/bin/python scripts/verify-mu-market-data.py
```

No new dependencies were needed for the options layer. `websockets==16.1.1`
remains pinned and installed only in this project's `.venv`.

## Public interface

```python
from src.data.market_data.mu_data import MUMarketData

with MUMarketData() as data:
    underlying, connection = data.underlying_snapshot('MU')
    contracts = data.discover_options('MU')
    sample = data.near_money_sample(underlying.mid, count=4)
    underlying, options, connection = data.option_snapshots(sample)
```

The outputs are `MUUnderlyingSnapshot`, `MUOptionSnapshot`, and
`MUOptionContract` objects. No raw frames, tokens or generic HTTP methods are
exposed by this interface. The catalog includes expiration, strike, call/put,
OCC symbol, streamer symbol, and trading cutoff. Expired trading cutoffs are
excluded from sample selection. Adjusted/nonstandard roots are excluded.

Every option streamer symbol comes from `GET /option-chains/MU` instrument data.
Subscriptions require exact catalog membership and agreement between the MU
underlying, returned OCC/streamer identities, expiration, strike and call/put.
Symbols are validated, never generated. Every non-MU underlying is rejected
before credentials or transport. A stream is limited to at most eight option
contracts; the live verifier chooses four near-the-money calls/puts in the
nearest still-trading expiration. This is proximity selection, not a guarantee
of liquidity; returned volume, open interest and spreads support assessment.

## Production boundaries and tokens

The internal REST transport allows only POST /oauth/token, GET /api-quote-tokens,
and the exact paginated GET /option-chains/MU form (250 per page, at most 100
pages). No accounts, positions, balances, order endpoints or trade scope exist.
Only the MU chain response has a larger 16 MiB bound; token responses retain the
1 MiB limit. The documented pagination metadata is used when present. A full
unpaginated response larger than the requested page size is accepted as complete.

Credentials come only from the production Keychain loader. The OAuth payload
contains exactly grant_type, refresh_token and client_secret; the configured
production grant is read-only and an explicitly broader response scope is
rejected. OAuth is refreshed with a 30-second expiration margin. Each WebSocket
attempt obtains quote credentials anew. Quote expiration is checked and limits
the stream deadline. Tokens stay in memory and are cleared on context exit.
Python does not guarantee memory zeroization.

The DXLink URL must originate in the authenticated production quote response,
use exactly wss, have a valid hostname equal to dxfeed.com or ending in
.dxfeed.com, no userinfo, default/443 port, no fragment, and no whitespace or
backslashes. Dynamic paths are allowed. Lookalikes, IP literals and unrelated
domains are rejected. WebSocket proxies and redirects are not used. TLS is
verified with certifi. A private disabled logger prevents AUTH/frame logging.
Exceptions and public status are sanitized; credentials are never persisted.

## Streaming and reliability

Protocol: SETUP, AUTH after UNAUTHORIZED, CHANNEL_REQUEST (odd channel 3, FEED,
AUTO), FEED_SETUP COMPACT, subscription, negotiated FEED_CONFIG, FEED_DATA.
Initial empty/default configurations are not treated as final schemas.
Subscription is sent after setup; per-event schemas are learned as they arrive.
The decoder follows negotiated field order and validates every delivered symbol.

There is no daemon. Each connection streams for 14 seconds by default (public
limit 1–30 seconds), then sends CHANNEL_CANCEL and closes. Open timeout is ten
seconds; close timeout is one second. Heartbeats run independently of data
traffic, at most five seconds apart and at half the server's negotiated timeout.
Authentication has a ten-second deadline. Transport failures have at most one
reconnect with backoff/jitter and new quote credentials. Credential, scope,
protocol and validation failures do not trigger blind retries. Failed-session
events are discarded before a reconnect to prevent mixing connection snapshots.

## Fields and provenance

| Source | Normalized fields |
|---|---|
| Quote | bid, ask, bid_size, ask_size; bid/ask business timestamps when available |
| Trade | regular-session last, last_size, total day volume, trade timestamp |
| Greeks | IV (volatility, unchanged source units), delta, gamma, theta, vega, rho, timestamp |
| Summary | open_interest, summary_day_id (daily metadata, not tick time) |
| TheoPrice | theoretical_price, theoretical_delta/gamma, timestamp |
| Profile | Description/status metadata; not required for the requested price/Greek/OI fields and not subscribed |

Mid/mark is the bid/ask arithmetic midpoint only for finite, nonnegative,
non-crossed two-sided quotes. It is labelled `bid_ask_midpoint`, never substituted
with a last trade or theoretical value. Missing/nonfinite fields remain None.
Negative option Greeks are preserved. Prices, sizes, volume and IV are not
invented or filled from unrelated event types.

Each event type records event time separately from local receive time. Freshness
is evaluated at snapshot creation with a conservative 60-second age threshold:
FRESH, STALE, UNKNOWN_EVENT_TIME, or MISSING. A new connection does not make an
old trade fresh. Summary OI has daily semantics; no tick freshness is claimed.
When retaining snapshots, re-evaluate `models.freshness()` with the current time.
No REST fallback for OI is needed because Summary is its documented source.
Server/entitlement omissions remain explicit missing values.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -q
```

Tests cover symbol and catalog isolation, precise REST allowlisting, destination
validation, negotiated/partial schemas, batching, no token output, signed Greeks,
midpoint and missing values, freshness, token expiry, bounded reconnect, and close.

Official references:
- https://developer.tastytrade.com/docs/guides/stream-market-data/
- https://developer.tastytrade.com/reference/instruments/getOptionChainsSymbol/
- https://developer.tastytrade.com/docs/guides/pagination-and-filtering/
- https://github.com/dxFeed/dxLink/blob/main/dxlink-specification/asyncapi.yml
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Trade.html
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Summary.html
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/TheoPrice.html
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Profile.html
