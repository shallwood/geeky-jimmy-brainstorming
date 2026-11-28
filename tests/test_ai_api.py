import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class MatrixApiTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(server, "DATA_DIR", folder))
        self.enterContext(patch.object(server, "DB_PATH", folder / "test.db"))
        server.init_db()
        with server.connect() as db:
            db.execute("INSERT INTO users VALUES(?,?,?)", ("owner", "Owner", server.stamp()))
            db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                       ("private", "Private", server.stamp(), server.stamp()))
        self.secret = server.create_note_record("private", None, "Restricted", "owner", body="private content")
        self.key = self.new_key("Claude")
        self.other = self.new_key("ChatGPT")

    def new_key(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            server.create_token(name, is_ai=True)
        return out.getvalue().splitlines()[1]

    def request(self, method, path, key, payload=None):
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(raw)
            def sendall(self, data):
                output.write(data)
        body = json.dumps(payload or {}).encode()
        raw = (f"{method} {path} HTTP/1.0\r\nAuthorization: Bearer {key}\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        output = io.BytesIO()
        with patch.object(server.Handler, "log_message"):
            server.Handler(Socket(), ("localhost", 0), None)
        headers, response = output.getvalue().split(b"\r\n\r\n", 1)
        return int(headers.split()[1]), json.loads(response)

    def call(self, tool, args=None, key=None):
        status, msg = self.request("POST", "/mcp", key or self.key, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name":tool, "arguments":args or {}}})
        self.assertEqual(status, 200)
        result = msg["result"]
        if result.get("isError"):
            return None
        return json.loads(result["content"][0]["text"])

    def test_http_matrix_and_creation(self):
        self.assertEqual(self.call("list_projects"), [])
        self.assertEqual(self.call("list_notes", {"query":"private"}), [])
        self.assertIsNone(self.call("create_note", {"project_id":"private", "title":"Sneak"}))
        project = self.call("create_project", {"title":"AI ideas"})["id"]
        note = self.call("create_note", {"project_id":project,"title":"Thought","body":"draft"})
        child = self.call("create_note", {"project_id":project,"parent_id":note["id"],"title":"Detail"})
        self.assertEqual(self.call("list_notes", key=self.other), [])
        self.assertIsNone(self.call("read_note", {"note_id":note["id"]}, self.other))
        args = {"note_id":note["id"], "body":"edited", "title":"Thought", "expected_revision":1}
        self.assertEqual(self.call("update_note", args)["revision"], 2)
        self.assertIsNone(self.call("update_note", args))
        with server.connect() as db:
            db.execute("UPDATE session_note_access SET allowed=0 WHERE note_id=?", (note["id"],))
        self.assertIsNone(self.call("read_note", {"note_id":note["id"]}))
        self.assertIsNone(self.call("update_note", dict(args, expected_revision=2)))
        self.assertIsNone(self.call("create_note", {"project_id":project,"parent_id":note["id"],"title":"Hidden parent"}))
        self.assertEqual([n["id"] for n in self.call("list_notes")], [child["id"]])
        for path in ["/api/notes/" + self.secret["id"], "/api/access-matrix", "/api/backups"]:
            self.assertEqual(self.request("GET",path,self.key)[0],401)
        with server.connect() as db:
            db.execute("UPDATE web_sessions SET revoked_at=? WHERE client_name='Claude'", (server.stamp(),))
        self.assertEqual(self.request("POST", "/mcp", self.key)[0],401)

    def test_ai_line_numbers_and_import_migration(self):
        project = self.call("create_project", {"title": "Docs"})["id"]
        imported_id = "bb76668eaaf5746d26ea46b2917beb19"
        with patch.object(server.secrets, "token_hex", side_effect=[imported_id, "first-event"]):
            note = self.call("create_note", {"project_id": project, "title": "AGENTS.md", "body": "original"})
        other = self.call("create_note", {"project_id": project, "title": "AGENTS.md"})
        with server.connect() as db:
            db.execute("PRAGMA user_version=0")
        server.init_db()
        args = {"note_id": note["id"]}
        self.assertEqual(self.call("read_note", args)["line_numbers"], False)
        self.assertEqual(self.call("read_note", {"note_id": other["id"]})["line_numbers"], True)
        setting = dict(args, expected_revision=2, line_numbers=True)
        self.assertIsNone(self.call("set_line_numbers", setting, self.other))
        self.assertIsNone(self.call("set_line_numbers", dict(setting, expected_revision=1)))
        self.assertIsNone(self.call("set_line_numbers", dict(setting, line_numbers=1)))
        self.assertEqual(self.call("set_line_numbers", setting)["revision"], 3)
        server.init_db()
        saved = self.call("read_note", args)
        self.assertEqual((saved["body"], saved["line_numbers"], saved["revision"]), ("original", True, 3))

    def test_attach_and_read_image(self):
        import base64
        from PIL import Image
        import drive_backup
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(drive_backup, "TARGET", directory))
        self.enterContext(patch.object(drive_backup, "mounted", return_value=True))
        png = io.BytesIO()
        Image.new("RGB", (2, 2), "green").save(png, "PNG")
        encoded = base64.b64encode(png.getvalue()).decode()
        project = self.call("create_project", {"title":"Image test"})
        note = self.call("create_note", {"project_id":project["id"], "title":"Test", "body":"abc"})
        args = dict(note_id=note["id"], expected_revision=1, offset=1, data_base64=encoded)
        self.assertIsNone(self.call("attach_image", args, self.other))
        self.assertIsNone(self.call("attach_image", dict(args, data_base64="aaaa")))
        linked = self.call("attach_image", args)
        self.assertIsNone(self.call("attach_image", args))
        image = dict(note_id=note["id"], attachment_id=linked["attachment_id"])
        self.assertIsNone(self.call("read_image", image, self.other))
        status,msg = self.request("POST", "/mcp", self.key, {"jsonrpc":"2.0","id":1,"method":"tools/call",
                                   "params":{"name":"read_image","arguments":image}})
        self.assertEqual(msg["result"]["content"], [{"type":"image","mimeType":"image/png","data":encoded}])
        saved = self.call("read_note", {"note_id":note["id"]})
        self.assertEqual(saved["body"], "a![Picture](picture:" + image["attachment_id"] + ")bc")
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT actor_type FROM change_events WHERE action='attached image'").fetchone()[0], "ai")
        with patch("pictures.chat_file", return_value=png.getvalue()):
            linked2 = self.call("attach_image", dict(note_id=note["id"],expected_revision=2,file={"file_id":"test","download_url":"test"}))
        self.assertEqual(linked2["attachment_id"], linked["attachment_id"])
        self.assertEqual(len(list((directory / "Note pictures").iterdir())), 1)

    def test_default_access_only_applies_at_creation(self):
        import hashlib
        with server.connect() as db:
            db.execute("UPDATE web_sessions SET default_note_access=1")
            other = db.execute("SELECT id FROM web_sessions WHERE token_hash=?", (hashlib.sha256(self.other.encode()).hexdigest(),)).fetchone()[0]
        self.assertIsNone(self.call("read_note", {"note_id":self.secret["id"]}, self.other))
        project = self.call("create_project", {"title":"Shared"})
        note = self.call("create_note", {"project_id":project["id"],"title":"Shared note"})
        self.assertEqual(self.call("read_note", {"note_id":note["id"]}, self.other)["title"], "Shared note")
        with server.connect() as db:
            db.execute("UPDATE web_sessions SET default_note_access=0 WHERE id=?", (other,))
        child = self.call("create_note", {"project_id":project["id"], "parent_id":note["id"],"title":"Private child"})
        self.assertIsNone(self.call("read_note", {"note_id":child["id"]},self.other))
        self.assertEqual(self.call("read_note", {"note_id":note["id"]},self.other)["title"], "Shared note")
        self.assertEqual(self.request("POST", "/api/default-note-access", self.other, {"session_id":other,"allowed":True})[0],401)

    def test_collapsed_project_survives_reload_per_session(self):
        def browser(name):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                server.create_token(name, can_manage_permissions=True)
            return server.redeem_token(out.getvalue().splitlines()[1])["session"]
        first, second = browser("Desktop"), browser("Phone")
        def collapsed(key):
            return self.request("GET", "/api/projects", key)[1][0]["collapsed"]
        def set_state(value):
            return self.request("POST", "/api/project-collapse", first, {"project_id":"private","collapsed":value})[0]
        self.assertFalse(collapsed(first))
        self.assertEqual(set_state(True), 200)
        server.init_db()
        self.assertTrue(collapsed(first))
        self.assertFalse(collapsed(second))
        self.assertEqual(set_state(False), 200)
        self.assertFalse(collapsed(first))
