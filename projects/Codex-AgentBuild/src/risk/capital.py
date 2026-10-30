"""Offline MU option accounting and hard operator-owned capital policy.

Money uses Decimal. No brokerage, network, credentials, or order API imports.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from collections import defaultdict
import json
from pathlib import Path

ZERO = Decimal('0')
ONE = Decimal('1')


def money(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('Money must be Decimal, integer, or decimal text')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid decimal') from None
    if not result.is_finite():
        raise ValueError('Nonfinite decimal')
    return result


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return value.astimezone(timezone.utc)


def positive_int(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Positive integer required')


@dataclass(frozen=True)
class CapitalPolicy:
    target_capital_deployment: Decimal = Decimal('0.70')
    target_reserve: Decimal = Decimal('0.30')
    minimum_emergency_reserve: Decimal = Decimal('0.10')
    fee_per_contract: Decimal = Decimal('0.65')
    max_quote_age_seconds: int = 30
    approval_lifetime_seconds: int = 15
    minimum_open_days_to_expiration: int = 1
    scenario_spot_moves: tuple = (Decimal('-0.50'),Decimal('-0.30'),Decimal('-0.15'),ZERO,Decimal('0.15'),Decimal('0.30'),Decimal('0.50'))
    scenario_iv_multipliers: tuple = (Decimal('0.70'),ONE,Decimal('1.50'))
    scenario_days: tuple = (0,7,30)

    def __post_init__(self):
        fractions=('target_capital_deployment','target_reserve','minimum_emergency_reserve')
        for name in fractions:
            value=money(getattr(self,name));object.__setattr__(self,name,value)
            if not ZERO <= value <= ONE: raise ValueError('Invalid policy fraction')
        if self.target_capital_deployment+self.target_reserve != ONE:
            raise ValueError('Deployment and reserve targets must sum to one')
        if not ZERO < self.minimum_emergency_reserve <= self.target_reserve:
            raise ValueError('Emergency reserve must be positive and below normal reserve')
        object.__setattr__(self,'fee_per_contract',money(self.fee_per_contract))
        if self.fee_per_contract < 0: raise ValueError('Negative fees')
        positive_int(self.max_quote_age_seconds);positive_int(self.approval_lifetime_seconds)
        if type(self.minimum_open_days_to_expiration) is not int or self.minimum_open_days_to_expiration < 1:
            raise ValueError('Opening expiration buffer required')
        moves=tuple(money(x) for x in self.scenario_spot_moves)
        ivs=tuple(money(x) for x in self.scenario_iv_multipliers)
        days=tuple(self.scenario_days)
        if not moves or min(moves)>Decimal('-.30') or max(moves)<Decimal('.30') or min(moves)<=-1 or ZERO not in moves:
            raise ValueError('Substantial two-sided MU scenarios required')
        if not ivs or min(ivs)<=0 or min(ivs)>=1 or max(ivs)<=1 or ONE not in ivs:
            raise ValueError('IV contraction and expansion required')
        if not days or any(type(x) is not int or x<0 for x in days) or 0 not in days or max(days)<7:
            raise ValueError('Time-passage scenarios required')
        object.__setattr__(self,'scenario_spot_moves',moves)
        object.__setattr__(self,'scenario_iv_multipliers',ivs)
        object.__setattr__(self,'scenario_days',days)

    @classmethod
    def from_file(cls,path):
        # Operator configuration only; never accepted in strategist proposals.
        values=json.loads(Path(path).read_text(),parse_float=Decimal)
        return cls(**{key.lower():value for key,value in values.items()})


class Action(str,Enum):
    OPEN='OPEN'
    ADD='ADD'
    PARTIAL_CLOSE='PARTIAL_CLOSE'
    CLOSE='CLOSE'
    ROLL='ROLL'
    RESTRUCTURE='RESTRUCTURE'
    RESERVE_DEPLOYMENT='RESERVE_DEPLOYMENT'


@dataclass(frozen=True,order=True)
class Option:
    symbol: str
    underlying: str
    expiration: datetime
    strike: Decimal
    right: str
    multiplier: int = 100

    def __post_init__(self):
        import re
        if self.underlying!='MU': raise ValueError('Only MU options allowed')
        exp=utc(self.expiration);object.__setattr__(self,'expiration',exp)
        strike=money(self.strike);object.__setattr__(self,'strike',strike)
        if strike<=0 or self.right not in ('C','P') or self.multiplier!=100:
            raise ValueError('Only standard MU equity options supported')
        match=re.fullmatch(r'MU\s+(\d{6})([CP])(\d{8})',self.symbol)
        if not match or match[1]!=exp.strftime('%y%m%d') or match[2]!=self.right or Decimal(match[3])/1000!=strike:
            raise ValueError('MU option identity mismatch')


@dataclass(frozen=True)
class Leg:
    option: Option
    ratio: int  # Positive long, negative short, per structure unit.
    def __post_init__(self):
        if not isinstance(self.option,Option) or type(self.ratio) is not int or self.ratio==0:
            raise ValueError('Invalid structure leg')


def validate_structure(legs):
    if not isinstance(legs,tuple) or not 1<=len(legs)<=8:
        raise ValueError('One to eight option legs required')
    if len({leg.option.symbol for leg in legs})!=len(legs):
        raise ValueError('Duplicate legs must be netted before proposing')
    groups=defaultdict(lambda:[0,0])
    for leg in legs:
        leg.option.__post_init__();leg.__post_init__()
        bucket=groups[(leg.option.expiration,leg.option.right,leg.option.multiplier)]
        bucket[0 if leg.ratio>0 else 1]+=abs(leg.ratio)
    if any(short>long for long,short in groups.values()):
        raise ValueError('Naked shorts or cross-expiry short coverage prohibited')
    return legs


def signature(legs):
    return tuple(sorted((leg.option.symbol,leg.ratio) for leg in legs))


def payoff_floor(legs,units=1):
    """Exact piecewise-linear minimum per expiry, summed conservatively across dates.

    Calls have nonnegative upper-tail slope after coverage validation. Therefore
    zero and all strikes contain every minimum. No independence/diversification
    credit between MU positions or expiration dates is presumed.
    """
    validate_structure(legs);positive_int(units)
    groups=defaultdict(list)
    for leg in legs: groups[leg.option.expiration].append(leg)
    floor=ZERO
    for group in groups.values():
        points={ZERO}|{leg.option.strike for leg in group}
        values=[]
        for spot in points:
            values.append(sum((Decimal(leg.ratio*units*leg.option.multiplier)*
                max(ZERO,spot-leg.option.strike if leg.option.right=='C' else leg.option.strike-spot)
                for leg in group),ZERO))
        floor+=min(values)
    return floor


@dataclass(frozen=True)
class Lot:
    lot_id: str
    units: int
    cost_basis: Decimal  # Signed net premium debit including entry fees.
    reserve_deployment: bool = False
    scaled_entry: bool = False
    def __post_init__(self):
        positive_int(self.units);object.__setattr__(self,'cost_basis',money(self.cost_basis))
        if not self.lot_id: raise ValueError('Lot ID required')


@dataclass(frozen=True)
class Position:
    position_id: str
    legs: tuple[Leg,...]
    lots: tuple[Lot,...]
    def __post_init__(self):
        validate_structure(self.legs)
        if not self.position_id or not self.lots or len({x.lot_id for x in self.lots})!=len(self.lots):
            raise ValueError('Position requires unique lots')
    @property
    def units(self): return sum(x.units for x in self.lots)
    @property
    def cost_basis(self): return sum((x.cost_basis for x in self.lots),ZERO)
    @property
    def collateral(self): return max(ZERO,-payoff_floor(self.legs,self.units))
    @property
    def maximum_loss(self): return max(ZERO,self.cost_basis-payoff_floor(self.legs,self.units))
    @property
    def deployed(self):
        # Gross capital commitment, deliberately NOT max loss or market value.
        return sum((max(ZERO,x.cost_basis) for x in self.lots),ZERO)+self.collateral


@dataclass(frozen=True)
class Portfolio:
    initial_equity: Decimal
    cash: Decimal
    positions: tuple[Position,...] = ()
    realized_pnl: Decimal = ZERO
    revision: int = 0
    def __post_init__(self):
        for name in ('initial_equity','cash','realized_pnl'):
            object.__setattr__(self,name,money(getattr(self,name)))
        if self.initial_equity<=0 or type(self.revision) is not int or self.revision<0:
            raise ValueError('Invalid portfolio')
        if len({p.position_id for p in self.positions})!=len(self.positions):
            raise ValueError('Duplicate position ID')
        # No external deposits/withdrawals in this initial research ledger.
        if self.cash != self.initial_equity+self.realized_pnl-sum((p.cost_basis for p in self.positions),ZERO):
            raise ValueError('Cash, realized P&L, and open cost basis do not reconcile')


@dataclass(frozen=True)
class Quote:
    option: Option
    bid: Decimal
    ask: Decimal
    iv: Decimal
    timestamp: datetime
    def __post_init__(self):
        for name in ('bid','ask','iv'):object.__setattr__(self,name,money(getattr(self,name)))
        object.__setattr__(self,'timestamp',utc(self.timestamp))
        if not ZERO<=self.bid<=self.ask or self.ask<=0 or self.iv<=0:
            raise ValueError('Valid two-sided quote and positive IV required')
    @property
    def mid(self): return (self.bid+self.ask)/2


@dataclass(frozen=True)
class Market:
    spot: Decimal
    timestamp: datetime
    quotes: tuple[Quote,...]
    rate: Decimal = Decimal('0.04')
    dividend_yield: Decimal = ZERO
    underlying: str = 'MU'
    def __post_init__(self):
        if self.underlying!='MU':raise ValueError('Only MU market state allowed')
        for name in ('spot','rate','dividend_yield'):object.__setattr__(self,name,money(getattr(self,name)))
        object.__setattr__(self,'timestamp',utc(self.timestamp))
        if self.spot<=0 or len({q.option.symbol for q in self.quotes})!=len(self.quotes):raise ValueError('Invalid market')
    def quote(self,option):
        quote=next((q for q in self.quotes if q.option==option),None)
        if quote is None:raise ValueError('Missing verified quote/IV for portfolio option')
        return quote


@dataclass(frozen=True)
class CapitalSnapshot:
    equity: Decimal
    cash: Decimal
    reserve: Decimal
    capital_deployed: Decimal
    collateral_reserved: Decimal
    buying_power_used: Decimal  # Modeled initial requirement, not broker-confirmed BP.
    maximum_portfolio_loss: Decimal  # Conservative sum, no cross-position netting credit.
    remaining_maximum_loss: Decimal  # Current marked value minus terminal payoff floor.
    marked_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    position_maximum_losses: tuple
    deployment_fraction: Decimal
    reserve_fraction: Decimal


def capital_snapshot(portfolio,market):
    marked=sum((market.quote(leg.option).mid*leg.ratio*leg.option.multiplier*p.units
                for p in portfolio.positions for leg in p.legs),ZERO)
    basis=sum((p.cost_basis for p in portfolio.positions),ZERO)
    equity=portfolio.cash+marked
    if equity<=0:raise ValueError('Nonpositive equity')
    collateral=sum((p.collateral for p in portfolio.positions),ZERO)
    deployed=sum((p.deployed for p in portfolio.positions),ZERO)
    losses=sum((p.maximum_loss for p in portfolio.positions),ZERO)
    remaining=max(ZERO,marked-sum((payoff_floor(p.legs,p.units) for p in portfolio.positions),ZERO))
    return CapitalSnapshot(equity,portfolio.cash,portfolio.cash-collateral,deployed,collateral,
                           losses,losses,remaining,marked,portfolio.realized_pnl,marked-basis,
                           tuple((p.position_id,p.maximum_loss) for p in portfolio.positions),
                           deployed/equity,(portfolio.cash-collateral)/equity)


@dataclass(frozen=True)
class Proposal:
    action: Action
    position_id: str
    units: int
    rationale: str
    legs: tuple[Leg,...] = ()
    open_debit_ceiling: Decimal | None = None  # Per structure unit, dollars incl multiplier, excl fees.
    close_credit_floor: Decimal | None = None
    replacement_units: int | None = None
    reserve_operation: Action | None = None
    def __post_init__(self):
        if not isinstance(self.action,Action):raise ValueError('Explicit action required')
        positive_int(self.units)
        if not self.position_id or not self.rationale.strip():raise ValueError('ID and rationale required')
        for name in ('open_debit_ceiling','close_credit_floor'):
            value=getattr(self,name)
            if value is not None:object.__setattr__(self,name,money(value))
        if self.replacement_units is not None:positive_int(self.replacement_units)
        if self.action==Action.RESERVE_DEPLOYMENT:
            if self.reserve_operation not in (Action.OPEN,Action.ADD,Action.ROLL,Action.RESTRUCTURE):
                raise ValueError('Reserve deployment requires an explicit exposure operation')
        elif self.reserve_operation is not None:raise ValueError('Reserve flag requires RESERVE_DEPLOYMENT action')
    @property
    def operation(self):return self.reserve_operation if self.action==Action.RESERVE_DEPLOYMENT else self.action
