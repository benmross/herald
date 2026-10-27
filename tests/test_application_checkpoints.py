"""Real local forms: row identities, remounts, uploads and restart recovery."""
import json
import pathlib
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'lib'))
from herald import apply, ats, application_checkpoints as cp, browser_workspaces as bw, browse, config, db


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.patches=[mock.patch.object(config,'HOME',self.root),mock.patch.object(config,'LEDGER',self.root/'ledger'),mock.patch.object(config,'DB_PATH',self.root/'facts.db'),mock.patch.object(bw,'admit')]
        self.patches.append(mock.patch.object(config,'link_ledger',return_value=None))
        for p in self.patches:
            p.start();self.addCleanup(p.stop)
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start();self.addCleanup(self.pw.stop)
            self.browser=self.pw.chromium.launch(headless=True)
            self.addCleanup(lambda:self.browser.close())
        except Exception as e:self.skipTest(str(e).splitlines()[0])
        self.page=self.browser.new_page();self.workspace=bw.create()['id']
        with db.session() as c:self.aid=apply.record(c,url='https://jobs.example/role',company='Example')
        self.resume=self.root/'resume.txt';self.resume.write_text('Approved resume version one')

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

    def test_existing_attachment_named_on_the_page_is_recorded(self):
        # An earlier run uploaded it without a record (Workday keeps files
        # server-side). The page naming the current file is accepted, and the
        # file is recorded as this draft's attachment.
        self.page.set_content('<div>resume.txt</div><button type=button>Submit</button>')
        a=self.adapter(str(self.resume))
        issues=a.checkpoint.verify_review({'done':True,'filled':[],'pending':[],'failed':[]})
        self.assertFalse(any('Attachment' in x for x in issues))
        self.assertEqual([x['name'] for x in a.checkpoint.artifacts],['resume.txt'])

    def test_existing_attachment_missing_from_the_page_is_not_ready(self):
        self.page.set_content('<div>no file here</div><button type=button>Submit</button>')
        a=self.adapter(str(self.resume))
        issues=a.checkpoint.verify_review({'done':True,'filled':[],'pending':[],'failed':[]})
        self.assertTrue(any('Attachment must be restored' in x for x in issues))
        self.assertFalse(cp.latest(self.aid)['verified'])

    def test_two_human_browsers_share_controller_runtime_and_close_independently(self):
        other=bw.create()['id'];workspaces=[self.workspace,other]
        browsers=[];urls={};sessions=[]
        try:
            for workspace in workspaces:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                browsers.append(self.pw.chromium.launch(headless=True,args=['--remote-debugging-port='+str(port)]))
                urls[workspace]='http://127.0.0.1:'+str(port)
                bw.take_control(workspace,review=True)
            with mock.patch.object(browse,'ensure'),mock.patch.object(browse,'cdp_url',side_effect=lambda w:urls[w]):
                for i,workspace in enumerate(workspaces):
                    s=browse.Session(workspace,human=True,playwright=self.pw)
                    sessions.append(s)
                    page=s.ctx.new_page();page.set_content('<input id=n>');page.locator('#n').fill(str(i))
                sessions[0].close();sessions.pop(0)
                self.assertEqual(sessions[0].pages()[-1].locator('#n').input_value(),'1')
                sessions[0].close();sessions.clear()
                # Closing borrowed sessions must not stop the shared runtime.
                self.assertTrue(self.browser.is_connected())
        finally:
            for s in sessions:s.close()
            for browser in browsers:browser.close()

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


class FixtureCleanup(unittest.TestCase):
    def test_unavailable_chromium_restores_patches_when_setup_skips(self):
        original_home=config.HOME;original_admit=bw.admit;original_link=config.link_ledger
        api=types.ModuleType('playwright.sync_api')
        pw=mock.Mock()
        pw.chromium.launch.side_effect=RuntimeError('Chromium is unavailable')
        api.sync_playwright=mock.Mock()
        api.sync_playwright.return_value.start.return_value=pw
        with mock.patch.dict(sys.modules,{'playwright':types.ModuleType('playwright'),'playwright.sync_api':api}):
            case=Recovery('test_secret_values_never_cross_scan_or_checkpoint')
            result=unittest.TestResult();case.run(result)
        self.assertEqual(len(result.skipped),1)
        self.assertEqual(result.errors,[])
        self.assertIs(config.HOME,original_home)
        self.assertIs(bw.admit,original_admit)
        self.assertIs(config.link_ledger,original_link)
        pw.stop.assert_called_once_with()
        self.assertFalse(case.root.exists())
