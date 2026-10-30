import unittest
import tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from unittest.mock import Mock,patch
from src.data.market_data.models import MUOptionContract,MUOptionSnapshot,MUUnderlyingSnapshot
from src.risk.paper import RiskGate,TradeIntent,RiskRejected
from src.journal.paper import PaperJournal
from src.broker.paper_execution import PaperExecution,ReconciliationError,BrokerDegraded

class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        sleeper=patch('src.broker.paper_execution.time.sleep');sleeper.start();self.addCleanup(sleeper.stop)
        self.now=datetime.now(timezone.utc);expiry=(self.now+timedelta(days=10)).strftime('%Y-%m-%d');occ=(self.now+timedelta(days=10)).strftime('%y%m%d')
        self.contract=MUOptionContract('MU','MU    '+occ+'C00100000','.MU'+occ+'C100',expiry,100,'C')
        self.gate=RiskGate([self.contract],Path(self.tmp.name)/'KILL')
        self.intent=TradeIntent('fake-intent',self.contract.symbol,'OPEN',1,'1.00','Plumbing test')
        stamp=self.now.isoformat()
        self.quote=MUOptionSnapshot(contract=self.contract,bid=.95,ask=1.05,bid_size=10,ask_size=10,
            received_timestamps={'Quote':stamp},event_timestamps={'Quote':stamp})
        self.underlying=MUUnderlyingSnapshot(bid=100,ask=100.1,received_timestamps={'Quote':stamp},event_timestamps={'Quote':stamp})
        self.state={'environment':'sandbox','timestamp':stamp,'equity':'10000','cash':'10000','buying_power':'10000',
            'deployed':'0','maximum_loss':'0','owned':{},'agent_owned':[],'working_orders':[]}
        self.journal=PaperJournal(Path(self.tmp.name)/'paper.sqlite3');self.addCleanup(self.journal.close)

    def check(self,intent=None,state=None,quote=None,**kw):
        return self.gate.check(intent or self.intent,state or self.state,quote or self.quote,self.underlying,now=self.now,**kw)
    def test_long_maximum_loss(self):self.assertEqual(self.check()['maximum_position_loss'],'102.00')
    def test_forbidden_intents(self):
        for changes in ({'environment':'production'},{'underlying':'AAPL'},{'underlying':'NVDA'},{'underlying':'SPY'},{'underlying':'MSFT'},
                        {'symbol':'UNKNOWN'},{'order_type':'Market'},{'quantity':0},{'quantity':True},{'quantity':-1},{'action':'SELL_SHORT'},
                        {'limit_price':'NaN'},{'limit_price':'0'},{'limit_price':'100'},{'rationale':''}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.check(replace(self.intent,**changes))
    def test_stale_quote(self):
        q=replace(self.quote,received_timestamps={'Quote':(self.now-timedelta(minutes=2)).isoformat()})
        with self.assertRaises(RiskRejected):self.check(quote=q)
    def test_unknown_event_time_needs_live_trade(self):
        with self.assertRaises(RiskRejected):self.check(quote=replace(self.quote,event_timestamps={}))
    def test_duplicate(self):
        with self.assertRaises(RiskRejected):self.check(seen=True)
    def test_kill_switch(self):
        self.gate.kill_switch.touch()
        with self.assertRaises(RiskRejected):self.check()
    def test_capital_and_reserve(self):
        for changes in ({'deployed':'6999'},{'cash':'3000'},{'buying_power':'1'},{'working_orders':[{}]},{'environment':'production'}):
            with self.subTest(changes=changes),self.assertRaises(RiskRejected):self.check(state=dict(self.state,**changes))
        self.assertTrue(self.check(state=dict(self.state,deployed='5000',cash='5000'))['approved'])
    def test_close_never_shorts(self):
        close=replace(self.intent,action='CLOSE')
        with self.assertRaises(RiskRejected):self.check(close)
        held=dict(self.state,owned={self.contract.symbol:2},agent_owned=[self.contract.symbol])
        self.assertTrue(self.check(replace(close,action='PARTIAL_CLOSE'),state=held)['approved'])
        with self.assertRaises(RiskRejected):self.check(replace(close,quantity=3),state=held)
    def test_expiry_and_catalog_identity(self):
        with self.assertRaises(RiskRejected):self.gate.check(self.intent,self.state,self.quote,self.underlying,now=self.now+timedelta(days=20))
        with self.assertRaises(RiskRejected):self.check(quote=replace(self.quote,contract=None))
    def test_persistent_duplicate_and_uncertain_block(self):
        self.journal.reserve('one');self.assertTrue(self.journal.seen('one'));self.assertTrue(self.journal.unresolved())
        with self.assertRaises(ValueError):self.journal.reserve('two')
        self.journal.status('one','RECONCILED')
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.journal.reserve('one')
    def test_journal_does_not_persist_tokens(self):
        self.journal.record('TEST',{'confirmation_token':'fake-confirm','nested':{'access_token':'fake-access'},'bid':1})
        payload=self.journal.db.execute('SELECT payload FROM events').fetchone()[0]
        self.assertNotIn('fake-',payload)
    def execution(self):
        e=PaperExecution('sandbox',self.journal,self.gate,trading=True);e.sandbox_verified=True;e.account='fake-account'
        e.broker_status='BROKER_READY'
        e.audit=[{'kind':'environment','environment':'sandbox','destination':'https://api.cert.tastyworks.com','read_only':'0'}]
        return e
    def test_runtime_destination_guard(self):
        e=self.execution();e.audit[0]['destination']='https://api.tastyworks.com'
        with self.assertRaises(ReconciliationError):e.assert_destination()
    def test_end_to_end_confirmed_fill(self):
        e=self.execution()
        order={'id':123,'status':'Filled','legs':[{'symbol':self.contract.symbol,'quantity':1,'fills':[{'quantity':1,'fill-price':'1.00'}]}]}
        after=dict(self.state,owned={self.contract.symbol:1})
        e.state=Mock(side_effect=[self.state,self.state,after,after])
        e._order=Mock(return_value=order)
        e._call=Mock(side_effect=[{'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{'order':{}}},
                                  {'environment':'sandbox','upstream':{'order':order}}])
        result=e.execute(self.intent,self.quote,self.underlying)
        self.assertEqual(result['filled_quantity'],1);self.assertEqual(result['fill_price'],'1.00')
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(e._call.call_args_list[1].args[0],'tastytrade_place_order')
    def test_submission_is_not_a_fill(self):
        e=self.execution();order={'id':123,'status':'Filled','legs':[{'symbol':self.contract.symbol,'quantity':1,'fills':[]}]}
        e._order=Mock(return_value=order);e.state=Mock(return_value=self.state)
        with self.assertRaises(ReconciliationError):e.reconcile(self.intent,'123',self.state)
    def test_timeout_never_resubmits(self):
        e=self.execution();e.state=Mock(return_value=self.state)
        e._call=Mock(side_effect=[{'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{'order':{}}},TimeoutError()])
        with self.assertRaises(TimeoutError):e.execute(self.intent,self.quote,self.underlying)
        self.assertEqual(sum(c.args[0]=='tastytrade_place_order' for c in e._call.call_args_list),1);self.assertTrue(self.journal.unresolved())

    def test_read_401_recovers_exactly_once(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();e._verify_current_credentials=Mock()
        operation=Mock(side_effect=[PreflightError('MCP_auth_failed; HTTP_STATUS=401'),{'items':[]}])
        self.assertEqual(e._read_with_recovery(operation),{'items':[]})
        self.assertEqual(operation.call_count,2);e._verify_current_credentials.assert_called_once()
        self.assertIn({'recovery':'succeeded'},e.lifecycle)

    def test_repeated_401_exhausts_bounded_recovery(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();e._verify_current_credentials=Mock()
        operation=Mock(side_effect=PreflightError('MCP_auth_failed; HTTP_STATUS=401'))
        with self.assertRaises(BrokerDegraded):e._read_with_recovery(operation)
        self.assertEqual(operation.call_count,6)

    def test_oauth_transient_during_401_recovery_can_recover(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();e._verify_current_credentials=Mock()
        operation=Mock(side_effect=[PreflightError('MCP_auth_failed; HTTP_STATUS=401'),PreflightError('MCP_upstream_error; HTTP_STATUS=502'),{'items':[]}])
        self.assertEqual(e._read_with_recovery(operation),{'items':[]})
        self.assertEqual(operation.call_count,3)

    def test_auth_recovery_never_retries_submit(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();e.state=Mock(return_value=self.state)
        e._call=Mock(side_effect=[{'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{'order':{}}},
                                PreflightError('MCP_auth_failed; HTTP_STATUS=401'),{'items':[]}])
        with self.assertRaises(PreflightError):e.execute(self.intent,self.quote,self.underlying)
        self.assertEqual(sum(c.args[0]=='tastytrade_place_order' for c in e._call.call_args_list),1)
        self.assertEqual(e._call.call_args_list[-1].args[0],'tastytrade_get_orders')
        self.assertTrue(self.journal.unresolved())

    def test_read_transport_retry_is_bounded(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();operation=Mock(side_effect=PreflightError('MCP_network; HTTP_STATUS=unknown'))
        with patch('src.broker.paper_execution.time.sleep'),self.assertRaises(BrokerDegraded):
            e._read_with_recovery(operation)
        self.assertEqual(operation.call_count,6)

    def test_retry_after_and_backoff(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution()
        operation=Mock(side_effect=[PreflightError('MCP_rate_limit_exceeded; HTTP_STATUS=429',7),{}])
        with patch('src.broker.paper_execution.time.sleep') as sleep:
            e._read_with_recovery(operation)
        sleep.assert_called_once_with(7)
        self.assertEqual(e.broker_status,'BROKER_DEGRADED') # A single successful read is not portfolio reconciliation.

    def test_permanent_oauth_not_retried(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution()
        def rejection():
            e.lifecycle.append({'phase':'oauth','status':400})
            raise PreflightError('MCP_auth_failed; HTTP_STATUS=400')
        operation=Mock(side_effect=rejection)
        with self.assertRaises(PreflightError):e._read_with_recovery(operation)
        self.assertEqual(operation.call_count,1)

    def test_degraded_disables_submission_and_respects_cooldown(self):
        e=self.execution();e._degrade('test')
        with self.assertRaises(BrokerDegraded):e._require_ready()
        with self.assertRaises(BrokerDegraded):e.probe()
        e.state=Mock(return_value={});e.retry_not_before=0
        e.probe();e.state.assert_called_once()

    def test_exact_backoff_budget_and_market_hook(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();e.degraded_hook=Mock()
        operation=Mock(side_effect=PreflightError('MCP_upstream_error; HTTP_STATUS=503'))
        with patch('src.broker.paper_execution.time.sleep') as sleep,self.assertRaises(BrokerDegraded):
            e._read_with_recovery(operation)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[2,5,10,20,30])
        e.degraded_hook.assert_called_once()

    def test_recovery_discards_preview_and_refreshes_again(self):
        e=self.execution();e.state=Mock(return_value=self.state)
        order={'id':123,'status':'Filled','legs':[{'symbol':self.contract.symbol,'quantity':1,'fills':[{'quantity':1,'fill-price':'1.00'}]}]}
        previews=0
        def call(name,args):
            nonlocal previews
            if name=='tastytrade_dry_run_order':
                previews+=1
                if previews==1:e.recovery_generation+=1
                return {'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{}}
            return {'environment':'sandbox','upstream':{'order':order}}
        e._call=Mock(side_effect=call)
        e.reconcile=Mock(return_value={'filled_quantity':1})
        fresh=Mock(return_value=(self.quote,self.underlying))
        e.execute(self.intent,self.quote,self.underlying,refresh=fresh)
        self.assertEqual(previews,2)
        self.assertEqual(fresh.call_count,3)
        self.assertEqual(sum(c.args[0]=='tastytrade_place_order' for c in e._call.call_args_list),1)

    def test_repriced_quote_requires_new_preview(self):
        e=self.execution();e.state=Mock(return_value=self.state)
        moved=replace(self.quote,bid=1.15,ask=1.25)
        fresh=Mock(side_effect=[(self.quote,self.underlying),(moved,self.underlying),(moved,self.underlying),(moved,self.underlying)])
        e._call=Mock(side_effect=[{'environment':'sandbox','confirmation_token':'first','upstream':{}},
            {'environment':'sandbox','confirmation_token':'second','upstream':{}},
            {'environment':'sandbox','upstream':{'order':{'id':123}}}])
        e.reconcile=Mock(return_value={})
        result=e.execute(self.intent,self.quote,self.underlying,refresh=fresh)
        self.assertEqual(e._call.call_args_list[0].args[1]['price'],'1.05')
        self.assertEqual(e._call.call_args_list[1].args[1]['price'],'1.25')
        self.assertEqual(e._call.call_args_list[2].args[1]['confirmation_token'],'second')
        self.assertEqual(result['market_reference']['mid'],1.2)

    def test_snapshot_mutation_invalidates_approval_before_submit(self):
        e=self.execution();e.state=Mock(return_value=self.state)
        e._call=Mock(return_value={'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{}})
        original_record=self.journal.record
        def record(kind,payload,intent_id=None):
            original_record(kind,payload,intent_id)
            if kind=='RISK_APPROVED':e.snapshot_version+=1
        with patch.object(self.journal,'record',side_effect=record),self.assertRaisesRegex(RiskRejected,'AUTHORIZATION_INVALIDATED'):
            e.execute(self.intent,self.quote,self.underlying,refresh=lambda symbol:(self.quote,self.underlying))
        self.assertEqual(e._call.call_count,1)
        self.assertFalse(self.journal.seen(self.intent.intent_id))

    def test_initial_recovery_precedes_market_refresh(self):
        e=self.execution();calls=[]
        def state():
            calls.append('broker');e.recovery_generation+=1;return self.state
        e.state=state
        def fresh(symbol):calls.append('market');return self.quote,self.underlying
        e._call=Mock(side_effect=[{'environment':'sandbox','confirmation_token':'fake-confirm','upstream':{}},
                                 {'environment':'sandbox','upstream':{'order':{'id':123}}}])
        e.reconcile=Mock(return_value={})
        e.execute(self.intent,self.quote,self.underlying,refresh=fresh)
        self.assertEqual(calls,['broker','market','market'])

    def test_server_500_read_can_recover(self):
        from src.broker.sandbox_inspection import PreflightError
        e=self.execution();operation=Mock(side_effect=[PreflightError('MCP_upstream_error; HTTP_STATUS=500'),{}])
        self.assertEqual(e._read_with_recovery(operation),{})
        self.assertEqual(operation.call_count,2)

    def test_today_order_feed_filters_terminal_orders(self):
        e=self.execution()
        e.read=Mock(side_effect=[{'items':[{'account':{'account-number':'fake-account'}}]},
            {'items':[{'currency':'USD','net-liquidating-value':'10000','cash-balance':'10000','derivative-buying-power':'10000'}]},
            {'items':[]},{'items':[{'id':123,'status':'Filled','legs':[]}]}])
        state=e.state()
        self.assertEqual(state['working_orders'],[])
        self.assertEqual(state['today_orders'][0]['status'],'Filled')

    def test_fill_position_can_reconcile_after_bounded_poll(self):
        e=self.execution()
        order={'id':123,'status':'Filled','legs':[{'symbol':self.contract.symbol,'quantity':1,'fills':[{'quantity':1,'fill-price':'1.00'}]}]}
        e._order=Mock(return_value=order)
        after=dict(self.state,owned={self.contract.symbol:1})
        e.state=Mock(side_effect=[self.state,self.state,after])
        self.journal.reserve(self.intent.intent_id)
        result=e.reconcile(self.intent,'123',self.state)
        self.assertEqual(result['filled_quantity'],1)
        self.assertEqual(e.state.call_count,3)
