"""Native quota history calibrates a conservative estimate, never a fixed price."""
import datetime as dt
import json
import sqlite3
import time
import unittest
from unittest import mock

from herald import claude_budget as budget, claude_accounts, config, db


class Budget(unittest.TestCase):
    def setUp(self):
        budget._cache.clear()
        self.addCleanup(budget._cache.clear)
        self.con=sqlite3.connect(':memory:')
        self.con.row_factory=sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.con.execute('CREATE TABLE IF NOT EXISTS ledger_changes (seq INTEGER PRIMARY KEY,entity TEXT,record_key TEXT,changed_at TEXT,before_json TEXT,after_json TEXT)')
        self.addCleanup(self.con.close)
        self.evidence=mock.patch.object(claude_accounts,'evidence',return_value=({},[],[]))
        self.evidence.start()
        self.addCleanup(self.evidence.stop)

    def native(self, at, reset, used):
        snapshot={'sampled_at':at,'windows':[{'key':'session','resets_at':reset,'used_percent':used}]}
        outer=json.dumps({'data':json.dumps(snapshot)})
        self.con.execute('INSERT INTO ledger_changes(entity,record_key,changed_at,after_json) VALUES(?,?,?,?)',
                         ('facts','["subscription_usage","snapshot","claude",null]',dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat(),outer))

    def history(self, count=6, *, model='opus', saturated=False):
        reset=round((time.time()-86400)/1800)*1800
        for i in range(count):
            end=reset-i*21600
            self.native(end-18000,end,0)
            self.native(end-1200,end,100 if saturated else 20)
            self.con.execute('INSERT INTO runs(ts,label,engine,model,duration_ms,cache_write,cache_read,output_tokens,exit_code) VALUES(?,?,?,?,?,?,?,?,?)',
                (dt.datetime.fromtimestamp(end-1800,config.tz()).isoformat(),'test','claude',model,60000,100000,500000,10000,0))
        # Establish that the first analyzed window is fully observed.
        self.native(reset-count*21600,reset-count*21600+18000,0)

    def test_insufficient_saturated_or_wrong_model_history_cannot_authorize_a_resume(self):
        self.assertIsNone(budget.calibration(self.con,'opus'))
        budget._cache.clear();self.history(count=4)
        self.assertIsNone(budget.calibration(self.con,'opus'))
        budget._cache.clear()
        self.assertIsNone(budget.calibration(self.con,'unknown-model'))
        self.assertIsNone(budget.calibration(self.con,'haiku'))

    def test_same_model_complete_native_windows_produce_a_conservative_rate(self):
        self.history()
        sample=budget.calibration(self.con,'claude-opus-5-5')
        self.assertEqual(sample['windows'],6)
        # A native 20% observation receives a rounding allowance, rather
        # than pretending that raw token traffic defines quota percentages.
        self.assertGreater(sample['percent_per_unit'],20/300000)

    def test_saturated_windows_are_not_treated_as_complete_demand(self):
        self.history(saturated=True)
        self.assertIsNone(budget.calibration(self.con,'opus'))

    def test_unknown_context_or_calibration_blocks_instead_of_assuming_a_cheap_replay(self):
        for value in (None,0,-1,True,float('nan')):
            with self.subTest(value=value):
                result,_=budget.estimate(self.con,{'context_tokens':value,'model':'opus'})
                self.assertIsNone(result)
        result,_=budget.estimate(self.con,{'context_tokens':100000,'model':'opus'})
        self.assertIsNone(result)

    def test_larger_context_increases_the_budget_and_keeps_working_room(self):
        self.history()
        small,_=budget.estimate(self.con,{'context_tokens':100000,'model':'opus'})
        large,_=budget.estimate(self.con,{'context_tokens':500000,'model':'opus'})
        self.assertGreater(large['required_percent'],small['required_percent'])
        self.assertGreater(small['required_percent'],small['estimated_percent'])
        self.assertGreater(small['cold_tokens'],100000)

    def test_anonymous_quota_cannot_be_calibrated_with_bound_multi_account_work(self):
        self.history()
        self.con.execute("UPDATE runs SET session_id='bound-session'")
        identity=json.dumps({'email':'other@example.com','organizationUuid':'other'})
        with mock.patch.object(claude_accounts,'evidence',return_value=({'bound-session':identity},[],[])):
            self.assertIsNone(budget.calibration(self.con,'opus'))

    def test_labeled_samples_use_the_account_that_owned_the_run_at_that_time(self):
        self.history()
        self.con.execute("UPDATE runs SET session_id='bound-session'")
        source=json.dumps({'email':'source@example.com','organizationUuid':'source'})
        target=json.dumps({'email':'target@example.com','organizationUuid':'target'})
        samples=[]
        plan={'label':'Pro','tier':'standard'}
        for row in self.con.execute('SELECT after_json FROM ledger_changes'):
            data=json.loads(json.loads(row[0])['data']);window=data['windows'][0]
            account={'plan':plan,'usage':{'fiveHour':{'pct':window['used_percent'],
                'resetsAt':dt.datetime.fromtimestamp(window['resets_at'],dt.timezone.utc).isoformat()}}}
            samples.append((source,data['sampled_at'],account))
        evidence=({'bound-session':target},[('bound-session',time.time()-1000,source,target)],samples)
        with mock.patch.object(claude_accounts,'evidence',return_value=evidence):
            self.assertEqual(budget.calibration(self.con,'opus',plan)['windows'],6)
            budget._cache.clear()
            self.assertIsNone(budget.calibration(self.con,'opus',{'label':'Free','tier':'free'}))
