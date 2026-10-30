"""Minimum long-option paper RiskGate. No broker, credentials, or network."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from src.risk.capital import money

@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    symbol: str
    action: str
    quantity: int
    limit_price: str
    rationale: str
    environment: str = 'sandbox'
    underlying: str = 'MU'
    order_type: str = 'Limit'

class RiskRejected(ValueError):pass

def decimal(value):
    return money(str(value)) if type(value) is float else money(value)

class RiskGate:
    def __init__(self,catalog,kill_switch,ceiling='0.70',reserve='0.30'):
        self.catalog={c.symbol:c for c in catalog}
        self.kill_switch=Path(kill_switch)
        self.ceiling=money(ceiling);self.reserve=money(reserve)
        if not 0<self.ceiling<=Decimal('.70') or self.reserve<Decimal('.30') or self.reserve+self.ceiling!=1:
            raise ValueError('Invalid paper deployment policy')

    def check(self,intent,state,quote,underlying,seen=False,now=None):
        now=now or datetime.now(timezone.utc)
        def require(ok,reason):
            if not ok:raise RiskRejected(reason)
        require(not self.kill_switch.exists(),'KILL_SWITCH')
        require(intent.environment=='sandbox' and state['environment']=='sandbox','SANDBOX_REQUIRED')
        require(intent.underlying=='MU','MU_REQUIRED')
        require(not seen,'DUPLICATE_INTENT')
        require(bool(intent.intent_id) and bool(intent.rationale.strip()),'ID_AND_RATIONALE_REQUIRED')
        require(intent.order_type=='Limit','LIMIT_REQUIRED')
        require(type(intent.quantity) is int and intent.quantity>0,'QUANTITY_INVALID')
        require(intent.action in ('OPEN','CLOSE','PARTIAL_CLOSE'),'ONLY_LONG_OPTIONS_SUPPORTED')
        contract=self.catalog.get(intent.symbol)
        require(contract is not None and contract.underlying=='MU' and quote.contract==contract,'TRUSTED_MU_CONTRACT_REQUIRED')
        contract.__post_init__()
        require(contract.expiration>now.date().isoformat(),'FUTURE_EXPIRATION_REQUIRED')
        if contract.stops_trading_at:
            require(datetime.fromisoformat(contract.stops_trading_at)>now,'EXPIRED')
        for snap in (underlying,quote):
            require(snap.symbol=='MU','MU_REQUIRED')
            stamp=snap.received_timestamps.get('Quote')
            require(stamp is not None,'QUOTE_MISSING')
            require(-2<=(now-datetime.fromisoformat(stamp)).total_seconds()<=45,'STALE_QUOTE')
            event=snap.event_timestamps.get('Quote')
            if event:
                require(-2<=(now-datetime.fromisoformat(event)).total_seconds()<=120,'STALE_MARKET_QUOTE')
            else:
                # Snapshot receipt alone cannot prove a live market: require a recent trade.
                trade=snap.event_timestamps.get('Trade')
                require(trade is not None and -2<=(now-datetime.fromisoformat(trade)).total_seconds()<=120,'CURRENT_MARKET_UNPROVEN')
            require(snap.bid is not None and snap.ask is not None,'QUOTE_MISSING')
            require(0<decimal(snap.bid)<=decimal(snap.ask),'INVALID_QUOTE')
        bid=decimal(quote.bid);ask=decimal(quote.ask);price=money(intent.limit_price)
        require(price>0 and price*100==(price*100).to_integral_value(),'LIMIT_PRICE_INVALID')
        require(bid<=price<=ask,'LIMIT_OUTSIDE_CURRENT_MARKET')
        require((ask-bid)/((ask+bid)/2)<=Decimal('.20'),'SPREAD_TOO_WIDE')
        require(decimal(quote.ask_size or 0)>=intent.quantity if intent.action=='OPEN' else decimal(quote.bid_size or 0)>=intent.quantity,'INSUFFICIENT_DISPLAYED_SIZE')
        require(not state['working_orders'],'WORKING_ORDER_RECONCILIATION_REQUIRED')
        require(-2<=(now-datetime.fromisoformat(state['timestamp'])).total_seconds()<=45,'STALE_ACCOUNT_STATE')
        equity=money(state['equity']);cash=money(state['cash']);bp=money(state['buying_power'])
        require(equity>0,'NONPOSITIVE_EQUITY')
        deployed=money(state['deployed']);maximum_loss=money(state['maximum_loss'])
        owned=state['owned'].get(intent.symbol,0)
        fees=Decimal('2')*intent.quantity # Conservative entry allowance; broker fees reconciled separately.
        amount=price*100*intent.quantity
        if intent.action=='OPEN':
            require((deployed+amount+fees)/equity<=self.ceiling,'DEPLOYMENT_CEILING')
            require(cash-amount-fees>=self.reserve*equity,'RESERVE_FLOOR')
            require(amount+fees<=bp,'BUYING_POWER')
            maximum_loss+=amount+fees
            deployed+=amount+fees
        else:
            require(intent.quantity<=owned and intent.symbol in state['agent_owned'],'UNCOVERED_OR_UNOWNED_CLOSE')
            require(intent.quantity==owned if intent.action=='CLOSE' else intent.quantity<owned,'CLOSE_QUANTITY_MISMATCH')
        return {'approved':True,'maximum_position_loss':str(amount+fees) if intent.action=='OPEN' else '0',
                'maximum_portfolio_loss_bound':str(maximum_loss),'projected_deployed':str(deployed),
                'deployment_fraction':str(deployed/equity),'equity':str(equity),
                'reserve_after':str(cash-amount-fees if intent.action=='OPEN' else cash+amount-fees)}
