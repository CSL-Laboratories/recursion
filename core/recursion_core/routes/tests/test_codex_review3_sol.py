"""Codex GPT-6.1 Sol's round-3 red tests for the Codex driver (lane DRIVER-CX keeps them).

Copied unchanged from review-3-402-gpt-6.1-sol-evidence/test_third_read.py in the lane's folder, the read of draft
#402 at 4b84bc47; the tests of its five required changes failed there and pass at the answer commit. The class
ConcurrentParse at the end is the read's supplemental test_parallel_parse.py, which failed there too (its import
of test_third_read and the turn and done helpers made local: ThirdRead is in this file). Left out:
test_environment.py (it tests child_env and the daemon's launcher, other lanes' code; its results are quoted in
the lane's report). Fakes only: no real codex, no credential, no network.
"""

"""Third read adversarial probes. All processes and credentials are synthetic."""
import argparse, contextlib, io, json, tempfile, unittest
from unittest.mock import patch
from recursion_core.routes import contract as c
from recursion_core.routes.driver import DriverStateError
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import AppServer, Launcher, FakeApprovals, begin, record, message, ask_command, done, turn, answers
from recursion_core.routes.tests.test_codex_attended import tool, LateTyping

class ThirdRead(unittest.TestCase):
    def open(self, pending=False, port=None):
        self.s=AppServer(silent_turns=pending)
        self.p=port or FakeApprovals()
        self.d=cx.CodexDriver(Launcher(self.s), FakeClock(), approvals=self.p)
        self.h=begin(self.d,record(),{})
        self.d.send(self.h,message())
        self.rid=self.s.sent[-1]['id']
        while self.s.out: self.d.pump(self.h,0)
        if pending: self.feed({'method':'turn/started','params':{'threadId':'thr-1','turn':turn('trn-1')}})
    def feed(self,obj): return self.d.parse(self.h,json.dumps(obj))
    def ask(self): return self.feed(ask_command()('trn-1'))
    def answer(self): return self.d.send(self.h,{'kind':'approval_answer','approval_id':'apr-1','decision':'approve'})
    def test_A1_S1_deferred_end_does_not_resurrect_on_midturn_message(self):
        self.open(pending=True)
        self.feed(done()('trn-1'))
        before=[x for x in self.s.sent if x.get('method')=='turn/start']
        try: self.d.send(self.h,message(delivery='dlv-2'))
        except DriverStateError: pass
        self.assertEqual([x for x in self.s.sent if x.get('method')=='turn/start'],before,'an ended turn must not launch another turn while its acceptance is deferred')
    def test_approval_resolved_while_port_returns_is_unanswerable(self):
        outer=self
        class ResolvingPort(FakeApprovals):
            def request(self,effect,binding):
                aid=super().request(effect,binding)
                outer.feed({'method':'serverRequest/resolved','params':{'threadId':'thr-1','requestId':77}})
                return aid
        self.open(port=ResolvingPort())
        self.ask()
        with self.assertRaises(DriverStateError): self.answer()
        self.assertEqual(answers(self.s,77),[])
    def test_A1_end_before_port_publication_opens_no_approval(self):
        self.open()
        original=cx._effect
        def ended(*a):
            effect=original(*a)
            self.feed(done()('trn-1'))
            return effect
        with patch.object(cx,'_effect',side_effect=ended): self.ask()
        self.assertEqual(self.p.requests,[],'turn completed before request reached the port')
    def test_S4_refusal_drops_unfinished_calls_and_all_turn_tables(self):
        self.open(pending=True)
        self.feed({'method':'item/started','params':{'threadId':'thr-1','turnId':'trn-1','item':{'type':'commandExecution','id':'incomplete','command':'pwd','cwd':'/w'}}})
        self.ask()
        self.feed({'id':self.rid,'error':{'code':-32000,'message':'synthetic'}})
        s=self.h._codex
        self.assertEqual((s.calls,s.items,s.bindings,s.seen_requests,s.engine_turn,s.core_turn),(set(),{},{},set(),{},{}))
    def test_S5_foreign_message_does_not_reserve_delivery(self):
        self.open()
        other=cx.CodexDriver(Launcher(),FakeClock(),approvals=FakeApprovals())
        with self.assertRaises(DriverStateError): other.send(self.h,message(delivery='dlv-2'))
        self.d.send(self.h,message(delivery='dlv-2'))
        self.assertEqual(self.s.sent[-1]['method'],'turn/steer')
    def test_A5_typed_unknown_and_tagged_resume_errors_keep_engine_error(self):
        for info in ('other',{'httpConnectionFailed':{'httpStatusCode':503}}):
            s=AppServer(refuse={'thread/resume':{'code':-32000,'message':'synthetic','data':{'codexErrorInfo':info}}})
            with self.assertRaises(c.RouteError) as caught: begin(cx.CodexDriver(Launcher(s),FakeClock()),record('thr-1'),{})
            self.assertEqual(caught.exception.code,c.ENGINE_ERROR)
    def test_A6_nested_surrogate_infinity_still_drafts_pair(self):
        self.open()
        item={'type':'dynamicToolCall','id':'nested','tool':'synthetic','arguments':{'\udfff':[float('inf'),'\ud800']},'status':'completed'}
        ds=self.d._tool_call(self.h._codex,item)+self.d._tool_result(self.h._codex,item)
        self.assertEqual([d['type'] for d in ds],['tool.call','tool.result'])
        self.assertIsNone(ds[0]['payload']['input'])
    def test_A3_S2_held_resume_cannot_hide_quit_while_approval_waits(self):
        m=tool(); s=AppServer(script={1:[ask_command()]}); said=[]
        console=m.Console(said.append); d=cx.CodexDriver(Launcher(s),FakeClock(),approvals=console)
        class Source:
            def __init__(self): self.commands=iter(['hello','/resume','/quit']); self.count=0
            def __iter__(self): return self
            def __next__(self): self.count+=1; return next(self.commands)
            def ready(self): return self.count<2 or not s.out
        source=Source(); original=m.takes_now; calls=[]
        def bounded(line,waiting):
            calls.append(line)
            if len(calls)>20: raise AssertionError('held /resume loops without reading /quit')
            return original(line,waiting)
        with patch.object(m,'takes_now',side_effect=bounded): m.session_loop(d,source,said.append,console,env={},read_s=0)
        self.assertTrue(s.stdin_closed)
    def test_changed_output_handler_reports_malformed_output_after_close(self):
        m=tool(); said=[]
        class BadAfterClose(AppServer):
            def close_stdin(self):
                super().close_stdin(); self.out.append('not-json')
        s=BadAfterClose(); console=m.Console(said.append); d=cx.CodexDriver(Launcher(s),FakeClock(),approvals=console)
        m.session_loop(d,iter(['/quit']),said.append,console,env={},read_s=0)
        self.assertTrue(any('output failed' in x for x in said))
    def test_A4_cleanup_raises_original_loop_error_after_all_fences(self):
        m=tool(); fenced=[]
        class Proc:
            def __init__(self,*a,**kw): self.proc=type('P',(),{'pid':12345})()
            def fence(self,reason): fenced.append(reason); raise SystemExit('cleanup')
        def loop(d,*a,**kw):
            d._launcher(['synthetic'],env={}); d._launcher(['synthetic'],env={}); raise RuntimeError('loop')
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(out=folder,workdir=folder,codex='synthetic',model=None)
            with patch.object(m,'_run',return_value='synthetic'),patch.object(m,'LocalProcess',Proc),patch.object(m,'session_loop',side_effect=loop),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'loop'): m.run_mode(args)
        self.assertEqual(fenced,['attended_run_over']*2)
    def test_S3_attended_env_is_exact_case_normalized_names_only(self):
        m=tool(); synthetic={name:'inert-'+name for name in m.CHILD_ENV_NAMES}
        synthetic.update({'OPENAI_API_KEY':'SYNTHETIC','CODEX_API_KEY':'SYNTHETIC','CODEX_ACCESS_TOKEN':'SYNTHETIC','monkey':'inert','OTHER_TOKEN':'SYNTHETIC','LD_PRELOAD':'/synthetic/library'})
        expected={k:v for k,v in synthetic.items() if k in m.CHILD_ENV_NAMES}
        self.assertEqual(m.child_env(synthetic),expected)
        self.assertEqual(m.child_env({'home':'/designated/home','codex_home':'/designated/codex','OPENAI_api_key':'SYNTHETIC'}),{'home':'/designated/home','codex_home':'/designated/codex'})
    def test_driver_forwards_callers_explicit_env_including_designated_homes(self):
        s=AppServer(); launch=Launcher(s); d=cx.CodexDriver(launch,FakeClock())
        explicit={'HOME':'/designated/home','CODEX_HOME':'/designated/codex','UNLISTED_REVIEW_MARKER':'inert','OTHER_API_KEY':'SYNTHETIC'}
        begin(d,record(),explicit)
        self.assertEqual(launch.calls[0]['env'],explicit)

    def test_A2_closed_worker_rejects_later_approval_request(self):
        self.open()
        self.d.send(self.h,{'kind':'close'})
        before=list(self.s.sent)
        self.ask()
        self.assertEqual(self.p.requests,[])
        self.assertEqual(self.s.sent,before)
    def test_S6_callback_records_all_drafts_from_two_turns_in_order(self):
        m=tool(); s=AppServer(script={1:[done()],2:[done()]}); console=m.Console(lambda _:None)
        d=cx.CodexDriver(Launcher(s),FakeClock(),approvals=console); captured=[]
        seen=m.session_loop(d,LateTyping(s,['first','second']),lambda _:None,console,env={},read_s=0,on_draft=captured.append)
        self.assertEqual(captured,seen['drafts'])
        self.assertEqual([x['payload']['delivery_id'] for x in captured if x['type']=='input.accepted'],['dlv-1','dlv-2'])
        self.assertEqual(sum(x['type']=='result' for x in captured),2)
    def test_coverage_held_messages_keep_text_order(self):
        m=tool(); s=AppServer(script={1:[done()],2:[done()],3:[done()]}); console=m.Console(lambda _:None)
        d=cx.CodexDriver(Launcher(s),FakeClock(),approvals=console)
        m.session_loop(d,iter(['first','second','third']),lambda _:None,console,env={},read_s=0)
        texts=[x['params']['input'][0]['text'] for x in s.sent if x.get('method')=='turn/start']
        self.assertEqual(texts,['first','second','third'])
    def test_coverage_missing_canonicalizer_does_not_silently_change_digest(self):
        import builtins
        self.open(); original=builtins.__import__
        def missing(name,globals=None,locals=None,fromlist=(),level=0):
            if name=='recursion_core.control.methods' and 'canonical_text' in fromlist: raise ImportError('synthetic missing canonicalizer')
            return original(name,globals,locals,fromlist,level)
        item={'type':'dynamicToolCall','id':'synthetic','tool':'synthetic','arguments':{}}
        with patch.object(builtins,'__import__',side_effect=missing):
            with self.assertRaises(ImportError): self.d._tool_call(self.h._codex,item)

class ConcurrentParse(unittest.TestCase):
    def open(self,pending=False):
        self.case=ThirdRead()
        self.case.open(pending=pending)
        self.h,self.rid=self.case.h,self.case.rid
    def feed(self,obj): return self.case.feed(obj)
    def test_A1_deferred_completion_is_atomic_with_start_acceptance(self):
        self.open(pending=True)
        original=self.h._codex.write_lock; response_drafts=[]; fire=[True]; outer=self
        class AfterUnlock:
            def __enter__(self): original.acquire(); return self
            def __exit__(self,*args):
                original.release()
                if fire[0]:
                    fire[0]=False
                    response_drafts.extend(outer.feed({'id':outer.rid,'result':{'turn':turn('trn-1')}}))
        self.h._codex.write_lock=AfterUnlock()
        completed=self.feed(done()('trn-1'))
        self.assertEqual([d['type'] for d in response_drafts+completed],['input.accepted','usage','result'])
        self.assertIsNone(self.h.live_turn)
        self.assertEqual(self.h._codex.deferred,{})

if __name__=='__main__': unittest.main(verbosity=2)
