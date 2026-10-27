"""Process ownership, durable review protection and restart invalidation."""
import multiprocessing
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
from herald import apply, browser_workspaces as bw, config, db


def try_lease(workspace, queue):
    try:
        with bw.lease(workspace, timeout=0.1):
            queue.put('acquired')
    except bw.Busy:
        queue.put('busy')


class Workspaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(config, 'HOME', root),
                        mock.patch.object(config, 'LEDGER', root/'ledger'),
                        mock.patch.object(config, 'DB_PATH', root/'facts.db'),
                        mock.patch.object(bw, 'available_memory_mb', return_value=12000)]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def app(self, workspace, n=1, verified=True):
        with db.session() as c:
            aid = apply.record(c, url=f'https://jobs{n}.example/{n}', company='Example')
            apply.set_state(c, aid, status='filled', pending=[])
            c.execute('INSERT INTO application_checkpoints(app_id,ts,workspace,generation,stage,payload,verified) VALUES (?,?,?,?,?,?,?)',
                      (aid, db.now(), workspace, bw.get(workspace)['generation'], 'review', '{}', verified))
        bw.bind(aid, workspace, 'tab'+str(n))
        return aid

    def test_workspace_process_lock_and_independent_workspace(self):
        if 'fork' not in multiprocessing.get_all_start_methods():self.skipTest('flock/fork test')
        a = bw.create()['id'];b = bw.create()['id']
        ctx = multiprocessing.get_context('fork');q=ctx.Queue()
        with bw.lease(a):
            p=ctx.Process(target=try_lease,args=(a,q));p.start();p.join(3)
            self.assertEqual(q.get(timeout=1),'busy')
            p=ctx.Process(target=try_lease,args=(b,q));p.start();p.join(3)
            self.assertEqual(q.get(timeout=1),'acquired')
        with bw.lease(a,timeout=0.1):pass

    def test_human_reservation_survives_release_and_new_session(self):
        w=bw.create()['id'];bw.take_control(w,review=True)
        with self.assertRaises(bw.Busy):
            with bw.lease(w):pass
        with bw.lease(w,human=True):pass
        bw.release_control(w)
        self.assertEqual(bw.get(w)['state'],'review')

    def test_login_return_releases_only_login_ownership(self):
        w=bw.create()['id'];bw.take_control(w);bw.release_control(w)
        self.assertEqual(bw.get(w)['state'],'agent')

    def test_unverified_or_old_checkpoint_cannot_be_handed_over(self):
        w=bw.create()['id'];aid=self.app(w,verified=False)
        self.assertIsNone(bw.ready_group(w,partial=True))
        with db.session() as c:c.execute('UPDATE application_checkpoints SET verified=1')
        group=bw.ready_group(w,partial=True)
        self.assertIsNotNone(group)
        bw.note_browser(w,'first');bw.note_browser(w,'second')
        self.assertEqual(bw.group_info(group)['state'],'recovery')
        self.assertEqual(bw.get(w)['generation'],1)

    def test_review_is_atomic_and_same_account_cannot_be_written(self):
        w=bw.create()['id'];aid=self.app(w);group=bw.ready_group(w,partial=True)
        self.assertIsNone(bw.ready_group(w,partial=True))
        other=bw.create()['id']
        with self.assertRaises(bw.Busy):bw.assert_agent(other,'https://jobs1.example/second')
        bw.assert_agent(other,'https://other.example/job')
        with self.assertRaises(bw.Busy):bw.finish_group(group)
        bw.mark_submitted(group,aid)
        self.assertEqual(bw.finish_group(group),w)
        self.assertEqual(bw.get(w)['state'],'closed')

    def test_memory_and_workspace_caps_preserve_existing_work(self):
        w=bw.create()['id']
        with mock.patch.object(bw,'available_memory_mb',return_value=100):
            with self.assertRaises(bw.Capacity):bw.create()
        self.assertEqual(bw.get(w)['state'],'agent')
        bw.create()
        with self.assertRaises(bw.Capacity):bw.create()
