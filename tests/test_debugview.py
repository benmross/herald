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
            {'timestamp':'2026-01-01T13:00:00Z','type':'user','message':{'content':'Second question'}},
            {'timestamp':'2026-01-01T13:00:05Z','type':'assistant','message':{'content':'Second answer'}}])
        self.assertEqual(data['timing']['observed_ms'],15000)

    def test_missing_timestamps_never_become_zero_duration_measurements(self):
        data=view([{'type':'assistant','message':{'content':[{'type':'tool_use','id':'a','name':'Bash','input':{'command':'pwd'}}]}},
                   {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'a','content':'output'}]}}])
        a=data['activities'][0]
        self.assertIsNone(a.get('duration_ms'))
        self.assertEqual(data['timing']['turns'],[])

if __name__=='__main__':unittest.main()
