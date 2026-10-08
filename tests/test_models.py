"""Catalog discovery never runs inference; model taps preserve topic context."""
from contextlib import contextmanager
import importlib.machinery
import importlib.util
import pathlib
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from herald import think

ROWS = [{'engine':'claude','model':'opus','name':'Opus','efforts':['low','medium','high','max']},
        {'engine':'claude','model':'haiku','name':'Haiku','efforts':[]},
        {'engine':'codex','model':'gpt-probe','name':'GPT Probe',
         'efforts':['low','high','max','ultra'],'default_effort':'low'}]

class CatalogTests(unittest.TestCase):
    def test_catalog_keeps_model_specific_effort_capabilities(self):
        @contextmanager
        def process(cmd):
            def ask(messages, matches):
                return {'type':'control_response','response':{'subtype':'success',
                        'request_id':'herald-models','response':{'models':[
                        {'value':'opus','displayName':'Opus','supportsEffort':True,
                         'supportedEffortLevels':['low','medium','high','max']},
                        {'value':'haiku','displayName':'Haiku'}]}}}
            yield ask
        with patch.object(think,'_catalog_process',process):
            self.assertEqual(think.available_models('claude'),ROWS[:2])

    def test_claude_uses_only_an_initialize_request(self):
        captured=[]
        @contextmanager
        def process(cmd):
            self.assertIn('--no-session-persistence',cmd)
            def ask(messages,matches):
                captured.extend(messages)
                result={'type':'control_response','response':{'subtype':'success','request_id':'herald-models',
                        'response':{'models':[{'value':'haiku','displayName':'Haiku'}, {'value':'haiku'}]}}}
                self.assertTrue(matches(result))
                return result
            yield ask
        with patch.object(think,'_catalog_process',process):
            self.assertEqual(think.available_models('claude'),[ROWS[1]])
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0]['request']['subtype'],'initialize')
        self.assertNotIn('prompt',captured[0])

    def test_codex_includes_hidden_models_and_every_page(self):
        captured=[]
        @contextmanager
        def process(cmd):
            self.assertEqual(cmd,['codex','app-server'])
            def ask(messages,matches):
                captured.extend(messages)
                request=messages[-1]
                value={'id':request['id'],'result':({} if request['method']=='initialize' else
                     {'data':[{'model':'one','displayName':'One'}], 'nextCursor':'page2'} if request['id']==2 else
                     {'data':[{'model':'two','displayName':'Two',
                               'supportedReasoningEfforts':[{'reasoningEffort':'max'},{'reasoningEffort':'ultra'}],
                               'defaultReasoningEffort':'max'}], 'nextCursor':None})}
                self.assertTrue(matches(value)); return value
            yield ask
        with patch.object(think,'_catalog_process',process):
            rows=think.available_models('codex')
            self.assertEqual([r['model'] for r in rows],['one','two'])
            self.assertEqual(rows[1]['efforts'],['max','ultra'])
            self.assertEqual(rows[1]['default_effort'],'max')
        listing=[r for r in captured if r.get('method')=='model/list']
        self.assertTrue(all(r['params']['includeHidden'] for r in listing))
        self.assertEqual(listing[1]['params']['cursor'],'page2')
        self.assertNotIn('turn/start',[r.get('method') for r in captured])

class TelegramModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader=importlib.machinery.SourceFileLoader('tg_models_test',str(ROOT/'bin/herald-telegram'))
        spec=importlib.util.spec_from_loader(loader.name,loader)
        cls.tg=importlib.util.module_from_spec(spec);loader.exec_module(cls.tg)
        cls.catalog_helper=staticmethod(cls.tg._model_catalog)

    def setUp(self):
        self.key='-100:9'
        self.state={'offset':0,'threads':{self.key:{'engine':'claude','model':'opus','session_id':'kept','turns':4,'last_ts':time.time()}}}
        self.sent=[]; self.calls=[]
        self.msg={'chat':{'id':-100},'message_thread_id':9,'message_id':5,'from':{'id':42},'text':'/models'}
        def api(method,**kw):
            self.calls.append((method,kw));return {'ok':True,'result':{'message_id':77}}
        for p in (patch.object(self.tg.config,'secret',return_value=42),
                  patch.object(self.tg,'api',side_effect=api),
                  patch.object(self.tg,'send',side_effect=lambda chat,text,*a,**k:self.sent.append(text)),
                  patch.object(self.tg,'_model_catalog',return_value=(ROWS,[]))):
            p.start(); self.addCleanup(p.stop)

    def menu(self):
        self.tg._handle_models({'message':self.msg},self.state)
        token=next(iter(self.state['model_menus']))
        return {'callback_query':{'id':'tap','from':{'id':42},'data':f'model:{token}:1',
                 'message':self.msg | {'message_id':77}}}

    def test_list_contains_all_models_and_does_not_change_the_session(self):
        self.menu()
        text='\n'.join(self.sent)
        for r in ROWS:self.assertIn(r['engine']+'/'+r['model'],text)
        self.assertEqual(self.state['threads'][self.key]['session_id'],'kept')
        self.assertEqual(self.tg.command_word('/models@bot'),'/models')
        buttons=next(k['reply_markup']['inline_keyboard'] for method,k in self.calls if method=='sendMessage')
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for row in buttons for b in row))

    def test_same_engine_tap_preserves_context(self):
        self.tg._handle_model_callback(self.menu(),self.state)
        ts=self.state['threads'][self.key]
        self.assertEqual(ts['model'],'haiku');self.assertEqual(ts['session_id'],'kept');self.assertEqual(ts['turns'],4)

    def test_a_saved_menu_still_selects_the_same_model_after_reload(self):
        import json
        update=self.menu()
        restored=json.loads(json.dumps(self.state))
        self.tg._handle_model_callback(update,restored)
        self.assertEqual(restored['threads'][self.key]['model'],'haiku')
        self.assertEqual(restored['threads'][self.key]['session_id'],'kept')

    def test_expired_menu_and_invalid_indices_leave_the_model_unchanged(self):
        update=self.menu()
        token=update['callback_query']['data'].split(':')[1]
        update['callback_query']['data']=f'model:{token}:999'
        self.tg._handle_model_callback(update,self.state)
        self.assertEqual(self.state['threads'][self.key]['model'],'opus')
        update['callback_query']['data']=f'model:{token}:1'
        self.state['model_menus'][token]['created']=time.time()-86401
        self.tg._handle_model_callback(update,self.state)
        self.assertEqual(self.state['threads'][self.key]['model'],'opus')

    def test_cross_engine_tap_clears_context(self):
        update=self.menu();update['callback_query']['data']=update['callback_query']['data'].rsplit(':',1)[0]+':2'
        self.tg._handle_model_callback(update,self.state)
        ts=self.state['threads'][self.key]
        self.assertEqual(ts['engine'],'codex');self.assertIsNone(ts['session_id']);self.assertEqual(ts['turns'],0)

    def test_other_sender_topic_and_message_cannot_use_a_menu(self):
        import copy
        original=self.menu()
        for change in ('sender','topic','message'):
            update=copy.deepcopy(original)
            if change=='sender':update['callback_query']['from']['id']=7
            if change=='topic':update['callback_query']['message']['message_thread_id']=10
            if change=='message':update['callback_query']['message']['message_id']=88
            self.tg._handle_model_callback(update,self.state)
            self.assertEqual(self.state['threads'][self.key]['model'],'opus')

    def test_typed_selection_uses_the_catalog_and_preserves_context(self):
        self.msg['text']='/models claude/haiku'
        self.tg._handle_models({'message':self.msg},self.state)
        self.assertEqual(self.state['threads'][self.key]['model'],'haiku')
        self.assertEqual(self.state['threads'][self.key]['session_id'],'kept')

    def test_callback_waits_for_the_current_turn(self):
        update=self.menu();lock=self.tg._lock_for(self.key)
        lock.acquire()
        worker=threading.Thread(target=self.tg._handle_model_callback,args=(update,self.state))
        worker.start()
        try:
            worker.join(.02)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.state['threads'][self.key]['model'],'opus')
        finally:
            lock.release();worker.join(timeout=2)
        self.assertEqual(self.state['threads'][self.key]['model'],'haiku')

    def test_partial_catalog_failure_keeps_the_other_engine(self):
        with patch.object(self.tg.think,'available_models',side_effect=lambda e:
                          ROWS[:2] if e=='claude' else (_ for _ in ()).throw(TimeoutError())):
            rows, errors=self.catalog_helper()
        self.assertEqual(rows,ROWS[:2]); self.assertEqual(len(errors),1)

    def effort_menu(self, index=2):
        update=self.menu()
        update['callback_query']['data']=update['callback_query']['data'].rsplit(':',1)[0]+f':{index}'
        self.tg._handle_model_callback(update,self.state)
        token=next(iter(self.state['effort_menus']))
        return {'callback_query':{'id':'effort-tap','from':{'id':42},'data':f'effort:{token}:4',
                 'message':self.msg | {'message_id':77}}}

    def test_model_tap_then_effort_tap_uses_native_ultra_and_preserves_session(self):
        update=self.effort_menu()
        ts=self.state['threads'][self.key]
        ts['session_id']='codex-session';ts['turns']=3
        self.tg._handle_effort_callback(update,self.state)
        self.assertEqual(ts['effort'],'ultra')
        self.assertEqual(ts['session_id'],'codex-session');self.assertEqual(ts['turns'],3)
        buttons=next(kw['reply_markup']['inline_keyboard']
               for method,kw in reversed(self.calls) if kw.get('reply_markup',{}).get('inline_keyboard'))
        labels=[b['text'] for row in buttons for b in row]
        self.assertEqual(labels,['CLI default','low','high','max','ultra'])
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for row in buttons for b in row))

    def test_typed_model_selection_also_opens_effort_picker(self):
        self.msg['text']='/models codex/gpt-probe'
        self.tg._handle_models({'message':self.msg},self.state)
        menu=next(iter(self.state['effort_menus'].values()))
        self.assertEqual(menu['choices'],['auto','low','high','max','ultra'])

    def test_switching_clears_unsupported_effort_and_retains_supported_effort(self):
        ts=self.state['threads'][self.key];ts['effort']='ultra'
        self.tg._choose_model(self.state,self.key,self.msg,ROWS[0])
        self.assertEqual(ts['effort'],'auto')
        ts['effort']='high'
        self.tg._choose_model(self.state,self.key,self.msg,ROWS[0])
        self.assertEqual(ts['effort'],'high')
        self.tg._choose_model(self.state,self.key,self.msg,ROWS[1])
        self.assertEqual(ts['effort'],'auto')
        self.assertIn('no adjustable thinking',' '.join(self.sent))

    def test_default_tap_clears_override_and_saved_picker_survives_reload(self):
        import json
        update=self.effort_menu()
        restored=json.loads(json.dumps(self.state))
        restored['threads'][self.key]['effort']='ultra'
        update['callback_query']['data']=update['callback_query']['data'].rsplit(':',1)[0]+':0'
        self.tg._handle_effort_callback(update,restored)
        self.assertEqual(restored['threads'][self.key]['effort'],'auto')

    def test_effort_picker_rejects_forged_expired_and_stale_model_taps(self):
        import copy
        original=self.effort_menu()
        ts=self.state['threads'][self.key]
        for change in ('sender','topic','message','index','model','expiry'):
            update=copy.deepcopy(original)
            ts['model']='gpt-probe';ts['effort']='auto'
            token=update['callback_query']['data'].split(':')[1]
            self.state['effort_menus'][token]['created']=time.time()
            self.state['effort_menus'][token].pop('selected',None)
            if change=='sender':update['callback_query']['from']['id']=7
            if change=='topic':update['callback_query']['message']['message_thread_id']=10
            if change=='message':update['callback_query']['message']['message_id']=88
            if change=='index':update['callback_query']['data']=f'effort:{token}:999'
            if change=='model':ts['model']='another'
            if change=='expiry':self.state['effort_menus'][token]['created']=time.time()-86401
            self.tg._handle_effort_callback(update,self.state)
            self.assertEqual(ts['effort'],'auto',change)

    def test_effort_callback_waits_for_current_turn(self):
        update=self.effort_menu();lock=self.tg._lock_for(self.key)
        lock.acquire()
        worker=threading.Thread(target=self.tg._handle_effort_callback,args=(update,self.state))
        worker.start()
        try:
            worker.join(.02)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.state['threads'][self.key]['effort'],'auto')
        finally:
            lock.release();worker.join(timeout=2)
        self.assertEqual(self.state['threads'][self.key]['effort'],'ultra')

    def test_old_model_menu_refreshes_capabilities(self):
        update=self.menu()
        token=update['callback_query']['data'].split(':')[1]
        self.state['model_menus'][token]['choices']=[{k:v for k,v in r.items() if k!='efforts'} for r in ROWS]
        update['callback_query']['data']=f'model:{token}:0'
        with patch.object(self.tg.think,'available_models',return_value=ROWS[:2]):
            self.tg._handle_model_callback(update,self.state)
        self.assertEqual(next(iter(self.state['effort_menus'].values()))['choices'],
                         ['auto','low','medium','high','max'])

    def test_effort_command_reopens_picker_and_refuses_unsupported_level(self):
        self.effort_menu()
        self.msg['text']='/effort'
        self.tg.handle({'message':self.msg},self.state)
        self.assertEqual(len(self.state['effort_menus']),2)
        self.msg['text']='/effort xhigh'
        self.tg.handle({'message':self.msg},self.state)
        self.assertEqual(self.state['threads'][self.key]['effort'],'auto')
        self.msg['text']='/effort max'
        self.tg.handle({'message':self.msg},self.state)
        self.assertEqual(self.state['threads'][self.key]['effort'],'max')

    def test_model_shortcut_does_not_carry_an_incompatible_level(self):
        self.effort_menu()
        self.state['threads'][self.key]['effort']='ultra'
        self.msg['text']='/opus'
        self.tg.handle({'message':self.msg},self.state)
        self.assertEqual(self.state['threads'][self.key]['effort'],'auto')
        self.assertNotIn('effort_caps',self.state['threads'][self.key])

    def test_model_and_effort_choices_edit_the_same_message_without_new_messages(self):
        update=self.menu()
        update['callback_query']['data']=update['callback_query']['data'].rsplit(':',1)[0]+':2'
        sends=len(self.sent);calls=len(self.calls)
        self.tg._handle_model_callback(update,self.state)
        model_calls=self.calls[calls:]
        self.assertEqual(len(self.sent),sends)
        self.assertFalse(any(method=='sendMessage' for method,kw in model_calls))
        edit=next(kw for method,kw in model_calls if method=='editMessageText')
        self.assertEqual(edit['message_id'],77);self.assertIn('codex/gpt-probe',edit['text'])
        self.assertTrue(all(b['callback_data'].startswith('effort:')
                            for row in edit['reply_markup']['inline_keyboard'] for b in row))
        token=next(iter(self.state['effort_menus']))
        self.assertEqual(self.state['effort_menus'][token]['message_id'],77)
        update['callback_query']['data']=f'effort:{token}:3'
        calls=len(self.calls)
        self.tg._handle_effort_callback(update,self.state)
        effort_calls=self.calls[calls:]
        self.assertEqual(len(self.sent),sends)
        self.assertFalse(any(method=='sendMessage' for method,kw in effort_calls))
        edit=next(kw for method,kw in effort_calls if method=='editMessageText')
        self.assertEqual(edit['message_id'],77);self.assertIn('codex/gpt-probe',edit['text'])
        self.assertIn('Thinking at max',edit['text'])
        self.assertEqual(edit['reply_markup'],{'inline_keyboard':[]})

    def test_model_without_effort_updates_original_and_removes_all_buttons(self):
        update=self.menu();sends=len(self.sent);calls=len(self.calls)
        self.tg._handle_model_callback(update,self.state)
        self.assertEqual(len(self.sent),sends)
        edit=next(kw for method,kw in self.calls[calls:] if method=='editMessageText')
        self.assertIn('claude/haiku',edit['text']);self.assertIn('no adjustable',edit['text'])
        self.assertEqual(edit['reply_markup'],{'inline_keyboard':[]})
        self.assertNotIn('effort_menus',self.state)

    def test_repeated_taps_cannot_apply_another_choice_from_consumed_picker(self):
        import json
        update=self.effort_menu()
        model_token=next(iter(self.state['model_menus']))
        replay={'callback_query':{'id':'replay','from':{'id':42},'data':f'model:{model_token}:1',
                'message':self.msg | {'message_id':77}}}
        self.tg._handle_model_callback(replay,self.state)
        self.assertEqual(self.state['threads'][self.key]['model'],'gpt-probe')
        self.tg._handle_effort_callback(update,self.state)
        restored=json.loads(json.dumps(self.state))
        update['callback_query']['data']=update['callback_query']['data'].rsplit(':',1)[0]+':1'
        self.tg._handle_effort_callback(update,restored)
        self.assertEqual(restored['threads'][self.key]['effort'],'ultra')


if __name__=='__main__':unittest.main()
