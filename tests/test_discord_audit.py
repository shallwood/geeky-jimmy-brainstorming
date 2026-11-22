import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import discord_audit
import server


class DiscordAuditTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(server, "DATA_DIR", folder))
        self.enterContext(patch.object(server, "DB_PATH", folder / "test.db"))
        server.init_db()
        self.url = "https://discord.com/api/webhooks/123/" + "a" * 60

    def event(self, db, title):
        db.execute("INSERT INTO change_events(id,action,occurred_at,subject_title) VALUES(?,?,?,?)",
                   (title, "edited", server.stamp(), title))

    def test_new_events_only_retry_and_disconnect(self):
        with server.connect() as db:
            self.event(db, "Past")
            db.commit()
            with self.assertRaises(ValueError):
                discord_audit.configure(db, "https://example.org/test")
            self.assertNotIn(self.url, str(discord_audit.configure(db, self.url)))
            self.event(db, "New @everyone")
        with patch("discord_audit.http.client.HTTPSConnection") as connection:
            http = connection.return_value
            response = http.getresponse.return_value
            response.status = 429
            response.getheader.return_value = "8"
            self.assertEqual(discord_audit.deliver(server.connect), 8)
            response.status = 200
            self.assertEqual(discord_audit.deliver(server.connect), 0)
            body = json.loads(http.request.call_args.args[2])
            self.assertIn("New @everyone", body["content"])
            self.assertEqual(body["allowed_mentions"], {"parse": []})
            self.assertEqual(discord_audit.deliver(server.connect), 3)
            self.assertEqual(http.request.call_count, 2)
            with server.connect() as db:
                discord_audit.configure(db, "")
                self.event(db, "After disconnect")
            discord_audit.deliver(server.connect)
            self.assertEqual(http.request.call_count, 2)
