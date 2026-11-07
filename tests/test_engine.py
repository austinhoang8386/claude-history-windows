import tempfile
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import claude_history_windows as app
from localization import get_language, set_language, tr

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
ORG = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SID = '33333333-3333-4333-8333-333333333333'
SID2 = '44444444-4444-4444-8444-444444444444'
RID = 'local_' + SID


class EngineTest(unittest.TestCase):
    def setUp(self):
        set_language('en')
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.root = self.home / 'claude-code-sessions'
        self.src = self.root / A / ORG
        self.dst = self.root / B / ORG
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.transcripts = self.home / '.claude' / 'projects' / 'my-project'
        self.transcripts.mkdir(parents=True)
        (self.transcripts / (SID + '.jsonl')).write_text('{"type":"user"}\n')
        self.card = self.src / (RID + '.json')
        self.card.write_text(json.dumps({'sessionId': RID, 'cliSessionId': SID,
                                         'cwd': 'C:\\work\\demo', 'title': 'Test history'}))
        self.jobs = self.home / 'jobs'

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        accounts = app.accounts_at(self.root)
        src = next(a for a in accounts if a.account == A)
        dst = next(a for a in accounts if a.account == B)
        sessions = app.load_sessions(src, app.transcript_ids(self.transcripts.parent))
        return src, dst, sessions

    def test_copy_and_undo(self):
        src, dst, s = self.scan()
        self.assertTrue(s[0].valid)
        receipt = app.transfer(s, src, dst, 'copy', self.jobs)
        self.assertEqual(receipt['done'], 1)
        self.assertTrue(self.card.exists())
        self.assertEqual(app.filehash(self.card), app.filehash(self.dst / self.card.name))
        undo = app.undo_job(receipt['job'])
        self.assertEqual(undo['removed'], 1)
        self.assertTrue(self.card.exists())
        self.assertFalse((self.dst / self.card.name).exists())

    def test_move_and_undo(self):
        src, dst, s = self.scan()
        receipt = app.transfer(s, src, dst, 'move', self.jobs)
        self.assertFalse(self.card.exists())
        self.assertTrue((self.dst / self.card.name).exists())
        undo = app.undo_job(receipt['job'])
        self.assertEqual(undo['restored'], 1)
        self.assertEqual(undo['removed'], 1)
        self.assertTrue(self.card.exists())

    def test_destination_collision_rejected(self):
        src, dst, s = self.scan()
        (self.dst / self.card.name).write_text('{"different": true}')
        with self.assertRaises(app.SafetyError):
            app.transfer(s, src, dst, 'copy', self.jobs)

    def test_newer_source_replaces_older_destination_and_undo_restores(self):
        old = json.dumps({'sessionId': RID, 'cliSessionId': SID, 'title': 'old',
                          'lastActivityAt': '2026-01-01T00:00:00Z'})
        (self.dst / self.card.name).write_text(old)
        self.card.write_text(json.dumps({'sessionId': RID, 'cliSessionId': SID, 'title': 'new',
                                         'lastActivityAt': '2026-02-01T00:00:00Z'}))
        src, dst, s = self.scan()
        receipt = app.transfer(s, src, dst, 'copy', self.jobs)
        self.assertEqual(receipt['done'], 1)
        self.assertEqual(app.filehash(self.dst / self.card.name), app.filehash(self.card))
        undo = app.undo_job(receipt['job'])
        self.assertEqual(undo['errors'], [])
        self.assertEqual((self.dst / self.card.name).read_text(), old)

    def test_later_focus_breaks_activity_tie(self):
        base = {'sessionId': RID, 'cliSessionId': SID, 'lastActivityAt': 1793772081418}
        (self.dst / self.card.name).write_text(json.dumps({**base, 'lastFocusedAt': 1793771992496}))
        self.card.write_text(json.dumps({**base, 'lastFocusedAt': 1793813059032}))
        src, dst, s = self.scan()
        self.assertEqual(app.transfer(s, src, dst, 'copy', self.jobs)['done'], 1)

    def test_older_source_does_not_replace_newer_destination(self):
        (self.dst / self.card.name).write_text(json.dumps({'sessionId': RID, 'cliSessionId': SID,
                                                           'lastActivityAt': 1893456000000}))
        self.card.write_text(json.dumps({'sessionId': RID, 'cliSessionId': SID,
                                         'lastActivityAt': 1767225600000}))
        src, dst, s = self.scan()
        with self.assertRaises(app.SafetyError):
            app.transfer(s, src, dst, 'copy', self.jobs)

    def test_bad_json_and_missing_transcript(self):
        self.card.write_text('{broken')
        _, _, s = self.scan()
        self.assertFalse(s[0].valid)
        self.assertEqual(s[0].reason, 'Invalid JSON')
        self.card.write_text(json.dumps({'sessionId':RID,'cliSessionId':SID,'title':'No transcript'}))
        (self.transcripts / (SID + '.jsonl')).unlink()
        _, _, s = self.scan()
        self.assertFalse(s[0].valid)
        self.assertEqual(s[0].reason, 'Local transcript not found')

    def test_parent_missing_prevents_copy(self):
        self.card.write_text(json.dumps({'sessionId':RID,'cliSessionId':SID,
                                         'forkedFromSessionId': 'local_' + SID2}))
        src, dst, s = self.scan()
        with self.assertRaises(app.SafetyError):
            app.transfer(s, src, dst, 'copy', self.jobs)

    def test_undo_does_not_delete_modified_target(self):
        src, dst, s = self.scan()
        receipt = app.transfer(s, src, dst, 'copy', self.jobs)
        (self.dst / self.card.name).write_text('{"edited": true}')
        undo = app.undo_job(receipt['job'])
        self.assertEqual(undo['removed'], 0)
        self.assertEqual(len(undo['errors']), 1)
        self.assertTrue((self.dst / self.card.name).exists())

    def test_registry_reference_rejected(self):
        (self.src / 'scheduled-tasks.json').write_text(json.dumps({'task': {'sessionId': RID}}))
        _, _, s = self.scan()
        self.assertFalse(s[0].valid)

    def test_source_changed_after_scan_rejected(self):
        src, dst, s = self.scan()
        self.card.write_text(self.card.read_text() + '\n ')
        with self.assertRaises(app.SafetyError):
            app.transfer(s, src, dst, 'copy', self.jobs)

    def test_english_default_and_vietnamese_translation(self):
        self.assertEqual(get_language(), 'en')
        _, _, sessions = self.scan()
        self.assertEqual(sessions[0].reason, 'Ready')
        self.assertIn('sessions', app.accounts_at(self.root)[0].label)
        set_language('vi')
        _, _, sessions = self.scan()
        self.assertEqual(sessions[0].reason, 'Sẵn sàng')
        self.assertIn('phiên', app.accounts_at(self.root)[0].label)

    def test_language_preference_settings(self):
        path = self.home / 'settings.json'
        self.assertEqual(app.read_saved_language(path), 'en')
        app.save_language('vi', path)
        self.assertEqual(app.read_saved_language(path), 'vi')
        app.save_language('en', path)
        self.assertEqual(app.read_saved_language(path), 'en')
        path.write_text('invalid JSON')
        self.assertEqual(app.read_saved_language(path), 'en')

    def test_transfer_error_english_and_vietnamese(self):
        src, _, sessions = self.scan()
        with self.assertRaisesRegex(app.SafetyError, 'Source and destination must be different'):
            app._ordered_plan(sessions, src, src)
        set_language('vi')
        with self.assertRaisesRegex(app.SafetyError, 'Tài khoản nguồn và đích'):
            app._ordered_plan(sessions, src, src)

    def test_scheduled_task_rejected(self):
        data = json.loads(self.card.read_text())
        data['scheduledTaskId'] = SID2
        self.card.write_text(json.dumps(data))
        _, _, s = self.scan()
        self.assertFalse(s[0].valid)


if __name__ == '__main__':
    unittest.main()
