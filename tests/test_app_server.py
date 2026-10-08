"""Exercise the app-server transport over real pipes, without model calls."""
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
from herald import think

SERVER = r'''
import json, sys
mode, log = sys.argv[1:]
turn = 0
def emit(value):
    print(json.dumps(value), flush=True)
def notify(method, **params):
    emit({'method':method,'params':params})
def finish(text, status='completed'):
    notify('item/completed',threadId='stored',turnId=str(turn),
           item={'id':'reply','type':'agentMessage','text':text,'phase':'final_answer'})
    notify('thread/tokenUsage/updated',threadId='stored',turnId=str(turn),tokenUsage={
           'last':{'inputTokens':10,'outputTokens':2,'cachedInputTokens':3},
           'total':{'inputTokens':turn*10+1000,'outputTokens':turn*2+100,'cachedInputTokens':turn*3+100}})
    notify('turn/completed',threadId='stored',turn={'id':str(turn),'status':status,'error':None})
for line in sys.stdin:
    msg=json.loads(line)
    with open(log,'a') as f: f.write(line)
    method=msg.get('method')
    if method=='initialize':emit({'id':msg['id'],'result':{}})
    elif method in ('thread/start','thread/resume'):
        if mode=='missing':
            emit({'id':msg['id'],'error':{'code':-1,'message':'thread not found'}})
        else:
            notify('thread/started',thread={'id':'stored'})
            emit({'id':msg['id'],'result':{'thread':{'id':'stored'}}})
    elif method=='turn/start':
        turn+=1
        notify('turn/started',threadId='stored',turn={'id':str(turn),'status':'inProgress'})
        emit({'id':msg['id'],'result':{'turn':{'id':str(turn)}}})
        if mode in ('steer','late','cancel','disconnect','idle') and turn==1:
            notify('item/started',threadId='stored',turnId=str(turn),
                   item={'id':'tool','type':'commandExecution','command':'probe command'})
            if mode=='disconnect':sys.exit(0)
        elif mode=='approval':
            emit({'id':900,'method':'item/commandExecution/requestApproval','params':{'threadId':'stored'}})
        elif mode=='commentary':
            notify('item/completed',threadId='stored',turnId=str(turn),
                   item={'id':'comment','type':'agentMessage','text':'Working on it.','phase':'commentary'})
            finish('Done.')
        else:finish(msg['params']['input'][0]['text'])
    elif method=='turn/steer':
        if mode=='late':
            finish('First reply.')
            emit({'id':msg['id'],'error':{'code':-1,'message':'No active turn'}})
        else:
            emit({'id':msg['id'],'result':{'turnId':str(turn)}})
            finish(msg['params']['input'][0]['text'])
    elif method=='turn/interrupt':
        emit({'id':msg['id'],'result':{}})
        finish('',status='interrupted')
    elif msg.get('id')==900:
        finish(msg.get('result',{}).get('decision','unexpected approval'))
'''


class AppServerTests(unittest.TestCase):
    def test_transport_is_explicit_and_batch_default_stays_exec(self):
        for transport, expected in ((None, '_run_codex'), ('app-server', '_run_codex_app_server')):
            with self.subTest(transport=transport), \
                 patch.object(think,'_run_codex',return_value=(0,{'result':'ok','usage':{}},None,'','',False)) as batch, \
                 patch.object(think,'_run_codex_app_server',return_value=(0,{'result':'ok','usage':{}},None,'','',False)) as live, \
                 patch.object(think,'_record'), \
                 patch.object(think.config,'get',side_effect=lambda key,default=None:default):
                args={'codex_transport':transport} if transport else {}
                r=think.think('probe',label='test',engine='codex',model='gpt-probe',permission_mode='auto',**args)
                self.assertTrue(r.ok)
                self.assertEqual(batch.called,expected=='_run_codex')
                self.assertEqual(live.called,expected=='_run_codex_app_server')

    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='app-server-test-')
        self.addCleanup(tmp.cleanup)
        self.root=pathlib.Path(tmp.name)
        self.script=self.root/'server.py';self.script.write_text(SERVER)
        self.log=self.root/'requests.jsonl'
        self.commands=[]

    def run_server(self, mode='basic', **kw):
        popen=subprocess.Popen
        def launch(cmd, **opts):
            self.commands.append(cmd)
            return popen([sys.executable,'-u',str(self.script),mode,str(self.log)],**opts)
        args=dict(model='gpt-probe',effort='max',cwd=self.root,timeout=3,idle_timeout=2,
                  append_system_prompt='orientation',json_schema=None,resume=None,
                  permission_mode='auto',add_dirs=['/tmp/memory'],cancel_key='app-test')
        args.update(kw)
        with patch.object(think.subprocess,'Popen',side_effect=launch), \
             patch.object(think.config,'get',side_effect=lambda key,default=None:default):
            return think._run_codex_app_server('Initial input.',**args)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_first_party_cli_settings_and_real_pipe_event_batches(self):
        result=self.run_server()
        self.assertEqual(result[0],0);self.assertEqual(result[1]['result'],'Initial input.')
        self.assertEqual(result[1]['session_id'],'stored');self.assertEqual(result[2],10)
        # Historic thread totals must not be charged to this invocation.
        self.assertEqual(result[1]['usage']['input_tokens'],10)
        cmd=self.commands[0]
        self.assertEqual(cmd[:5],['codex','--dangerously-bypass-hook-trust','app-server','--listen','stdio://'])
        self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]',cmd)
        self.assertIn('sandbox_workspace_write.writable_roots=["/tmp/memory"]',cmd)
        self.assertIn('approval_policy="never"',cmd)
        self.assertNotIn('app-test',think._active)

    def test_resume_reuses_id_and_passes_model_and_orientation(self):
        result=self.run_server(resume='stored')
        req=next(r for r in self.requests() if r.get('method')=='thread/resume')
        self.assertEqual(req['params']['threadId'],'stored')
        self.assertEqual(req['params']['model'],'gpt-probe')
        self.assertEqual(req['params']['developerInstructions'],'orientation')
        self.assertTrue(req['params']['excludeTurns'])
        self.assertEqual(result[1]['session_id'],'stored')

    def test_steer_is_accepted_in_the_same_active_turn(self):
        seen=[]
        def progress(event):
            if event.kind=='tool':self.assertTrue(think.steer('app-test','Follow-up.'))
            seen.append(event.kind)
        submitted=[]
        result=self.run_server('steer',on_progress=progress,submitted=submitted)
        req=next(r for r in self.requests() if r.get('method')=='turn/steer')
        self.assertEqual(req['params']['expectedTurnId'],'1')
        self.assertEqual(req['params']['threadId'],'stored')
        self.assertEqual(result[1]['result'],'Follow-up.')
        self.assertEqual(submitted,['Initial input.','Follow-up.'])
        self.assertEqual(sum(r.get('method')=='turn/start' for r in self.requests()),1)

    def test_late_rejected_steer_runs_on_same_thread_without_dropping_first_reply(self):
        def progress(event):
            if event.kind=='tool':self.assertTrue(think.steer('app-test','Late follow-up.'))
        result=self.run_server('late',on_progress=progress)
        turns=[r for r in self.requests() if r.get('method')=='turn/start']
        self.assertEqual(len(turns),2)
        self.assertEqual(turns[1]['params']['input'][0]['text'],'Late follow-up.')
        self.assertEqual(result[1]['result'],'First reply.\n\nLate follow-up.')
        self.assertEqual(result[1]['usage']['input_tokens'],20)

    def test_native_interrupt_cancels_without_a_fresh_turn(self):
        def progress(event):
            if event.kind=='tool':self.assertTrue(think.cancel('app-test'))
        result=self.run_server('cancel',on_progress=progress)
        self.assertTrue(result[5])
        self.assertTrue(any(r.get('method')=='turn/interrupt' for r in self.requests()))
        self.assertFalse(think.cancel('app-test'))

    def test_disconnect_keeps_session_and_is_an_error(self):
        result=self.run_server('disconnect')
        self.assertNotEqual(result[0],0);self.assertTrue(result[1]['is_error'])
        self.assertIn('disconnected',result[1]['result'])
        self.assertEqual(think._session_from_stream(result[3]),'stored')

    def test_idle_timeout_does_not_wait_for_process_exit(self):
        start=time.monotonic()
        result=self.run_server('idle',idle_timeout=.1)
        self.assertLess(time.monotonic()-start,2)
        self.assertIn('went silent',result[1]['result'])

    def test_server_approval_is_declined(self):
        result=self.run_server('approval')
        self.assertEqual(result[1]['result'],'decline')
        response=next(r for r in self.requests() if r.get('id')==900)
        self.assertEqual(response['result']['decision'],'decline')

    def test_commentary_delivered_as_interim_and_not_used_as_final(self):
        seen=[]
        def progress(event):
            seen.append((event.kind,event.text));return event.kind=='interim'
        result=self.run_server('commentary',on_progress=progress)
        self.assertIn(('interim','Working on it.'),seen)
        self.assertEqual(result[1]['result'],'Done.')

    def test_missing_session_fails_without_automatically_creating_another(self):
        result=self.run_server('missing',resume='stored')
        self.assertTrue(result[1]['is_error'])
        self.assertIn('thread not found',result[1]['result'])
        self.assertFalse(any(r.get('method')=='thread/start' for r in self.requests()))

    def test_plan_sandbox_and_structured_output(self):
        result=self.run_server(permission_mode='plan',json_schema={'type':'object','properties':{}})
        cmd=self.commands[0]
        self.assertIn('sandbox_mode="read-only"',cmd)
        turn=next(r for r in self.requests() if r.get('method')=='turn/start')
        self.assertFalse(turn['params']['outputSchema']['additionalProperties'])


if __name__=='__main__':unittest.main()
