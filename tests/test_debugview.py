"""Semantic log views must neither double-count time nor hide original evidence."""
import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'lib'))
from herald.debugui import normalize
from herald.debugview import build


def event(second, payload, outer='response_item'):
    return {'timestamp':f'2026-01-01T12:{second//60:02d}:{second%60:02d}.000Z', 'type':outer, 'payload':payload}


def view(records):
    return build([normalize(r,i+1) for i,r in enumerate(records)])


class SemanticTimeline(unittest.TestCase):
    def test_parallel_tools_count_once_and_idle_between_turns_is_excluded(self):
        data=view([
            event(0,{'type':'task_started','turn_id':'one'}),
            event(2,{'type':'function_call','call_id':'a','name':'exec_command','arguments':'first'}),
            event(4,{'type':'function_call','call_id':'b','name':'exec_command','arguments':'second'}),
            event(6,{'type':'function_call_output','call_id':'a','output':'first result'}),
            event(8,{'type':'function_call_output','call_id':'b','output':'second result'}),
            event(10,{'type':'task_complete'}),
            event(120,{'type':'task_started','turn_id':'two'}),
            event(125,{'type':'task_complete'})])
        self.assertEqual(data['timing']['observed_ms'],15000)
        self.assertEqual(data['timing']['tool_ms'],6000)
        self.assertEqual(data['timing']['between_ms'],9000)
        self.assertEqual(len(data['timing']['turns']),2)
        tools=[a for a in data['activities'] if a['kind']=='tool']
        self.assertEqual(len(tools),2)
        self.assertEqual(tools[0]['output'],'first result')
        self.assertEqual(len(tools[0]['records']),2)

    def test_missing_result_is_unconfirmed_not_model_time(self):
        data=view([event(0,{'type':'task_started','turn_id':'one'}),
                   event(2,{'type':'function_call','call_id':'a','name':'slow','arguments':'start'}),
                   event(10,{'type':'task_complete'})])
        self.assertEqual(data['timing']['unconfirmed_ms'],8000)
        self.assertEqual(data['timing']['between_ms'],2000)
        self.assertIsNone(next(a for a in data['activities'] if a['kind']=='tool').get('duration_ms'))

    def test_repeated_instructions_and_mirrored_messages_are_not_new_work(self):
        instruction={'type':'message','role':'developer','content':[{'type':'input_text','text':'Shared instruction'}]}
        data=view([event(0,{'type':'task_started','turn_id':'one'}),event(1,instruction),event(2,instruction),
                   event(3,{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Hello'}]}),
                   event(3,{'type':'item_completed','item':{'type':'AgentMessage','content':[{'text':'Hello'}]}})])
        visible=[a for a in data['activities'] if not a['technical']]
        self.assertEqual(sum(a['kind']=='assistant' for a in visible),1)
        self.assertEqual(sum(a['kind']=='context' for a in visible),1)
        self.assertEqual(next(a for a in visible if a['kind']=='context')['repeats'],1)
        repeated=next(a for a in data['activities'] if a.get('repeat_of'))
        self.assertTrue(repeated['records'])
        self.assertEqual(data['raw_count'],5)

    def test_dots_link_to_nearby_activity_without_claiming_causation(self):
        data=view([event(0,{'type':'task_started','turn_id':'one'}),
                   event(1,{'type':'function_call','name':'exec_command','call_id':'a','arguments':'pwd'}),
                   event(2,{'type':'function_call_output','call_id':'a','output':'directory'}),
                   event(2,{'type':'token_count','info':{'last_token_usage':{'input_tokens':1000},'model_context_window':4000}}),
                   event(3,{'type':'token_count','info':{'last_token_usage':{'input_tokens':1200}}})])
        p=data['context']
        self.assertEqual(p[0]['anchor_seq'],2)
        self.assertEqual(p[0]['turn'],'one')
        self.assertEqual(p[0]['tools'],1)
        self.assertEqual(p[1]['delta'],200)

    def test_claude_inferred_turns_do_not_include_idle_before_next_message(self):
        data=view([
            {'timestamp':'2026-01-01T12:00:00Z','type':'user','message':{'content':'First question'}},
            {'timestamp':'2026-01-01T12:00:10Z','type':'assistant','message':{'content':'First answer'}},
            {'timestamp':'2026-01-01T12:00:10Z','type':'system','subtype':'stop_hook_summary','preventedContinuation':False},
            {'timestamp':'2026-01-01T13:00:00Z','type':'user','message':{'content':'Second question'}},
            {'timestamp':'2026-01-01T13:00:05Z','type':'assistant','message':{'content':'Second answer'}}])
        self.assertEqual(data['timing']['observed_ms'],15000)

    def test_claude_tool_results_and_steering_stay_in_the_active_turn(self):
        def claude(second,role,content,**extra):
            return {'timestamp':f'2026-01-01T12:00:{second:02d}Z','type':role,'message':{'content':content},**extra}
        data=view([
            claude(0,'user',[{'type':'text','text':'First request'}]),
            claude(1,'assistant',[{'type':'tool_use','id':'a','name':'Bash','input':{'command':'pwd'}}]),
            claude(3,'user',[{'type':'tool_result','tool_use_id':'a','content':'A plain text tool result'}]),
            claude(4,'assistant',[{'type':'text','text':'Still working.'}]),
            claude(5,'user',[{'type':'text','text':'Use this other approach'}]),
            claude(6,'user',[{'type':'text','text':'Injected engine context'}],isMeta=True),
            claude(7,'assistant',[{'type':'tool_use','id':'b','name':'Read','input':{'file_path':'file.txt'}}]),
            claude(9,'user',[{'type':'tool_result','tool_use_id':'b','content':[{'type':'text','text':'Nested result'}]}]),
            claude(10,'assistant',[{'type':'text','text':'Done'}]),
            {'timestamp':'2026-01-01T12:00:10Z','type':'system','subtype':'stop_hook_summary','preventedContinuation':False},
            claude(20,'user',[{'type':'text','text':'A separate request'}]),
            claude(22,'assistant',[{'type':'text','text':'Second answer'}]),
            {'timestamp':'2026-01-01T12:00:22Z','type':'system','subtype':'stop_hook_summary','preventedContinuation':False}])
        turns=data['timing']['turns']
        self.assertEqual(len(turns),2)
        self.assertTrue(all(t['complete'] for t in turns))
        self.assertEqual(data['timing']['observed_ms'],12000)
        self.assertEqual(turns[0]['tool_ms'],4000)
        users=[a for a in data['activities'] if a['kind']=='user']
        self.assertEqual(len(users),3)
        self.assertEqual(users[0]['turn'],users[1]['turn'])
        self.assertEqual(users[1]['name'],'Your steering update')
        self.assertNotEqual(users[1]['turn'],users[2]['turn'])
        self.assertEqual([t['messages'] for t in turns],[2,1])

    def test_codex_steering_is_one_turn_until_completion(self):
        data=view([event(0,{'type':'task_started','turn_id':'one'}),
                   event(1,{'type':'message','role':'user','content':[{'type':'input_text','text':'First request'}]}),
                   event(2,{'type':'message','role':'user','content':[{'type':'input_text','text':'Steering update'}]}),
                   event(5,{'type':'task_complete'})])
        self.assertEqual(len(data['timing']['turns']),1)
        self.assertEqual(data['timing']['turns'][0]['messages'],2)
        self.assertEqual([a['name'] for a in data['activities'] if a['kind']=='user'],['Your message','Your steering update'])

    def test_blocked_stop_hook_does_not_end_a_turn(self):
        data=view([
            {'timestamp':'2026-01-01T12:00:00Z','type':'user','message':{'content':'Request'}},
            {'timestamp':'2026-01-01T12:00:01Z','type':'system','subtype':'stop_hook_summary','preventedContinuation':True},
            {'timestamp':'2026-01-01T12:00:02Z','type':'user','message':{'content':'Steering'}},
            {'timestamp':'2026-01-01T12:00:03Z','type':'result','result':'Done'}])
        self.assertEqual(len(data['timing']['turns']),1)
        self.assertTrue(data['timing']['turns'][0]['complete'])
        self.assertEqual(data['timing']['turns'][0]['messages'],2)

    def test_missing_timestamps_never_become_zero_duration_measurements(self):
        data=view([{'type':'assistant','message':{'content':[{'type':'tool_use','id':'a','name':'Bash','input':{'command':'pwd'}}]}},
                   {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'a','content':'output'}]}}])
        a=data['activities'][0]
        self.assertIsNone(a.get('duration_ms'))
        self.assertEqual(data['timing']['turns'],[])

if __name__=='__main__':unittest.main()
