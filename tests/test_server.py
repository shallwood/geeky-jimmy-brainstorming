import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import server


class BrainstormingModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "test.sqlite3"
        server.init_db()
        self.user_id = "a" * 32
        with server.connect() as db:
            db.execute("INSERT INTO users(id,username,created_at) VALUES(?,?,?)", (self.user_id,"Jimmy",server.stamp()))
            db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)", ("p"*32,"Ideas",server.stamp(),server.stamp()))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_hierarchy_stops_at_three_and_rejects_cross_project_parent(self):
        root = server.create_note_record("p"*32,None,"Top",self.user_id)
        child = server.create_note_record("p"*32,root["id"],"Child",self.user_id)
        grandchild = server.create_note_record("p"*32,child["id"],"Grandchild",self.user_id)
        with self.assertRaisesRegex(ValueError,"three levels"):
            server.create_note_record("p"*32,grandchild["id"],"Too deep",self.user_id)
        with server.connect() as db:
            db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)", ("q"*32,"Other",server.stamp(),server.stamp()))
        with self.assertRaisesRegex(ValueError,"same project"):
            server.create_note_record("q"*32,root["id"],"Wrong parent",self.user_id)

    def test_autosave_revisions_and_audit_actor(self):
        note = server.create_note_record("p"*32,None,"Scratch",self.user_id)
        first = server.save_note_record(note["id"],"Scratch","First thought.",self.user_id)
        unchanged = server.save_note_record(note["id"],"Scratch","First thought.",self.user_id)
        second = server.save_note_record(note["id"],"Renamed","Second thought.",self.user_id)
        self.assertEqual(first["revision"],2)
        self.assertEqual(unchanged["revision"],2)
        self.assertEqual(second["revision"],3)
        with self.assertRaises(server.RevisionConflict):
            server.save_note_record(note["id"],"Offline edit","Stale draft",self.user_id,expected_revision=2)
        with server.connect() as db:
            events = db.execute("SELECT action,revision,subject_title,actor_id FROM change_events WHERE note_id=? ORDER BY rowid", (note["id"],)).fetchall()
            actor = db.execute("SELECT username FROM users WHERE id=?", (events[-1]["actor_id"],)).fetchone()["username"]
        self.assertEqual([event["action"] for event in events], ["created","edited","edited"])
        self.assertEqual(events[-1]["subject_title"],"Renamed")
        self.assertEqual(actor,"Jimmy")

    def test_token_is_single_use_and_session_can_be_revoked(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            server.create_token("Test iPhone")
        one_use_token = output.getvalue().splitlines()[1]
        session = server.redeem_token(one_use_token)
        self.assertEqual(session["client_name"],"Test iPhone")
        self.assertIsNone(server.redeem_token(one_use_token))
        with server.connect() as db:
            token_row = db.execute("SELECT used_at FROM api_tokens").fetchone()
            db.execute("UPDATE web_sessions SET revoked_at=? WHERE id=?", (server.stamp(),session["session_id"]))
            session_row = db.execute("SELECT revoked_at FROM web_sessions WHERE id=?", (session["session_id"],)).fetchone()
        self.assertIsNotNone(token_row["used_at"])
        self.assertIsNotNone(session_row["revoked_at"])

    def test_browser_reconnect_replace_and_pin(self):
        def issue(**kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                server.create_token("Browser", **kwargs)
            return output.getvalue().splitlines()[1]
        first = server.redeem_token(issue())
        sid = first["session_id"]
        key = issue()
        self.assertEqual(server.redeem_token(key, first["device"])["confirm_reconnect"], sid)
        second = server.redeem_token(key, first["device"], sid)
        self.assertEqual(second["session_id"], sid)
        self.assertNotEqual(first["session"], second["session"])
        import hashlib
        with server.connect() as db:
            row = db.execute("SELECT token_hash,device_hash FROM web_sessions WHERE id=?", (sid,)).fetchone()
            self.assertEqual(row[0], hashlib.sha256(second["session"].encode()).hexdigest())
            self.assertEqual(row[1], hashlib.sha256(second["device"].encode()).hexdigest())
            server.set_project_pin(db,sid,True,"p"*32,None)
        with server.connect() as db:
            with self.assertRaises(server.RevisionConflict):
                server.set_project_pin(db,sid,True,None,None)
        with server.connect() as db:
            db.execute("UPDATE web_sessions SET revoked_at=? WHERE id=?", (server.stamp(),sid))
        self.assertIsNone(server.redeem_token(issue(),second["device"]))
        transfer = issue(replacement_id=sid)
        self.assertEqual(server.redeem_token(transfer)["confirm_reconnect"], sid)
        third = server.redeem_token(transfer, confirmed_id=sid)
        self.assertEqual(third["session_id"], sid)
        self.assertTrue(third["can_manage_permissions"])
        self.assertIsNone(server.redeem_token(transfer))
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT pinned_project FROM web_sessions").fetchall()[0][0],"p"*32)
            server.set_project_pin(db,sid,True,None,"p"*32)

    def test_title_validation(self):
        self.assertEqual(server.clean_title("  Idea  "),"Idea")
        with self.assertRaises(ValueError):
            server.clean_title(" ")
        with self.assertRaises(ValueError):
            server.clean_title("x"*161)


if __name__ == "__main__":
    unittest.main()
