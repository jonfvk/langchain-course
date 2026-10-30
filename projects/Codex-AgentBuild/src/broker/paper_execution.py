"""Restricted sandbox execution. No production imports or destination options."""
from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import asdict, replace
import time
import json
import copy
import hashlib
import hmac
from src.security.sandbox_keychain import load_sandbox
from src.broker.sandbox_inspection import SandboxInspection, PreflightError
from src.risk.paper import decimal, RiskRejected

class ReconciliationError(RuntimeError):pass
class BrokerDegraded(ReconciliationError):pass

def rows(result):
    value=result.get('items') if isinstance(result,dict) else result
    if not isinstance(value,list):raise ReconciliationError('Expected collection')
    return value

def curated_order(order):
    keys=('id','status','price','price-effect','size','remaining-quantity','filled-quantity','order-type','time-in-force','received-at','updated-at')
    result={k:order[k] for k in keys if k in order}
    result['legs']=[{k:leg[k] for k in ('symbol','action','quantity','remaining-quantity','instrument-type','fills') if k in leg} for leg in order.get('legs',[])]
    return result

class PaperExecution(SandboxInspection):
    def __init__(self,environment,journal,gate,*,trading=False):
        super().__init__(environment)
        self._read_only=not trading
        self.journal=journal;self.gate=gate;self.account=None
        self.agent_owned=set()
        self.lifecycle=[]
        self.broker_status='BROKER_DEGRADED'
        self.retry_not_before=0
        self.degraded_hook=None
        self.recovery_generation=0
        self.snapshot_version=0
        self._last_state=None

    def assert_destination(self):
        events=[x for x in self.audit if x.get('kind')=='environment']
        if not self.sandbox_verified or len(events)!=1 or events[0].get('environment')!='sandbox' or events[0].get('destination')!='https://api.cert.tastyworks.com':
            raise ReconciliationError('Sandbox destination not proven')
        if events[0].get('read_only')!=('1' if self._read_only else '0'):
            raise ReconciliationError('Runtime mode mismatch')

    def _call(self, name, arguments=None):
        start=len(self.audit)
        try:return super()._call(name, arguments)
        finally:
            for event in self.audit[start:]:
                if event.get('kind')=='http':
                    safe={k:event[k] for k in ('phase','status','access_token_obtained','expires_in','refresh_margin_seconds','retry_after_seconds','error_code','error_message','error_details') if k in event}
                    self.lifecycle.append(safe)
                    self.journal.record('MCP_LIFECYCLE',safe)

    def _verify_current_credentials(self):
        credentials=load_sandbox()
        environment=next((x for x in self.audit if x.get('kind')=='environment'),{})
        for key,field in (('CLIENT_ID','client_id'),('CLIENT_SECRET','client_secret'),('REFRESH_TOKEN','refresh_token')):
            expected=hashlib.sha256(getattr(credentials,field)._for_auth().encode()).hexdigest()
            received=environment.get('credentials',{}).get(key,{}).get('digest','')
            if not hmac.compare_digest(expected,received):
                raise ReconciliationError('Sandbox credentials changed since process launch; no retry')
        del credentials

    def _degrade(self, reason, delay=30):
        self.recovery_generation+=1
        self.snapshot_version+=1
        self.broker_status='BROKER_DEGRADED'
        self.retry_not_before=time.monotonic()+delay
        self.journal.record('BROKER_STATE',{'state':self.broker_status,'reason':reason,'retry_after_seconds':delay})

    def _read_with_recovery(self, operation):
        backoff=(2,5,10,20,30)
        recovering=False
        for attempt in range(6): # Initial attempt + at most five recovery attempts.
            start=len(self.lifecycle)
            try:
                result=operation()
                if recovering:
                    self.journal.record('AUTH_RECOVERY',{'attempted':True,'succeeded':True})
                    self.lifecycle.append({'recovery':'succeeded'})
                return result
            except PreflightError as exc:
                message=str(exc)
                events=[x for x in self.lifecycle[start:] if 'phase' in x]
                last=events[-1] if events else {}
                is401='MCP_auth_failed; HTTP_STATUS=401' in message
                # OAuth 400/401/403 is a credential rejection; downstream REST
                # 401 instead lets the installed MCP retire its cached bearer.
                if last.get('phase')=='oauth' and last.get('status') in (400,401,403):
                    self._degrade('Permanent OAuth rejection');raise
                transient=any('HTTP_STATUS='+str(s) in message for s in (429,500,502,503,504)) or message in ('MCP_network; HTTP_STATUS=unknown','MCP_rate_limit_exceeded; HTTP_STATUS=unknown')
                if not (transient or is401):
                    self._degrade('Non-retryable broker error');raise
                self._degrade('Transient broker/authentication failure')
                if attempt==5:
                    if self.degraded_hook is not None:self.degraded_hook()
                    raise BrokerDegraded('BROKER_DEGRADED: five recovery attempts exhausted') from None
                if is401:
                    self._verify_current_credentials();recovering=True
                    self.lifecycle.append({'recovery':'attempted'})
                retry_after=max(last.get('retry_after_seconds',0),exc.retry_after_seconds)
                delay=max(backoff[attempt],retry_after)
                if delay>300:
                    self._degrade('Retry-After exceeds bounded session wait',delay)
                    if self.degraded_hook is not None:self.degraded_hook()
                    raise BrokerDegraded('BROKER_DEGRADED: respecting long Retry-After without early retry')
                self.journal.record('BROKER_RETRY',{'recovery_attempt':attempt+1,'delay_seconds':delay,'read_only':True})
                time.sleep(delay)
        raise BrokerDegraded('BROKER_DEGRADED')

    def probe(self):
        """Periodic half-open recovery; a complete reconciled portfolio closes circuit."""
        if time.monotonic()<self.retry_not_before:
            raise BrokerDegraded('BROKER_DEGRADED: recovery cooldown active')
        return self.state()

    def _require_ready(self):
        if self.broker_status!='BROKER_READY' or self.journal.unresolved():
            raise BrokerDegraded('BROKER_DEGRADED: submissions disabled')

    def read(self, name, arguments=None):
        return self._read_with_recovery(lambda:super(PaperExecution,self).read(name,arguments))

    def state(self):
        self.assert_destination()
        accounts=rows(self.read('tastytrade_get_accounts'))
        if len(accounts)!=1:raise ReconciliationError('Account selection ambiguous')
        account=accounts[0]['account']['account-number']
        if self.account is not None and self.account!=account:raise ReconciliationError('Account changed')
        self.account=account
        args={'account_number':account}
        balance=self.read('tastytrade_get_balances',args)
        balances=rows(balance) if 'items' in balance else [balance]
        usd=[x for x in balances if x.get('currency')=='USD']
        if len(usd)!=1:raise ReconciliationError('USD balance unrecognized')
        b=usd[0]
        def amount(key):
            value=decimal(b[key]);effect=b.get(key+'-effect')
            if effect=='Debit':value=-abs(value)
            elif effect not in ('Credit','None',None):raise ReconciliationError('Unknown balance sign')
            return value
        positions=rows(self.read('tastytrade_get_positions',args))
        orders=rows(self.read('tastytrade_get_live_orders',args))
        owned={};deployed=Decimal(0);safe_positions=[]
        for p in positions:
            if p.get('instrument-type')!='Equity Option' or p.get('underlying-symbol')!='MU' or p.get('quantity-direction')!='Long':
                raise ReconciliationError('Unexpected exposure: only long MU options supported')
            quantity=decimal(p['quantity']);mult=decimal(p['multiplier'])
            if quantity<=0 or quantity!=quantity.to_integral_value() or mult!=100:raise ReconciliationError('Invalid held option quantity/multiplier')
            owned[p['symbol']]=int(quantity)
            basis=decimal(p['average-open-price'])*mult*quantity
            if basis<0:raise ReconciliationError('Negative long basis')
            deployed+=basis
            safe_positions.append({k:p[k] for k in ('symbol','quantity','quantity-direction','average-open-price','mark-price','close-price','realized-day-gain','unrealized-day-gain') if k in p})
        state={'environment':'sandbox','timestamp':datetime.now(timezone.utc).isoformat(),
            'equity':str(amount('net-liquidating-value')),'cash':str(amount('cash-balance')),
            'buying_power':str(amount('derivative-buying-power')),'deployed':str(deployed),
            'maximum_loss':str(deployed),'owned':owned,'agent_owned':sorted(self.agent_owned),
            'working_orders':[curated_order(o) for o in orders if o.get('status') not in ('Filled','Cancelled','Canceled','Rejected','Expired')],
            'today_orders':[curated_order(o) for o in orders],'positions':safe_positions}
        self.journal.record('PORTFOLIO',state)
        self.broker_status='BROKER_DEGRADED' if self.journal.unresolved() else 'BROKER_READY'
        self.retry_not_before=0
        self.journal.record('BROKER_STATE',{'state':self.broker_status,'portfolio_reconciled':not self.journal.unresolved()})
        self._last_state=(self.recovery_generation,copy.deepcopy(state))
        return state

    def _order(self,order_id):
        self.assert_destination()
        data=self._read_with_recovery(lambda:self._call('tastytrade_get_order',{'account_number':self.account,'order_id':str(order_id)}))
        if not isinstance(data,dict) or str(data.get('id'))!=str(order_id):raise ReconciliationError('Order ID mismatch')
        return data

    def _approval_version(self,intent,state,quote,underlying):
        value={'intent':asdict(intent),'state':state,'quote':asdict(quote),'underlying':asdict(underlying),
               'broker_generation':self.recovery_generation,'snapshot_version':self.snapshot_version}
        return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

    def execute(self,intent,quote,underlying,*,refresh=None):
        self.assert_destination()
        if self._read_only:raise ReconciliationError('Read-only session')
        initial_generation=self.recovery_generation
        def fresh():
            nonlocal quote,underlying
            if refresh is not None:
                quote,underlying=refresh(intent.symbol)
            elif self.recovery_generation!=initial_generation:
                raise RiskRejected('SNAPSHOT_INVALIDATED: fresh market source required')
            self.snapshot_version+=1
            return quote,underlying
        # A preview does not submit anything. Re-prepare it when recovery, quote
        # ageing, or a changed limit invalidates its price-bound confirmation.
        for preparation in range(5):
            cached=self._last_state
            if cached and cached[0]==self.recovery_generation and self.broker_status=='BROKER_READY' and 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(cached[1]['timestamp'])).total_seconds()<=15:
                state=copy.deepcopy(cached[1])
            else:
                state=self.state()
            self._require_ready()
            generation=self.recovery_generation
            fresh()
            if generation!=self.recovery_generation:continue
            side=quote.ask if intent.action=='OPEN' else quote.bid
            if side is None:raise RiskRejected('QUOTE_MISSING')
            intent=replace(intent,limit_price=str(decimal(side).quantize(Decimal('.01'))))
            self.journal.record('PROPOSAL',{'intent':asdict(intent),'quote':asdict(quote),'underlying':asdict(underlying),
                'snapshot_version':self.snapshot_version,'broker_generation':generation},intent.intent_id)
            try:decision=self.gate.check(intent,state,quote,underlying,seen=self.journal.seen(intent.intent_id))
            except RiskRejected as exc:
                if str(exc) in ('STALE_QUOTE','STALE_ACCOUNT_STATE') and refresh is not None:continue
                self.journal.record('RISK_REJECTED',{'approved':False,'reason':str(exc)},intent.intent_id);raise
            args={'account_number':self.account,'order_type':'Limit','time_in_force':'Day',
                'price':intent.limit_price,'price_effect':'Debit' if intent.action=='OPEN' else 'Credit',
                'legs':[{'symbol':intent.symbol,'instrument_type':'Equity Option','action':'Buy to Open' if intent.action=='OPEN' else 'Sell to Close','quantity':intent.quantity}]}
            self.journal.record('ORDER_REQUEST',{k:v for k,v in args.items() if k!='account_number'},intent.intent_id)
            preview=self._read_with_recovery(lambda:self._call('tastytrade_dry_run_order',args))
            if preview.get('environment')!='sandbox':raise ReconciliationError('Dry-run environment mismatch')
            upstream=preview.get('upstream',{})
            token=preview.get('confirmation_token')
            self.journal.record('DRY_RUN',{'environment':preview.get('environment'),'order':curated_order(upstream.get('order',{})),
                'errors_present':bool(upstream.get('errors')),'buying-power-effect':upstream.get('buying-power-effect')},intent.intent_id)
            if not isinstance(token,str) or not token or upstream.get('errors'):raise ReconciliationError('Dry-run rejected')
            if generation!=self.recovery_generation or self.broker_status!='BROKER_READY':
                del token
                continue
            # Always refresh AFTER preview: all calculations/approval below use
            # this exact new snapshot, without another intervening broker read.
            fresh()
            if generation!=self.recovery_generation:
                del token
                continue
            if quote.bid is None or quote.ask is None:raise RiskRejected('QUOTE_MISSING')
            if not decimal(quote.bid)<=decimal(intent.limit_price)<=decimal(quote.ask):
                self.journal.record('REPRICE_REQUIRED',{'old_limit':intent.limit_price,'new_bid':quote.bid,'new_ask':quote.ask},intent.intent_id)
                del token
                continue # New limit needs a new official preview/confirmation.
            try:decision=self.gate.check(intent,state,quote,underlying,seen=self.journal.seen(intent.intent_id))
            except RiskRejected as exc:
                if str(exc) in ('STALE_QUOTE','STALE_ACCOUNT_STATE') and refresh is not None:
                    del token
                    continue
                raise
            approval=self._approval_version(intent,state,quote,underlying)
            self.journal.record('RISK_APPROVED',dict(decision,approval_version=approval,snapshot_version=self.snapshot_version,
                broker_generation=generation,quote=asdict(quote),underlying=asdict(underlying),intent=asdict(intent)),intent.intent_id)
            self.assert_destination();self._require_ready()
            if approval!=self._approval_version(intent,state,quote,underlying):
                raise RiskRejected('AUTHORIZATION_INVALIDATED')
            self.gate.check(intent,state,quote,underlying,seen=self.journal.seen(intent.intent_id))
            break
        else:raise RiskRejected('Fresh quote/preview authorization could not stabilize within five preparations')
        self.journal.reserve(intent.intent_id) # Durable before any non-idempotent request.
        try:
            result=self._call('tastytrade_place_order',dict(args,confirmation_token=token))
            del token
            if result.get('environment')!='sandbox':raise ReconciliationError('Submit environment mismatch')
            order=result.get('upstream',{}).get('order',{})
            order_id=order.get('id')
            if not order_id:raise ReconciliationError('Submission missing broker ID')
            self.journal.status(intent.intent_id,'WORKING',str(order_id))
            self.journal.record('SUBMITTED',curated_order(order),intent.intent_id)
            result=self.reconcile(intent,str(order_id),state)
            result['market_reference']={'symbol':intent.symbol,'bid':quote.bid,'ask':quote.ask,'mid':(quote.bid+quote.ask)/2,'limit':intent.limit_price,'snapshot_version':self.snapshot_version,'timestamps':quote.event_timestamps}
            return result
        except BaseException:
            self.journal.status(intent.intent_id,'UNKNOWN')
            # Never repeat a write. Inspect broker history, but absence from one
            # response is not proof of nonacceptance (or permission to resubmit).
            try:
                history=rows(self._read_with_recovery(lambda:self._call('tastytrade_get_orders',{'account_number':self.account})))
                candidates=[curated_order(o) for o in history if any(l.get('symbol')==intent.symbol for l in o.get('legs',[]))]
                self.journal.record('AMBIGUOUS_SUBMISSION_RECONCILIATION',{'candidate_orders':candidates,'resubmitted':False},intent.intent_id)
            except Exception:
                self.journal.record('AMBIGUOUS_SUBMISSION_RECONCILIATION',{'read_succeeded':False,'resubmitted':False},intent.intent_id)
            raise

    def reconcile(self,intent,order_id,before):
        order=self._order(order_id)
        self.state() # Re-read positions/working orders after submission.
        if order.get('status') not in ('Filled','Cancelled','Canceled','Rejected','Expired'):
            # Cancel only this session's just-created order, never a protective/foreign order.
            self.assert_destination()
            self._call('tastytrade_cancel_order',{'account_number':self.account,'order_id':order_id})
            for _ in range(3):
                time.sleep(1)
                order=self._order(order_id)
                if order.get('status') in ('Filled','Cancelled','Canceled','Rejected','Expired'):break
        status=order.get('status')
        if status not in ('Filled','Cancelled','Canceled','Rejected','Expired'):raise ReconciliationError('Order not terminal after cancellation')
        legs=order.get('legs',[])
        if len(legs)!=1 or legs[0].get('symbol')!=intent.symbol or decimal(legs[0].get('quantity'))!=intent.quantity:
            raise ReconciliationError('Broker legs mismatch')
        fills=legs[0].get('fills',[])
        qty=sum((decimal(f['quantity']) for f in fills),Decimal(0))
        if qty<0 or qty>intent.quantity or qty!=qty.to_integral_value():raise ReconciliationError('Fill quantity invalid')
        if status=='Filled' and qty!=intent.quantity:raise ReconciliationError('Filled status not supported by fill records')
        if any(decimal(f['fill-price'])<=0 for f in fills):raise ReconciliationError('Fill price invalid')
        price=sum((decimal(f['fill-price'])*decimal(f['quantity']) for f in fills),Decimal(0))/qty if qty else None
        self.journal.record('BROKER_FILL_OBSERVED',{'order_id':order_id,'status':status,'filled_quantity':int(qty),'fill_price':str(price) if price is not None else None,'position_reconciled':False},intent.intent_id)
        if qty and intent.action=='OPEN':self.agent_owned.add(intent.symbol)
        after=self.state()
        expected=before['owned'].get(intent.symbol,0)+(int(qty) if intent.action=='OPEN' else -int(qty))
        for delay in (2,5,10,20,30):
            if after['owned'].get(intent.symbol,0)==expected:break
            self.journal.record('POSITION_RECONCILIATION_PENDING',{'order_id':order_id,'expected_quantity':expected,'observed_quantity':after['owned'].get(intent.symbol,0),'retry_after_seconds':delay},intent.intent_id)
            time.sleep(delay)
            after=self.state()
        if after['owned'].get(intent.symbol,0)!=expected:raise ReconciliationError('Position does not reconcile to confirmed fills after bounded polling')
        if after['working_orders']:raise ReconciliationError('Working orders remain after reconciliation')
        self.journal.record('BROKER_CONFIRMED',{'order':curated_order(order),'filled_quantity':int(qty),
            'fill_price':str(price) if price is not None else None,'synthetic_sandbox_fill':True},intent.intent_id)
        self.journal.status(intent.intent_id,'RECONCILED',order_id)
        self.broker_status='BROKER_READY'
        self.journal.record('BROKER_STATE',{'state':'BROKER_READY','portfolio_reconciled':True})
        return {'order_id':order_id,'status':status,'filled_quantity':int(qty),'fill_price':str(price) if price else None,'portfolio':after}
