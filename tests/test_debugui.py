"""Transcript fidelity, private-mode exclusion and the read-only HTTP boundary."""
from __future__ import annotations
import json
import contextlib
import subprocess
import http.client
import threading
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from herald import debugui, debugtrace, think


class Events(unittest.TestCase):
    def test_claude_keeps_full_tool_input_and_result(self):
        command = 'echo ' + 'x' * 15000
        ev = debugui.normalize({'timestamp':'2026-01-01T12:00:00.123456Z','type':'assistant', 'message':{'content':[{'type':'tool_use','id':'tool1','name':'Bash','input':{'command':command}}], 'usage':{'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':30}}}, 1)
        self.assertIn(command, ev['text'])
        self.assertEqual(ev['context'], 60)
        self.assertEqual(ev['call_id'], 'tool1')
        self.assertEqual(ev['ts'], '2026-01-01T12:00:00.123456Z')
        ev = debugui.normalize({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool1','content':'result'*10000}]}},2)
        self.assertIn('result'*10000,ev['text'])
        self.assertEqual(ev['call_id'],'tool1')

    def test_codex_tool_and_context_measurements(self):
        ev=debugui.normalize({'timestamp':'2026-01-01T12:00:00Z','type':'response_item','payload':{'type':'function_call','name':'exec_command','call_id':'c','arguments':'{"cmd":"pwd"}'}},1)
        self.assertEqual(ev['name'],'exec_command')
        self.assertIn('pwd',ev['text'])
        ev=debugui.normalize({'type':'event_msg','payload':{'type':'token_count','info':{'last_token_usage':{'input_tokens':3500},'model_context_window':100000}}},2)
        self.assertEqual((ev['context'],ev['window']),(3500,100000))

    def test_app_server_capture_preserves_observed_time(self):
        ev=debugui.normalize({'ts':'2026-01-01T12:00:00.987654Z','elapsed_ns':123456789,'channel':'stdout','data':{'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'last':{'inputTokens':100},'modelContextWindow':500}}}},3)
        self.assertEqual(ev['context'],100)
        self.assertEqual(ev['window'],500)
        self.assertEqual(ev['elapsed_ns'],123456789)
        self.assertEqual(ev['ts'],'2026-01-01T12:00:00.987654Z')

    def test_malformed_and_partial_lines_do_not_hide_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'log.jsonl'
            p.write_text('{"type":"event"}\ninvalid\n{"partial":')
            self.assertEqual(list(debugui.records(p)),[{'type':'event'}])

    def test_capture_session_identity(self):
        self.assertEqual(debugui.session_id({'channel':'stdout','data':{'result':{'thread':{'id':'session1'}}}}),'session1')
        self.assertEqual(debugui.session_id({'channel':'outcome','data':{'payload':{'session_id':'session2'}}}),'session2')

    @patch('herald.config.secrets', return_value={'key':'a-credential-known-only-to-herald'})
    def test_redaction_works_before_serialization(self, _):
        clean=debugui.redactor()
        data=clean({'output':'a-credential-known-only-to-herald','nested':{'refresh_token':'opaque-value','safe':'hello'}})
        self.assertEqual(data['output'],'[redacted]')
        self.assertEqual(data['nested']['refresh_token'],'[redacted]')
        self.assertEqual(data['nested']['safe'],'hello')


class Captures(unittest.TestCase):
    @patch('herald.private.active', return_value=True)
    def test_private_mode_never_opens_a_capture(self, _):
        with patch('os.open') as opened:
            trace=debugtrace.Trace(prompt='private')
            trace.emit('stdout','private output')
            trace.close()
            opened.assert_not_called()

    def test_capture_fidelity_redaction_order_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(debugtrace.config,'HOME',pathlib.Path(tmp)), patch('herald.config.secrets',return_value={}):
            trace=debugtrace.Trace(engine='codex',prompt='hello')
            trace.emit('stdout',json.dumps({'refresh_token':'opaque','payload':'x'*16000}))
            trace.close()
            path=next((pathlib.Path(tmp)/'debug-traces').glob('*.jsonl'))
            data=[json.loads(s) for s in path.read_text().splitlines()]
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual([r['seq'] for r in data],[1,2,3])
            self.assertEqual(data[1]['data']['refresh_token'],'[redacted]')
            self.assertEqual(len(data[1]['data']['payload']),16000)
            self.assertLessEqual(data[0]['elapsed_ns'],data[1]['elapsed_ns'])
            self.assertRegex(data[0]['ts'],r'\.\d{6}\+00:00$')

    def test_process_capture_receives_full_stdout_stderr_and_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(debugtrace.config,'HOME',pathlib.Path(tmp)), patch('herald.config.secrets',return_value={}):
            trace=debugtrace.Trace(engine='codex',prompt='probe')
            think._private.debug_trace=trace
            script="import sys,json; p=sys.stdin.readline(); print('diagnostic',file=sys.stderr,flush=True); print(json.dumps({'session_id':'probe-session','result':'z'*17000}),flush=True)"
            def write(proc,text):
                proc.stdin.write(text+'\n')
                proc.stdin.flush()
            def handle(line,state,progress,key):
                state['payload']=json.loads(line)
            processes=[]
            def spawn(engine,cmd,**kwargs):
                proc=subprocess.Popen(cmd,**kwargs)
                processes.append(proc)
                return proc
            try:
                with patch.object(think,'_spawn',side_effect=spawn):
                    result=think._run_process('codex',[sys.executable,'-u','-c',script],'probe',write=write,steerable=False,handler=handle,finish=lambda state:(state['payload'],None),cwd=pathlib.Path(tmp),timeout=5,idle_timeout=5)
                self.assertEqual(result[0],0)
            finally:
                trace.close()
                think._private.debug_trace=None
                for proc in processes:
                    proc.stdout.close()
                    proc.stderr.close()
            path=next((pathlib.Path(tmp)/'debug-traces').glob('*.jsonl'))
            events=list(debugui.records(path))
            self.assertEqual(next(e['data']['text'] for e in events if e['channel']=='stdin'),'probe')
            self.assertEqual(len(next(e['data']['result'] for e in events if e['channel']=='stdout')),17000)
            self.assertIn('diagnostic',next(e['data'] for e in events if e['channel']=='stderr'))
            self.assertEqual(debugui.capture_identity(str(path),path.stat().st_size,path.stat().st_mtime_ns),'probe-session')

    def test_existing_schema_is_opened_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/'facts.db'
            con=sqlite3.connect(path)
            con.execute('CREATE TABLE sentinel (value TEXT)')
            con.close()
            with patch.object(debugui.config,'DB_PATH',path):
                with contextlib.closing(debugui.connect()) as con:
                    with self.assertRaises(sqlite3.OperationalError):
                        con.execute("INSERT INTO sentinel VALUES ('no')")

class HTTPBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        path=pathlib.Path(self.tmp.name)/'session.jsonl'
        path.write_text(''.join(json.dumps({'type':'response_item','timestamp':'2026-01-01T12:00:00Z','payload':{'type':'function_call_output','call_id':str(i),'output':('needle' if i==140 else 'value')+str(i)}})+'\n' for i in range(160)))
        self.session={'id':'test-session','engine':'codex','updated':None,'sources':[('transcript',path)],'runs':[]}
        self.catalog_patch=patch.object(debugui.Catalog,'get',return_value={'test-session':self.session})
        self.secret_patch=patch('herald.config.secrets',return_value={})
        self.catalog_patch.start()
        self.secret_patch.start()
        self.server=debugui.make_server(0)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.catalog_patch.stop()
        self.secret_patch.stop()
        self.tmp.cleanup()

    def get(self,path,headers=None):
        con=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        con.request('GET',path,headers=headers or {})
        response=con.getresponse()
        result=response.status,dict(response.getheaders()),response.read()
        con.close()
        return result

    def test_host_and_origin_guard(self):
        self.assertEqual(self.get('/api/sessions',{'Host':'attacker.invalid'})[0],403)
        self.assertEqual(self.get('/api/sessions',{'Origin':'https://attacker.invalid'})[0],403)
        status,headers,body=self.get('/api/sessions')
        self.assertEqual(status,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

    def test_pagination_search_and_full_export(self):
        status,_,body=self.get('/api/session/test-session/events?offset=100')
        data=json.loads(body)
        self.assertEqual(status,200)
        self.assertEqual(data['total'],160)
        self.assertEqual(len(data['events']),60)
        self.assertEqual(data['events'][0]['seq'],101)
        status,_,body=self.get('/api/session/test-session/events?q=needle')
        self.assertEqual(json.loads(body)['events'][0]['seq'],141)
        status,headers,body=self.get('/api/session/test-session/download')
        self.assertEqual(len(body.splitlines()),160)
        self.assertIn('attachment',headers['Content-Disposition'])

    def test_arbitrary_paths_and_unknown_sessions_refused(self):
        self.assertEqual(self.get('/api/session/unknown/events')[0],404)
        self.assertEqual(self.get('/api/session/..%2F..%2Fsecret/events')[0],404)
        self.assertEqual(self.get('/../../config/defaults.json')[0],404)


if __name__=='__main__':
    unittest.main()
