"""Real local forms: row identities, remounts, uploads and restart recovery."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'lib'))
from herald import apply, ats, application_checkpoints as cp, browser_workspaces as bw, config, db


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
        self.patches=[mock.patch.object(config,'HOME',self.root),mock.patch.object(config,'LEDGER',self.root/'ledger'),mock.patch.object(config,'DB_PATH',self.root/'facts.db'),mock.patch.object(bw,'admit')]
        self.patches.append(mock.patch.object(config,'link_ledger',return_value=None))
        for p in self.patches:p.start()
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start();self.browser=self.pw.chromium.launch(headless=True)
        except Exception as e:self.skipTest(str(e).splitlines()[0])
        self.page=self.browser.new_page();self.workspace=bw.create()['id']
        with db.session() as c:self.aid=apply.record(c,url='https://jobs.example/role',company='Example')
        self.resume=self.root/'resume.txt';self.resume.write_text('Approved resume version one')

    def tearDown(self):
        if hasattr(self,'browser'):self.browser.close()
        if hasattr(self,'pw'):self.pw.stop()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def adapter(self,resume=''):
        a=ats.Generic(self.page,{'profile':{'first_name':'Ada'}},'Example',{},resume=resume)
        a.checkpoint=cp.Recorder(self.aid,self.workspace,self.page,a)
        return a

    def test_secret_values_never_cross_scan_or_checkpoint(self):
        self.page.set_content('''<label for=p>Password</label><input id=p type=password value="super-secret-password">
          <label for=ssn>SSN</label><input id=ssn value="123-45-6789">
          <textarea id=h-captcha-response>captcha-secret-value</textarea>
          <label for=n>First name</label><input id=n value=Ada>''')
        a=self.adapter();fields=a.scan();text=json.dumps([cp.safe_field(f) for f in fields])
        for secret in ['super-secret-password','123-45-6789','captcha-secret-value']:self.assertNotIn(secret,text)
        a.checkpoint.snapshot('saved')
        saved=cp.latest(self.aid)
        self.assertNotIn('123-45-6789',json.dumps(saved));self.assertNotIn('captcha-secret-value',json.dumps(saved))
        protected=next(f for f in fields if f.get('field_id')=='ssn')
        self.assertFalse(a.set(protected,'000-00-0000'))
        self.assertEqual(self.page.locator('#ssn').input_value(),'123-45-6789')

    def test_repeated_years_and_remount_keep_distinct_values(self):
        self.page.set_content('''<label for=start>Year</label><input id=start>
          <label for=end>Year</label><input id=end>''')
        a=self.adapter();fields=a.scan();self.assertTrue(a.set(fields[0],'2026'))
        # Rebuild controls in another order while preserving their semantic IDs.
        self.page.set_content('''<label for=end>Year</label><input id=end>
          <label for=start>Year</label><input id=start value=2026>''')
        # IDs distinguish fields even when both visible labels are Year.
        end=next(f for f in a.scan() if f['field_id']=='end')
        self.assertTrue(a.set(end,'2029'))
        payload=cp.latest(self.aid)['payload']
        self.assertEqual(len(payload['writes']),2)
        self.assertEqual(self.page.locator('#start').input_value(),'2026')
        self.assertEqual(self.page.locator('#end').input_value(),'2029')

    def test_checkpoint_survives_actual_browser_loss_and_restores_attachment(self):
        html='''<label for=n>First name</label><input id=n required>
          <label for=essay>Why this role?</label><textarea id=essay required></textarea>
          <label for=resume>Resume</label><input type=file id=resume required>
          <button type=button>Submit</button>'''
        self.page.set_content(html);a=self.adapter(str(self.resume))
        for f in a.scan():
            if f['label']=='First name':self.assertTrue(a.set(f,'Ada'))
            if f['label']=='Why this role?':self.assertTrue(a.set(f,'An approved, specific answer.'))
            if f['kind']=='file':self.assertTrue(a.set(f,str(self.resume)))
        a.checkpoint.snapshot('page')
        # Destroy the browser, not only the Python adapter.
        self.browser.close();bw.note_browser(self.workspace,'old');bw.note_browser(self.workspace,'new')
        self.resume.write_text('A later resume must not replace the approved version')
        self.browser=self.pw.chromium.launch(headless=True);self.page=self.browser.new_page();self.page.set_content(html)
        restored=self.adapter(str(self.resume))
        self.assertTrue(restored.checkpoint.recovering)
        restored.checkpoint.restore_uploads()
        for f in restored.scan():
            value,found=restored.checkpoint.saved_value(f)
            if found:self.assertTrue(restored.set(f,value))
        self.assertEqual(self.page.locator('#essay').input_value(),'An approved, specific answer.')
        self.assertEqual(self.page.locator('#n').input_value(),'Ada')
        info=restored.checkpoint.artifacts[0]
        self.assertEqual(pathlib.Path(cp.artifact_path(info)).read_text(),'Approved resume version one')
        self.assertEqual(restored.checkpoint.verify_review({'done':True,'pending':[],'failed':[]}),[])
        self.assertTrue(cp.latest(self.aid)['verified'])

    def test_review_detects_late_parser_changes_and_does_not_submit(self):
        self.page.set_content('''<label for=n>First name</label><input id=n required>
          <button onclick="window.submitted=true">Submit</button>''')
        a=self.adapter();self.assertTrue(a.set(a.scan()[0],'Ada'))
        self.page.locator('#n').fill('Wrong parser result')
        failures=a.checkpoint.verify_review({'done':True,'pending':[],'failed':[]})
        self.assertTrue(failures);self.assertFalse(cp.latest(self.aid)['verified'])
        self.assertIsNone(self.page.evaluate('window.submitted'))

    def test_artifact_cannot_be_replaced_or_redirected(self):
        info=cp.artifact(self.resume)
        with self.assertRaises(ValueError):cp.artifact_path({**info,'path':'/etc/passwd'})
        pathlib.Path(info['path']).write_text('tampered')
        with self.assertRaises(ValueError):cp.artifact_path(info)
