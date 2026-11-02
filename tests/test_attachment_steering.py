"""Files sent during active work must reach it, without duplicating downloads."""
import pathlib
import queue
import tempfile
import unittest
from unittest import mock

import test_reply_resume as base
from herald import think


class Attachments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg = base._load()

    setUp_ = base.ReplyResume.setUp

    def setUp(self):
        self.setUp_()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.tg._private.clear)
        self.root = pathlib.Path(self.tmp.name)
        self.downloads = []
        def download(identity, suggested, folder=None):
            self.downloads.append((identity, suggested, folder))
            return (folder or self.root) / suggested
        for patch in (
            mock.patch.object(self.tg, 'download', side_effect=download),
            mock.patch.object(self.tg.think, 'steering_target', return_value=mock.sentinel.active_run),
            mock.patch.object(self.tg.think, 'steer', return_value=True),
            mock.patch.object(self.tg.think, 'current_progress', return_value='working'),
            mock.patch('builtins.print'),
        ):
            patch.start();self.addCleanup(patch.stop)

    def update(self, **extra):
        return {'update_id':17,'message':{'message_id':17,'chat':{'id':base.CHAT,'type':'supergroup'},
                'message_thread_id':base.TOPIC,'from':{'id':42},'date':1700000000,**extra}}

    def test_photo_caption_and_largest_image_reach_active_turn(self):
        update=self.update(caption='Fix what this screenshot shows',photo=[{'file_id':'small','file_size':1},{'file_id':'large','file_size':20}])
        self.assertTrue(self.tg._try_steer(update,base.KEY,self.state))
        self.assertEqual(self.downloads[0][0],'large')
        args=self.tg.think.steer.call_args
        self.assertIn('large.jpg',args.args[1])
        self.assertIn('Fix what this screenshot shows',args.args[1])
        self.assertIn('steering update to the active request',args.args[1])
        self.assertEqual(args.kwargs['target'],mock.sentinel.active_run)

    def test_document_without_caption_can_steer(self):
        update=self.update(document={'file_id':'doc','file_name':'notes.pdf'})
        self.assertTrue(self.tg._try_steer(update,base.KEY,self.state))
        self.assertIn('notes.pdf',self.tg.think.steer.call_args.args[1])
        self.assertIn('without a caption',self.tg.think.steer.call_args.args[1])

    def test_album_is_one_steer_with_all_files_and_the_caption(self):
        first=self.update(photo=[{'file_id':'one'}])
        second=self.update(caption='Read both images',photo=[{'file_id':'two'}])
        merged=dict(first,_album=[first['message'],second['message']])
        self.assertTrue(self.tg._try_steer(merged,base.KEY,self.state))
        prompt=self.tg.think.steer.call_args.args[1]
        self.assertIn('one.jpg',prompt);self.assertIn('two.jpg',prompt)
        self.assertIn('Read both images',prompt)
        self.tg.think.steer.assert_called_once()

    def test_finished_turn_falls_back_without_downloading_again(self):
        self.tg.think.steer.return_value=False
        update=self.update(photo=[{'file_id':'one'}],caption='Follow up')
        self.assertFalse(self.tg._try_steer(update,base.KEY,self.state))
        self.tg.handle(update,self.state)
        self.assertEqual(len(self.downloads),1)
        self.assertEqual(len(self.prompts),1)
        self.assertIn('one.jpg',self.prompts[0])

    def test_no_active_turn_does_not_download_until_normal_handling(self):
        self.tg.think.steering_target.return_value=None
        self.assertFalse(self.tg._try_steer(self.update(photo=[{'file_id':'one'}]),base.KEY,self.state))
        self.assertEqual(self.downloads,[])

    def test_unauthorized_sender_is_checked_before_download(self):
        update=self.update(photo=[{'file_id':'one'}]);update['message']['from']['id']=999
        self.assertFalse(self.tg._try_steer(update,base.KEY,self.state))
        self.assertEqual(self.downloads,[])

    def test_download_failure_reports_once_and_never_injects(self):
        self.tg.download.side_effect=None;self.tg.download.return_value=None
        self.assertTrue(self.tg._try_steer(self.update(photo=[{'file_id':'one'}]),base.KEY,self.state))
        self.tg.think.steer.assert_not_called()
        self.assertEqual(len(self.sent),1)

    def test_private_attachment_uses_only_scratch(self):
        self.tg._private[base.KEY]={'scratch':str(self.root/'scratch')}
        update=self.update(photo=[{'file_id':'private'}])
        self.assertTrue(self.tg._try_steer(update,base.KEY,self.state))
        self.assertEqual(self.downloads[0][2],self.root/'scratch'/'inbox')
        self.assertIn('not archived',self.tg.think.steer.call_args.args[1])

    def test_ended_private_session_is_not_injected_into_its_replacement(self):
        self.tg._private[base.KEY]={'scratch':str(self.root/'scratch')}
        def download(*args):
            self.tg._private.pop(base.KEY)
            return self.root/'scratch'/'inbox'/'image.jpg'
        self.tg.download.side_effect=download
        self.assertTrue(self.tg._try_steer(self.update(photo=[{'file_id':'private'}]),base.KEY,self.state))
        self.tg.think.steer.assert_not_called()
        self.assertIn('private session ended',self.sent[0])

    def test_private_fallback_never_archives_after_private_mode_ends(self):
        self.tg._private[base.KEY]={'scratch':str(self.root/'scratch')}
        self.tg.think.steer.return_value=False
        update=self.update(photo=[{'file_id':'private'}])
        self.assertFalse(self.tg._try_steer(update,base.KEY,self.state))
        self.tg._private.pop(base.KEY)
        self.tg.handle(update,self.state)
        self.assertEqual(len(self.downloads),1)
        self.assertEqual(self.calls,[])
        self.assertIn('private session ended',self.sent[0])

    def test_album_flush_tries_steering_before_topic_lock(self):
        update=self.update(photo=[{'file_id':'one'}])
        self.tg._albums['group']=[update]
        with mock.patch.object(self.tg.time,'sleep'),mock.patch.object(self.tg,'_try_steer',return_value=True) as steer, \
             mock.patch.object(self.tg,'_lock_for') as lock, mock.patch.object(self.tg,'handle') as handle, \
             mock.patch.object(self.tg,'_checkpoint'),mock.patch.object(self.tg,'_maybe_restart'):
            self.tg._flush_album('group',self.state)
        steer.assert_called_once();lock.assert_not_called();handle.assert_not_called()


class Binding(unittest.TestCase):
    def test_prepared_input_cannot_jump_to_a_replacement_run(self):
        key='attachment-steer-test'
        old={'accepting_steers':True,'steer_queue':queue.Queue()}
        replacement={'accepting_steers':True,'steer_queue':queue.Queue()}
        with mock.patch.dict(think._active,{key:old}):
            target=think.steering_target(key)
            think._active[key]=replacement
            self.assertFalse(think.steer(key,'old file',target=target))
            self.assertTrue(replacement['steer_queue'].empty())
            self.assertTrue(think.steer(key,'new file',target=think.steering_target(key)))
            self.assertEqual(replacement['steer_queue'].get_nowait(),'new file')


if __name__=='__main__':unittest.main()
