import base64
import hashlib
import io
import json
import secrets
import tempfile
import unittest
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, parse_qs, urlsplit

import mcp_oauth as oauth
import server


class ChatGPTConnection(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(server, "DATA_DIR", folder))
        self.enterContext(patch.object(server, "DB_PATH", folder / "test.db"))
        self.enterContext(patch.object(server.Handler, "log_message"))
        server.init_db()
        self.key = secrets.token_urlsafe()
        with server.connect() as db:
            db.execute("INSERT INTO users VALUES('owner','Jimmy',?)", (server.stamp(),))
            db.execute("""INSERT INTO web_sessions(id,user_id,client_name,token_hash,created_at,expires_at,is_ai)
                VALUES('ai','owner','Codex',?,?,?,1)""", (oauth.digest(self.key), server.stamp(), server.token_expiry()))

    def request(self, method, path, data=None, headers=None):
        headers = dict(headers or {})
        raw = b""
        if data is not None:
            mime = headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            raw = (json.dumps(data) if mime == "application/json" else urlencode(data)).encode()
        headers["Content-Length"] = str(len(raw))
        request = (f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k,v in headers.items()) + "\r\n").encode() + raw
        output = io.BytesIO()

        class Socket:
            def makefile(self, *args):
                return io.BytesIO(request)

            def sendall(self, data):
                output.write(data)

        server.Handler(Socket(), ("localhost", 0), None)
        status, response = output.getvalue().split(b"\r\n", 1)
        head, body = response.split(b"\r\n\r\n", 1)
        metadata = BytesParser().parsebytes(head)
        return int(status.split()[1]), metadata, json.loads(body) if metadata.get_content_type() == "application/json" else body

    def test_chatgpt_link_and_live_matrix(self):
        self.check_link(oauth.CALLBACK, "ChatGPT")

    def test_claude_link_and_live_matrix(self):
        self.check_link("https://claude.ai/api/mcp/auth_callback", "Claude")

    def check_link(self, callback, client_name):
        as_json = {"Content-Type": "application/json"}
        status, hdr, _ = self.request("POST", "/mcp", {}, as_json)
        self.assertEqual(status, 401)
        schema = self.request("POST", "/mcp", dict(jsonrpc="2.0", id=1, method="tools/list"), as_json)[2]
        self.assertEqual(schema["result"]["tools"][0]["securitySchemes"][0]["scopes"], ["notes"])
        self.assertEqual(self.request("POST", "/mcp", dict(jsonrpc="2.0", id=1, method="tools/call"), as_json)[0], 401)
        discovery = "/.well-known/oauth-protected-resource/mcp"
        self.assertIn(discovery, hdr["WWW-Authenticate"])
        self.assertEqual(self.request("GET", discovery)[2]["resource"], oauth.RESOURCE)
        self.assertEqual(self.request("GET", "/.well-known/oauth-authorization-server")[2]["issuer"], oauth.ISSUER)
        self.assertEqual(self.request("POST", "/oauth/register", {"redirect_uris": ["https://example.org"]}, as_json)[0], 400)
        status, _, client = self.request("POST", "/oauth/register", {"redirect_uris": [callback], "token_endpoint_auth_method": "client_secret_post" if client_name == "Claude" else "client_secret_basic"}, as_json)
        self.assertEqual(status, 201)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = dict(client_id=client["client_id"], redirect_uri=callback, response_type="code",
                      state="test-state", code_challenge_method="S256", code_challenge=challenge, resource=oauth.RESOURCE)
        status, headers, form = self.request("GET", "/oauth/authorize?" + urlencode(params))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_all("Referrer-Policy"), ["same-origin"])
        self.assertIn(("Authorize " + client_name).encode(), form)
        params["ai_token"] = self.key
        self.assertEqual(self.request("POST", "/oauth/authorize", params)[0], 403)
        for origin in ("null", "https://example.org"):
            self.assertEqual(self.request("POST", "/oauth/authorize", params, {"Origin": origin})[0], 403)
        status, hdr, _ = self.request("POST", "/oauth/authorize", params, {"Origin": oauth.ISSUER})
        self.assertEqual(status, 303)
        result = parse_qs(urlsplit(hdr["Location"]).query)
        self.assertEqual(result["state"], ["test-state"])
        self.assertEqual(result["iss"], [oauth.ISSUER])
        auth = base64.b64encode((client["client_id"] + ":" + client["client_secret"]).encode()).decode()
        headers = {"Authorization": "Basic " + auth}
        exchange = dict(code=result["code"][0], grant_type="authorization_code", redirect_uri=callback)
        def token(**params):
            if client_name == "Claude":
                return self.request("POST", "/oauth/token", dict(params, client_id=client["client_id"], client_secret=client["client_secret"]))
            return self.request("POST", "/oauth/token", params, headers)
        self.assertEqual(token(**exchange, code_verifier="wrong")[0], 400)
        status, _, issued = token(**exchange, code_verifier=verifier)
        self.assertEqual(status, 200)
        self.assertEqual(token(**exchange, code_verifier=verifier)[0], 400)
        refresh = dict(grant_type="refresh_token", refresh_token=issued["refresh_token"])
        status, _, renewed = token(**refresh)
        self.assertEqual(status, 200)
        self.assertEqual(token(**refresh)[0], 400)
        bearer = {**as_json, "Authorization": "Bearer " + renewed["access_token"]}
        def rpc(method, params):
            return self.request("POST", "/mcp", dict(jsonrpc="2.0", id=1, method=method, params=params), bearer)
        self.assertEqual(rpc("initialize", {})[2]["result"]["serverInfo"]["title"], oauth.TITLE)
        with server.connect() as db:
            db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES('p','Private',?,?)", (server.stamp(), server.stamp()))
        note = server.create_note_record("p", None, "Matrix test", "owner")
        def read():
            return rpc("tools/call", dict(name="read_note", arguments={"note_id":note["id"]}))[2]["result"]
        self.assertTrue(read()["isError"])
        with server.connect() as db:
            db.execute("INSERT OR REPLACE INTO session_note_access VALUES('ai',?,1,?)", (note["id"],server.stamp()))
        self.assertIn("Matrix test", read()["content"][0]["text"])
        self.assertEqual(self.request("GET", "/api/access-matrix", headers=bearer)[0], 401)
        with server.connect() as db:
            db.execute("UPDATE web_sessions SET revoked_at=?", (server.stamp(),))
        self.assertEqual(rpc("initialize", {})[0], 401)
        self.assertEqual(token(grant_type="refresh_token", refresh_token=renewed["refresh_token"])[0], 400)
