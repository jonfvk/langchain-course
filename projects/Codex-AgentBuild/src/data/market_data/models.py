"""Normalized public MU market-data values; no transport or credential objects."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re


def require_mu(underlying):
    if type(underlying) is not str or underlying != 'MU':
        raise ValueError('Only MU underlying is allowed')


@dataclass(frozen=True)
class MUOptionContract:
    underlying: str
    symbol: str
    streamer_symbol: str
    expiration: str
    strike: float
    option_type: str
    stops_trading_at: str | None = None

    def __post_init__(self):
        require_mu(self.underlying)
        # Validate returned identities; never synthesize a streamer symbol.
        if not re.fullmatch(r'MU\s+\d{6}[CP]\d{8}', self.symbol):
            raise ValueError('Nonstandard or non-MU option identity rejected')
        if not re.fullmatch(r'\.MU\d{6}[CP]\d+(?:\.\d+)?', self.streamer_symbol):
            raise ValueError('Non-MU streamer identity rejected')
        exp = datetime.strptime(self.expiration, '%Y-%m-%d').strftime('%y%m%d')
        occ = re.fullmatch(r'MU\s+(\d{6})([CP])(\d{8})', self.symbol)
        stream = re.fullmatch(r'\.MU(\d{6})([CP])(\d+(?:\.\d+)?)', self.streamer_symbol)
        if (self.option_type not in ('C','P') or not math.isfinite(self.strike) or self.strike <= 0
                or occ[1] != exp or stream[1] != exp or occ[2] != self.option_type
                or stream[2] != self.option_type or abs(int(occ[3])/1000-self.strike)>1e-8
                or abs(float(stream[3])-self.strike)>1e-8):
            raise ValueError('Inconsistent MU option instrument metadata')


@dataclass
class MUUnderlyingSnapshot:
    symbol: str = 'MU'
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    last_size: float | None = None
    volume: float | None = None
    mid: float | None = None
    mark: float | None = None
    mark_source: str | None = None
    event_timestamps: dict = field(default_factory=dict)
    received_timestamps: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    available_events: list = field(default_factory=list)
    missing_fields: list = field(default_factory=list)

    def __post_init__(self):
        require_mu(self.symbol)


@dataclass
class MUOptionSnapshot(MUUnderlyingSnapshot):
    contract: MUOptionContract | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    open_interest: float | None = None
    theoretical_price: float | None = None
    theoretical_delta: float | None = None
    theoretical_gamma: float | None = None
    summary_day_id: int | None = None


def freshness(event_time, receipt_time, now=None, max_age=60):
    now = now or datetime.now(timezone.utc)
    if receipt_time is None:
        return 'MISSING'
    received = datetime.fromisoformat(receipt_time)
    if (now-received).total_seconds() > max_age:
        return 'STALE'
    if event_time is None:
        return 'UNKNOWN_EVENT_TIME'
    age = (now-datetime.fromisoformat(event_time)).total_seconds()
    return 'FRESH' if -5 <= age <= max_age else 'STALE'
