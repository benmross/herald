"""Exercise the actual Telegram choice state machine without models or network."""
import json
import time
import types
import unittest
from unittest import mock

import test_reply_resume as reply
from herald import claude_accounts, limitchoice, limitresume

KEY,CHAT,TOPIC=reply.KEY,reply.CHAT,reply.TOPIC


class AccountChoices(unittest.TestCase):
    setUpClass=classmethod(reply.ReplyResume.setUpClass.__func__)
    say=reply.ReplyResume.say

    def setUp(self):
        reply.ReplyResume.setUp(self)
        self.source={'email':'current@example.com','organizationUuid':'source'}
        self.target={'email':'other@example.com','organizationUuid':'target'}
        self.bound=self.source
        original_get=self.tg.config.get
        self.candidate={'source':self.source,'target':self.target,'target_label':'Other account',
                        'cold_tokens':100000,'required_percent':40,'available_percent':90,
                        'target_pressure':5,'source_pressure':100,'drift_percent':1}
        self.inspection={'candidate':self.candidate,'account_count':2,'reason':''}
        def commit(*args,**kwargs):self.bound=self.target
        patches=[mock.patch.object(limitchoice,'account_plan',side_effect=lambda *a,**k:self.inspection),
                 mock.patch.object(limitchoice,'capacity',return_value=None),
                 mock.patch.object(limitresume,'reset_at',return_value=time.time()+3600),
                 mock.patch.object(self.tg,'_checkpoint'),
                 mock.patch.object(self.tg.config,'get',side_effect=lambda k,d=None:
                    {'engines.claude.model':'opus','engines.claude.effort':'high'}.get(k,original_get(k,d))),
                 mock.patch.object(claude_accounts,'binding',side_effect=lambda *a:self.bound),
                 mock.patch.object(claude_accounts,'commit_switch',side_effect=commit),
                 mock.patch.object(claude_accounts,'release')]
        self.commit=patches[-2].start();self.release=patches[-1].start()
        for p in patches[:-2]:p.start()
        self.addCleanup(lambda:[p.stop() for p in patches])

    def limited(self,error="You've hit your 5-hour limit",context=250000):
        failure=types.SimpleNamespace(ok=False,cancelled=False,rate_limited=True,text='',
            session_id='stopped',cost_usd=None,error=error,context_tokens=context)
        with mock.patch.object(self.tg.think,'think',return_value=failure):
            self.say('finish the current task')
        return self.state['threads'][KEY]

    def tap(self,action):
        record=self.state['threads'][KEY][limitchoice.KEY]
        cq={'id':'tap','from':{'id':42},'data':f"limit:{record['token']}:{action}",
            'message':{'message_id':record['message_id'],'text':record['text'],
                       'chat':{'id':CHAT},'message_thread_id':TOPIC}}
        self.tg._handle_limit_callback({'callback_query':cq},self.state)

    def run_due(self):
        record=self.state['threads'][KEY][limitresume.KEY]
        self.tg._run_limit_resume(self.state,KEY,record)
        return record

    def test_hard_limit_waits_one_minute_then_switches_once_with_same_session_and_model(self):
        ts=self.limited()
        ts.update(model='opus',effort='high')
        ts[limitchoice.KEY].update(model='opus',effort='high')
        self.state=json.loads(json.dumps(self.state))
        ts=self.state['threads'][KEY];card=ts[limitchoice.KEY]
        self.assertEqual(ts['context_tokens'],250000)
        self.assertIn('one minute',card['text'])
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']-1):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.assertNotIn(limitresume.KEY,ts)
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1):
            self.tg._default_limit_choice(self.state,KEY,card)
            self.tg._default_limit_choice(self.state,KEY,card)
        self.commit.assert_not_called() # The queued job checks again at actual launch.
        queued=self.run_due()
        self.tg._run_limit_resume(self.state,KEY,queued)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['resume'],'stopped')
        self.assertEqual(self.calls[0]['model'],'opus')
        self.assertEqual(self.calls[0]['effort'],'high')
        self.commit.assert_called_once()
        self.release.assert_called_once()
        self.assertIn('Earlier approvals do not carry over',self.prompts[0])

    def test_account_button_does_not_wait_for_timeout(self):
        self.limited();self.tap('account');self.run_due()
        self.assertEqual(len(self.calls),1)
        self.commit.assert_called_once()

    def test_capacity_disappears_at_timeout_so_default_waits(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        self.inspection={'candidate':None,'account_count':2,'reason':'Resume too large'}
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1), \
             mock.patch.object(limitchoice,'wait_at',return_value=card['at']) as wait:
            self.tg._default_limit_choice(self.state,KEY,card)
        self.assertTrue(ts[limitresume.KEY]['reconsider_accounts'])
        self.assertEqual(ts[limitresume.KEY]['at'],card['at'])
        self.assertTrue(wait.call_args.kwargs['earliest'])
        self.commit.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_capacity_disappears_while_job_is_queued_so_no_model_is_started(self):
        ts=self.limited();self.tap('account')
        self.inspection={'candidate':None,'account_count':2,'reason':'Resume no longer fits'}
        self.run_due()
        self.commit.assert_not_called()
        self.assertEqual(self.calls,[])
        self.assertIn(limitchoice.KEY,ts)
        self.assertNotIn('default_action',ts[limitchoice.KEY])

    def test_warning_never_defaults_and_keep_preserves_the_original_pending_request(self):
        self.say('first turn');self.calls.clear();self.prompts.clear()
        self.say('complete the second request exactly')
        ts=self.state['threads'][KEY];card=ts[limitchoice.KEY]
        self.assertEqual(card['mode'],'warning')
        self.assertIsNone(card['default_at'])
        self.assertEqual(self.calls,[])
        self.tg._default_limit_choice(self.state,KEY,card)
        self.assertNotIn(limitresume.KEY,ts)
        self.tap('keep');self.run_due()
        self.assertEqual(len(self.calls),1)
        self.assertIn('complete the second request exactly',self.prompts[0])
        self.commit.assert_not_called()

    def test_warning_account_choice_preserves_pending_request(self):
        self.say('first turn');self.calls.clear();self.prompts.clear()
        self.say('do the new task with the attachment')
        self.state=json.loads(json.dumps(self.state))
        self.tap('account');self.run_due()
        self.commit.assert_called_once()
        self.assertEqual(len(self.calls),1)
        self.assertIn('do the new task with the attachment',self.prompts[0])

    def test_stop_during_recheck_wins_over_the_timeout(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        def stop(*a,**k):
            limitresume.cancel(ts)
            ts['last_ts']+=1
            return self.inspection
        with mock.patch.object(limitchoice,'account_plan',side_effect=stop), \
             mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.assertNotIn(limitresume.KEY,ts)
        self.commit.assert_not_called()

    def test_transient_overload_does_not_arm_an_account_timeout(self):
        ts=self.limited('API Error: 529 Overloaded. This is a server-side issue.')
        self.assertNotIn('default_action',ts[limitchoice.KEY])

    def test_new_message_invalidates_the_old_timeout(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        self.inspection={'candidate':None,'account_count':2,'reason':''}
        self.say('different instruction')
        before=len(self.calls)
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.assertEqual(len(self.calls),before)
        self.commit.assert_not_called()

    def test_a_message_already_received_wins_during_a_slow_capacity_read(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        def replied(*a,**k):
            with mock.patch.object(self.tg,'_handle_usage'):
                self.tg._dispatch({'update_id':900,'message':{'message_id':900,'chat':{'id':CHAT},
                    'message_thread_id':TOPIC,'from':{'id':42},'text':'/usage'}},self.state)
            return self.inspection
        with mock.patch.object(limitchoice,'account_plan',side_effect=replied), \
             mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.assertNotIn(limitresume.KEY,ts)
        self.commit.assert_not_called()

    def test_wait_rechecks_the_other_account_at_its_reset(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        self.inspection={'candidate':None,'account_count':2,'reason':'Both full'}
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1), \
             mock.patch.object(limitchoice,'wait_at',return_value=card['default_at']+600):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.inspection={'candidate':self.candidate,'account_count':2,'reason':''}
        with mock.patch.object(claude_accounts,'enabled',return_value=True), \
             mock.patch.object(claude_accounts,'select',return_value={}), \
             mock.patch.object(claude_accounts,'pressure',return_value=100):
            self.run_due()
        self.assertEqual(len(self.calls),1)
        self.commit.assert_called_once()

    def test_the_user_gets_a_full_minute_after_slow_card_delivery(self):
        clock=[time.time()]
        original_api=self.tg.api
        def slow(method,**kwargs):
            if method=='sendMessage' and 'This task is paused.' in kwargs.get('text',''):
                clock[0]+=25
            return original_api(method,**kwargs)
        with mock.patch.object(self.tg.time,'time',side_effect=lambda:clock[0]), \
             mock.patch.object(self.tg,'api',side_effect=slow):
            ts=self.limited()
        self.assertAlmostEqual(ts[limitchoice.KEY]['default_at'],clock[0]+60)

    def test_continuation_does_not_follow_a_default_model_change_after_the_card(self):
        ts=self.limited();self.tap('account')
        self.assertIsNone(ts.get('model'))
        original_get=self.tg.config.get
        with mock.patch.object(self.tg.config,'get',side_effect=lambda k,d=None:
                'sonnet' if k=='engines.claude.model' else original_get(k,d)):
            self.run_due()
        self.assertEqual(self.calls[0]['model'],'opus')
        self.assertEqual(self.calls[0]['effort'],'high')
        self.assertIsNone(ts.get('model'))

    def test_current_account_recovered_during_the_minute_keeps_its_cache(self):
        ts=self.limited();card=ts[limitchoice.KEY]
        self.inspection={'candidate':None,'account_count':2,'current_ready':True,'reason':'Reset already happened'}
        with mock.patch.object(self.tg.time,'time',return_value=card['default_at']+1):
            self.tg._default_limit_choice(self.state,KEY,card)
        self.run_due()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['resume'],'stopped')
        self.commit.assert_not_called()

    def test_recovery_between_model_failure_and_card_delivery_keeps_the_failed_context(self):
        ts=self.limited(context=420000)
        row=dict(self.con.execute('SELECT * FROM telegram_turns ORDER BY created_at DESC LIMIT 1').fetchone())
        ts.pop(limitchoice.KEY)
        ts['context_tokens']=10000
        self.assertTrue(self.tg._saved_answer(row,self.state))
        self.assertEqual(ts['context_tokens'],420000)
        self.assertIn('default_at',ts[limitchoice.KEY])
        self.assertGreater(ts[limitchoice.KEY]['default_at'],time.time()+55)
        self.assertEqual(self.calls,[])
