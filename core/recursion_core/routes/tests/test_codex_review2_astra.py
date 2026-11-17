"""Codex GPT-6 Astra's round-2 red tests for the Codex driver (lane DRIVER-CX keeps them).

Copied unchanged from review-2-402-gpt-6-astra-evidence/test_review2_402.py in the lane's folder, the read of draft #402 at 749773c8; they failed there and
pass at the answer commit. Fakes only: no real codex, no credential, no network.
"""

"""Local state/cleanup regression checks. Synthetic stand-ins only; never runs or approves a tool."""
import argparse, contextlib, io, json, pathlib, tempfile, threading, unittest
from unittest.mock import patch
from recursion_core.routes import contract as c
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import AppServer, Launcher, FakeApprovals, begin, record, message, ask_command, done, turn, check_answer, check_params, PROTOCOL
from recursion_core.routes.tests.test_codex_attended import tool

class Review2(unittest.TestCase):
    def driver(self, server=None, port=None):
        server=server or AppServer()
        driver=cx.CodexDriver(Launcher(server),FakeClock(),approvals=port)
        handle=begin(driver,record(),{})
        return driver,handle,server
    def feed(self,driver,handle,obj):
        method=obj.get('method')
        if method is not None:
            check_params('server_requests' if 'id' in obj else 'server_notifications',method,obj.get('params'))
        else:
            pending=handle._codex.pending.get(obj.get('id'))
            check_answer(pending[0] if pending else '',obj)
        return driver.parse(handle,json.dumps(obj))
    def drain(self,driver,handle,server):
        drafts=[]
        while server.out: drafts += driver.pump(handle,0) or []
        return drafts

    def test_R1_file_change_uses_matching_turn_and_typed_item(self):
        session=cx._Session(AppServer(),'synthetic')
        session.session_id='ses-synthetic'; session.run_id='run-synthetic'
        self.assertIsNone(cx._effect(session,'fileChange',{'reason':None},{'type':'commandExecution','changes':[]}))
        changes=[{'path':'/synthetic/file.txt','kind':{'type':'add'},'diff':'synthetic inert text'}]
        effect=cx._effect(session,'fileChange',{'reason':None},{'type':'fileChange','changes':changes})
        self.assertEqual(effect['input']['changes'],changes)

    def test_R2_refused_start_clears_early_pending_approval(self):
        port=FakeApprovals(); driver,handle,server=self.driver(AppServer(silent_turns=True),port)
        driver.send(handle,message()); rid=server.sent[-1]['id']
        self.feed(driver,handle,{'method':'turn/started','params':{'threadId':'thr-1','turn':turn('trn-1')}})
        self.feed(driver,handle,{'method':'item/started','params':{'threadId':'thr-1','turnId':'trn-1','startedAtMs':1,'item':{'type':'commandExecution','id':'it-1','command':'pwd','cwd':'/w','status':'inProgress','commandActions':[]}}})
        self.feed(driver,handle,ask_command(command='pwd')('trn-1'))
        self.assertEqual(len(port.requests),1)
        self.feed(driver,handle,{'id':rid,'error':{'code':-32600,'message':'synthetic refusal'}})
        self.assertIsNone(handle.live_turn)
        self.assertEqual({'bindings':handle._codex.bindings,'items':handle._codex.items,'seen_requests':handle._codex.seen_requests},{'bindings':{},'items':{},'seen_requests':set()},'a refused turn must retain no approval eligibility or per-turn state')

    def test_R3_close_during_held_flush_suppresses_remaining_writes(self):
        class CloseAfterFirst(AppServer):
            callback=None
            def send(self,line):
                super().send(line)
                if json.loads(line).get('method')=='turn/steer' and self.callback:
                    callback,self.callback=self.callback,None
                    # Set the closed state while already in write_held; no reentrant write lock.
                    callback()
        server=CloseAfterFirst(); driver,handle,_=self.driver(server)
        driver.send(handle,message())
        driver.send(handle,message(delivery='dlv-2'))
        driver.send(handle,message(delivery='dlv-3'))
        server.callback=lambda:setattr(handle._codex,'closed',True)
        driver.pump(handle,0)
        self.assertEqual([m['params']['clientUserMessageId'] for m in server.sent if m.get('method')=='turn/steer'],['dlv-2'])

    def test_R4_deferred_completion_invalidates_pending_approval_immediately(self):
        port=FakeApprovals(); driver,handle,server=self.driver(AppServer(silent_turns=True),port)
        driver.send(handle,message()); rid=server.sent[-1]['id']
        self.feed(driver,handle,{'method':'turn/started','params':{'threadId':'thr-1','turn':turn('trn-1')}})
        self.feed(driver,handle,ask_command(command='pwd')('trn-1'))
        drafts=self.feed(driver,handle,done('interrupted')('trn-1'))
        retained=dict(handle._codex.bindings)
        drafts+=self.feed(driver,handle,{'id':rid,'result':{'turn':turn('trn-1')}})
        self.assertEqual([d['type'] for d in drafts],['input.accepted','usage','result'])
        self.assertEqual(retained,{},'engine completion must invalidate approvals before deferred core completion')
    def test_R5_response_nested_types_and_fixture_closure(self):
        bad={'id':33,'result':{'turn':{'id':'trn-synthetic','items':[],'status':7}}}
        with self.assertRaises(AssertionError): check_answer('turn/start',bad)
        module=tool()
        missing=module._refs(PROTOCOL['definitions'])-set(PROTOCOL['definitions'])
        self.assertEqual(missing,set())

    def test_R6_queued_message_does_not_hide_interrupt(self):
        class Budget(AppServer):
            empty=0
            def read_line(self,timeout_s):
                if not self.out:
                    self.empty+=1
                    if self.empty>8: raise RuntimeError('bounded idle-read budget reached')
                return super().read_line(timeout_s)
        module=tool(); server=Budget(); console=module.Console(lambda _:None)
        driver=cx.CodexDriver(Launcher(server),FakeClock(),approvals=console)
        consumed=[]
        def commands():
            for text in ('first message','queued second message','/interrupt','/quit'):
                consumed.append(text); yield text
        try: module.session_loop(driver,commands(),lambda _:None,console,env={},read_s=0)
        except RuntimeError: pass
        self.assertIn('/interrupt',consumed,'queued text must not prevent reading a later control command')
        self.assertIn('turn/interrupt',[m.get('method') for m in server.sent])

    def test_R7_capture_close_error_still_fences_worker(self):
        module=tool(); fenced=[]; original_open=pathlib.Path.open
        class Capture(io.StringIO):
            def close(self):
                super().close()
                raise OSError('synthetic capture flush failure')
        class Process:
            def __init__(self,*a,**kw): self.proc=type('Child',(),{'pid':12345})()
            def fence(self,reason): fenced.append(reason)
        def fake_open(path,*a,**kw):
            return Capture() if path.name=='drafts.jsonl' else original_open(path,*a,**kw)
        def loop(driver,*a,**kw): driver._launcher(['synthetic-child'],env={})
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(out=folder,workdir=folder,codex='synthetic-codex',model=None)
            with patch.object(module,'_run',return_value='synthetic 0'), patch.object(module,'LocalProcess',Process), patch.object(module,'session_loop',side_effect=loop), patch.object(pathlib.Path,'open',fake_open), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError): module.run_mode(args)
        self.assertEqual(fenced,['attended_run_over'])

    def test_R8_object_typed_resume_failures_preserve_classification(self):
        for info,code in [('unauthorized',c.AUTH_FAILED),('usageLimitExceeded',c.PLAN_LIMIT)]:
            with self.subTest(info=info):
                server=AppServer(refuse={'thread/resume':{'code':-32000,'message':'synthetic','data':{'codexErrorInfo':{info:{}}}}})
                driver=cx.CodexDriver(Launcher(server),FakeClock())
                with self.assertRaises(c.RouteError) as caught: begin(driver,record('thr-1'),{})
                self.assertEqual(caught.exception.code,code)
                self.assertTrue(server.stdin_closed)
                self.assertEqual(server.fenced,['start_refused'])

    def test_R3_close_finishes_before_approval_publication_no_request_is_opened(self):
        port=FakeApprovals(); driver,handle,server=self.driver(port=port)
        driver.send(handle,message()); self.drain(driver,handle,server)
        reached=threading.Event(); release=threading.Event(); errors=[]
        original=cx._effect
        def delayed(*args):
            reached.set()
            if not release.wait(3): raise RuntimeError('bounded wait')
            return original(*args)
        def reader():
            try: self.feed(driver,handle,ask_command(command='pwd')('trn-1'))
            except BaseException as err: errors.append(err)
        with patch.object(cx,'_effect',side_effect=delayed):
            worker=threading.Thread(target=reader); worker.start()
            try:
                self.assertTrue(reached.wait(3))
                driver.send(handle,{'kind':'close'})
            finally: release.set(); worker.join(3)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors,[])
        self.assertEqual(port.requests,[],'close finished before publication; no actionable record may open')

    def test_R8_resume_internal_error_is_not_a_missing_thread(self):
        server=AppServer(refuse={'thread/resume':{'code':-32000,'message':'synthetic','data':{'codexErrorInfo':'internalServerError'}}})
        driver=cx.CodexDriver(Launcher(server),FakeClock())
        with self.assertRaises(c.RouteError) as caught: begin(driver,record('thr-1'),{})
        self.assertEqual(caught.exception.code,c.ENGINE_ERROR)
    def test_R4_deferred_completed_turn_cannot_open_a_new_approval(self):
        port=FakeApprovals(); driver,handle,server=self.driver(AppServer(silent_turns=True),port)
        driver.send(handle,message())
        self.feed(driver,handle,{'method':'turn/started','params':{'threadId':'thr-1','turn':turn('trn-1')}})
        self.feed(driver,handle,done('interrupted')('trn-1'))
        self.feed(driver,handle,ask_command(command='pwd')('trn-1'))
        self.assertEqual(port.requests,[],'engine completion ends eligibility even before acceptance is delivered')

    def test_R7_interrupted_worker_cleanup_still_attempts_other_workers(self):
        module=tool(); fenced=[]
        class Process:
            def __init__(self,*a,**kw): self.proc=type('Child',(),{'pid':12345})()
            def fence(self,reason):
                fenced.append(reason)
                if len(fenced)==1: raise KeyboardInterrupt('synthetic cleanup interruption')
        def loop(driver,*a,**kw):
            driver._launcher(['synthetic-child'],env={})
            driver._launcher(['synthetic-child'],env={})
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(out=folder,workdir=folder,codex='synthetic-codex',model=None)
            with patch.object(module,'_run',return_value='synthetic 0'),patch.object(module,'LocalProcess',Process),patch.object(module,'session_loop',side_effect=loop),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt): module.run_mode(args)
        self.assertEqual(fenced,['attended_run_over','attended_run_over'])
    def test_F3_noncanonical_inputs_retain_call_and_result(self):
        for value in (float('inf'),'\ud800'):
            with self.subTest(kind='number' if isinstance(value,float) else 'unicode'):
                driver,handle,server=self.driver()
                item={'type':'mcpToolCall','id':'it-synthetic','server':'docs','tool':'lookup','status':'completed','arguments':{'value':value},'result':None}
                try:
                    drafts=driver._tool_call(handle._codex,item)+driver._tool_result(handle._codex,item)
                except UnicodeError as err:
                    self.fail('the noncanonical fallback raised '+type(err).__name__)
                self.assertEqual([d['type'] for d in drafts],['tool.call','tool.result'])
                self.assertIsNone(drafts[0]['payload']['input'])
    def test_F12_normal_declaration_mutation_is_refused(self):
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError): cx.DECLARATION.flags.multi_turn=False
        with self.assertRaises(FrozenInstanceError): cx.DECLARATION.input_ack=False
    def test_STRICT_launch_uses_exact_env_argv_and_no_shell(self):
        server=AppServer(); launch=Launcher(server)
        driver=cx.CodexDriver(launch,FakeClock(),binary='/synthetic/codex')
        env={'PATH':'/synthetic/bin','TMPDIR':tempfile.gettempdir()}
        begin(driver,record(),env)
        self.assertEqual(launch.calls,[{'argv':['/synthetic/codex','-c','check_for_update_on_startup=false','app-server'],'env':env,'shell':False}])

    def test_STRICT_run_mode_calls_child_env_and_passes_its_result(self):
        module=tool(); observed=[]
        allowed={'PATH':'/synthetic/bin','TMPDIR':tempfile.gettempdir(),'XDG_RUNTIME_DIR':tempfile.gettempdir()}
        parent={**allowed,'UNEXPECTED_PARENT_MARKER':'inert'}
        def loop(driver,*a,**kw): observed.append(kw['env'])
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(out=folder,workdir=folder,codex='synthetic-codex',model=None)
            with patch.object(module,'_run',return_value='synthetic 0'),patch.object(module.os,'environ',parent),patch.object(module,'session_loop',side_effect=loop),contextlib.redirect_stdout(io.StringIO()): module.run_mode(args)
        self.assertEqual(observed,[allowed])

if __name__=='__main__': unittest.main(verbosity=2)
