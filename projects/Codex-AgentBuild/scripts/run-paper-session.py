#!/usr/bin/env python3
"""One execution proof plus three bounded strategy/management observations."""
import json, subprocess, sys, time, uuid, selectors, os
from pathlib import Path
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.broker.process_cleanup import cleanup_stale_sandbox_wrappers
from src.broker.paper_execution import PaperExecution,ReconciliationError
from src.broker.sandbox_inspection import PreflightError
from src.risk.paper import RiskGate,TradeIntent,RiskRejected,decimal
from src.journal.paper import PaperJournal
from src.data.market_data.models import MUOptionContract,MUOptionSnapshot,MUUnderlyingSnapshot
ROOT=Path(__file__).resolve().parents[1]

def emit(kind,data):print(json.dumps({'event':kind,'data':data},allow_nan=False),flush=True)

class MarketWorker:
    def __init__(self):
        self.process=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/paper-market-snapshot.py')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,
            env={'PATH':'/usr/bin:/bin','HOME':str(Path.home())})
        self.buffer=b''
    def snapshot(self,held=(),selected=None):
        self.process.stdin.write((json.dumps({'held':list(held),'selected':selected})+'\n').encode())
        deadline=time.monotonic()+180
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout,selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                if not selector.select(max(0,deadline-time.monotonic())):raise ReconciliationError('Market-data worker timed out')
                chunk=os.read(self.process.stdout.fileno(),65536)
                if not chunk:raise ReconciliationError('Market-data worker disconnected')
                self.buffer+=chunk
                if len(self.buffer)>4*1024*1024:raise ReconciliationError('Market-data response too large')
        line,self.buffer=self.buffer.split(b'\n',1)
        data=json.loads(line)
        if 'error' in data:raise ReconciliationError('Production market-data unavailable')
        catalog=[MUOptionContract(**c) for c in data['catalog']]
        underlying=MUUnderlyingSnapshot(**data['underlying'])
        options=[]
        for item in data['options']:
            item['contract']=MUOptionContract(**item['contract']);options.append(MUOptionSnapshot(**item))
        return catalog,underlying,options
    def fresh(self,symbol):
        _,u,quotes=self.snapshot(selected=[symbol])
        quote=next((q for q in quotes if q.contract.symbol==symbol),None)
        if quote is None:raise ReconciliationError('Selected contract quote missing')
        return quote,u
    def close(self):
        self.process.terminate()
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        self.process.stdin.close();self.process.stdout.close()


def limit(quote,opening=True):
    # Cross no farther than the displayed market; penny rounding only.
    price=decimal(quote.ask if opening else quote.bid)
    return str(price.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))

def proposal(quote,rationale,action='OPEN',quantity=1):
    return TradeIntent(str(uuid.uuid4()),quote.contract.symbol,action,quantity,limit(quote,action=='OPEN'),rationale)

def main():
    journal=PaperJournal(ROOT/'logs/paper-session.sqlite3')
    worker=MarketWorker()
    market=worker.snapshot
    started=time.monotonic();owned=set();entry={};decisions=[];market_realized=Decimal(0);market_marks={}
    try:
        emit('STALE_SANDBOX_PROCESSES_TERMINATED',cleanup_stale_sandbox_wrappers())
        if journal.unresolved():raise ReconciliationError('Prior unresolved intent requires reconciliation; no new orders')
        def degraded_market():
            try:
                _,u,qs=market()
                journal.record('DEGRADED_MARKET_DATA',{'underlying':asdict(u),'options':[asdict(q) for q in qs]})
                emit('BROKER_DEGRADED_MARKET_DATA',{'MU_bid':u.bid,'MU_ask':u.ask,'option_snapshots':len(qs)})
            except Exception:journal.record('DEGRADED_MARKET_DATA',{'available':False})
        with PaperExecution('sandbox',journal,RiskGate([],ROOT/'config/PAPER_KILL_SWITCH')) as inspection:
            inspection.degraded_hook=degraded_market
            starting=inspection.state()
            emit('STARTING_PORTFOLIO',starting)
            emit('BROKER_READY',{'process_id':inspection.process.pid,'lifecycle':inspection.lifecycle})
            if starting['working_orders'] or starting['owned']:raise ReconciliationError('First-session baseline must be empty; foreign exposure not managed')
        with PaperExecution('sandbox',journal,RiskGate([],ROOT/'config/PAPER_KILL_SWITCH'),trading=True) as broker:
            broker.degraded_hook=degraded_market
            state=broker.state()
            catalog,underlying,options=market()
            gate=RiskGate(catalog,ROOT/'config/PAPER_KILL_SWITCH');broker.gate=gate
            ranked=sorted(options,key=lambda q:((q.ask-q.bid)/q.mid if q.mid and q.bid is not None and q.ask is not None else float('inf'),-(q.volume or 0)))
            chosen=None;reasons=[]
            for q in ranked:
                intent=proposal(q,'Quantity-one execution plumbing proof; not a directional investment thesis')
                try:gate.check(intent,state,q,underlying)
                except (ValueError,TypeError,KeyError) as e:
                    reasons.append(str(e) if isinstance(e,RiskRejected) else 'INVALID_MARKET');continue
                chosen=(q,intent);break
            if chosen is None:
                journal.record('NO_TRADE',{'reason':'No option passes current-market hard checks','rejections':reasons})
                emit('NO_TRADE',{'reason':'No option passes hard checks','rejections':reasons})
                raise ReconciliationError('Execution proof blocked by unusable market data or risk checks')
            q,intent=chosen
            proof=broker.execute(intent,q,underlying,refresh=worker.fresh)
            emit('EXECUTION_PROOF',proof)
            if proof['filled_quantity']:
                owned.add(q.contract.symbol);entry[q.contract.symbol]={'market_entry_mid':proof['market_reference']['mid'],'sandbox_entry_price':proof['fill_price'],'quantity':proof['filled_quantity']}
            # Three reassessments, at most two additional quantity-one openings; no daemon.
            started=time.monotonic()
            last_spot=underlying.mid;opens=0
            for cycle in range(3):
                if time.monotonic()-started>480:break
                time.sleep(5)
                state=broker.state()
                catalog,underlying,options=market(owned)
                broker.gate=RiskGate(catalog,ROOT/'config/PAPER_KILL_SWITCH')
                optionmap={q.contract.symbol:q for q in options}
                acted=False
                for symbol in list(owned):
                    held=state['owned'].get(symbol,0)
                    if not held:owned.remove(symbol);continue
                    q=optionmap.get(symbol)
                    if q is None:raise ReconciliationError('Held option missing refreshed market')
                    market_return=(q.mid-entry[symbol]['market_entry_mid'])/entry[symbol]['market_entry_mid'] if q.mid else None
                    market_marks[symbol]=q.mid
                    action='HOLD'
                    if market_return is not None and abs(market_return)>=.10:action='CLOSE'
                    rationale='Market midpoint move reached 10% management threshold' if action=='CLOSE' else 'No management threshold reached during bounded observation'
                    journal.record('MANAGEMENT',{'symbol':symbol,'action':action,'rationale':rationale,'market_return':market_return,'quote':asdict(q)})
                    if action=='CLOSE':
                        result=broker.execute(proposal(q,rationale,'CLOSE',held),q,underlying,refresh=worker.fresh);emit('MANAGEMENT_EXECUTION',result)
                        if result['filled_quantity']:
                            market_realized+=(decimal(q.mid)-decimal(entry[symbol]['market_entry_mid']))*100*result['filled_quantity']
                        if result['portfolio']['owned'].get(symbol,0)==0:owned.remove(symbol)
                        acted=True
                    decisions.append({'cycle':cycle,'action':action,'symbol':symbol,'rationale':rationale})
                change=(underlying.mid-last_spot)/last_spot if underlying.mid and last_spot else 0
                # Require a measured directional move, meaningful volume and liquid Greek exposure.
                candidates=[q for q in options if q.contract.symbol not in owned and q.delta is not None and
                    .30<=abs(q.delta)<=.60 and (q.volume or 0)>=100 and q.mid and
                    (q.ask-q.bid)/q.mid<=.10 and (q.contract.option_type=='C' if change>0 else q.contract.option_type=='P')]
                if not acted and abs(change)>=.002 and opens<2 and candidates:
                    q=candidates[0];intent=proposal(q,'Observed MU momentum >=0.2%; liquid 30–60 delta option, quantity one')
                    try:broker.gate.check(intent,state,q,underlying)
                    except (ValueError,TypeError,KeyError):candidates=[]
                    if candidates:
                        result=broker.execute(intent,q,underlying,refresh=worker.fresh);emit('AUTONOMOUS_EXECUTION',result);opens+=1
                        if result['filled_quantity']:
                            owned.add(q.contract.symbol);entry[q.contract.symbol]={'market_entry_mid':result['market_reference']['mid'],'sandbox_entry_price':result['fill_price'],'quantity':result['filled_quantity']}
                        decisions.append({'cycle':cycle,'action':'OPEN','symbol':q.contract.symbol});acted=True
                if not acted:
                    decision={'cycle':cycle,'action':'NO_TRADE','rationale':'No qualifying fresh directional setup; deployment target does not compel a trade'}
                    decisions.append(decision);journal.record('STRATEGY_DECISION',decision)
                last_spot=underlying.mid
                emit('SESSION_DECISIONS',decisions[-3:])
            final=broker.state();equity=decimal(final['equity'])
            journal.record('SESSION_COMPLETE',{'starting':starting,'final':final,'decisions':decisions})
            emit('FINAL',{'portfolio':final,'capital_deployed_percent':str(decimal(final['deployed'])/equity*100),
                'reserve_percent':str(decimal(final['cash'])/equity*100),'maximum_defined_loss':final['maximum_loss'],
                'sandbox_equity_change':str(equity-decimal(starting['equity'])),
                'market_referenced_realized_pnl':str(market_realized),
                'market_referenced_unrealized_pnl':str(sum(((decimal(market_marks[s])-decimal(entry[s]['market_entry_mid']))*100*final['owned'].get(s,0) for s in owned if s in market_marks),Decimal(0))) if all(s in market_marks for s in owned) else None,
                'journal_records':journal.count(),'decisions':decisions})
    except Exception as exc:
        # Only fixed authored exceptions can reach stdout. Never dump MCP bodies/credentials.
        reason=str(exc) if isinstance(exc,(RiskRejected,ReconciliationError,PreflightError)) else type(exc).__name__
        lifecycle=[]
        for name in ('inspection','broker'):
            obj=locals().get(name)
            if obj is not None:lifecycle.extend(obj.lifecycle)
        journal.record('SESSION_STOP',{'reason':reason,'lifecycle':lifecycle});emit('STOP',{'reason':reason,'lifecycle':lifecycle,'journal_records':journal.count()})
        return 1
    finally:worker.close();journal.close()
    return 0
if __name__=='__main__':raise SystemExit(main())
