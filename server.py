"""Small single-user API and static file server for Geeky Jimmy's Brainstorming Tool."""

from __future__ import annotations

import base64
from xml.sax.saxutils import escape

import mcp_oauth
import pictures
import ai_api
import drive_backup
import discord_audit
import threading
import hashlib
import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "brainstorming.sqlite3"
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
MAX_NOTE_DEPTH = 3


def azure_speech_endpoint():
    endpoint = os.getenv("AZURE_SPEECH_ENDPOINT", "").strip().rstrip("/")
    if endpoint:
        if re.fullmatch(r"https://(?:[a-z0-9-]+\.cognitiveservices\.azure\.com/tts|[a-z0-9]+\.tts\.speech\.microsoft\.com)/cognitiveservices/v1", endpoint):
            return endpoint
        return None
    region = os.getenv("AZURE_SPEECH_REGION", "").strip()
    if re.fullmatch(r"[a-z0-9]+", region):
        return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    return None


def speech_providers():
    providers = []
    if os.environ.get("AZURE_SPEECH_KEY") and azure_speech_endpoint():
        providers.append("azure")
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        providers.append("google")
    return providers


class WorkspaceConnection(sqlite3.Connection):
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=WorkspaceConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        access_matrix_existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_note_access'").fetchone() is not None
        db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_tokens (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                can_manage_permissions INTEGER NOT NULL DEFAULT 0,
                used_at TEXT, revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS web_sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE, client_name TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                last_used_at TEXT, revoked_at TEXT,
                can_manage_permissions INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collapsed_projects (
                session_id TEXT NOT NULL REFERENCES web_sessions(id) ON DELETE CASCADE,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                PRIMARY KEY(session_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                parent_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                line_numbers INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS session_note_access (
                session_id TEXT NOT NULL REFERENCES web_sessions(id) ON DELETE CASCADE,
                note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                allowed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(session_id,note_id)
            );
            CREATE INDEX IF NOT EXISTS notes_project_idx ON notes(project_id);
            CREATE INDEX IF NOT EXISTS notes_parent_idx ON notes(parent_id);
            CREATE INDEX IF NOT EXISTS notes_updated_idx ON notes(updated_at);
            -- Reserved stable relationship tables: populated by a later increment.
            CREATE TABLE IF NOT EXISTS note_links (
                source_note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                target_note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                anchor TEXT, PRIMARY KEY(source_note_id, target_note_id, anchor)
            );
            CREATE TABLE IF NOT EXISTS tags (
                id TEXT PRIMARY KEY, normalized_label TEXT UNIQUE NOT NULL, label TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS note_tags (
                note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(note_id, tag_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, storage_key TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                sha256 TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS note_attachments (
                note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
                position INTEGER, PRIMARY KEY(note_id, attachment_id)
            );
            CREATE TABLE IF NOT EXISTS change_events (
                id TEXT PRIMARY KEY, actor_type TEXT NOT NULL DEFAULT 'user',
                actor_id TEXT, note_id TEXT, action TEXT NOT NULL,
                occurred_at TEXT NOT NULL, revision INTEGER, subject_title TEXT
            );
        """)
        db.executescript(mcp_oauth.SCHEMA)
        db.executescript(discord_audit.SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(change_events)")}
        if "subject_title" not in columns:
            db.execute("ALTER TABLE change_events ADD COLUMN subject_title TEXT")
        if "archived" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
            db.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "can_backup" not in {r["name"] for r in db.execute("PRAGMA table_info(web_sessions)")}:
            db.execute("ALTER TABLE web_sessions ADD COLUMN can_backup INTEGER NOT NULL DEFAULT 0")
        if "ai_creator" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
            db.execute("ALTER TABLE projects ADD COLUMN ai_creator TEXT")
        token_columns = {row["name"] for row in db.execute("PRAGMA table_info(api_tokens)")}
        if "can_manage_permissions" not in token_columns:
            db.execute("ALTER TABLE api_tokens ADD COLUMN can_manage_permissions INTEGER NOT NULL DEFAULT 0")
        session_columns = {row["name"] for row in db.execute("PRAGMA table_info(web_sessions)")}
        if "replacement_id" not in token_columns:
            db.execute("ALTER TABLE api_tokens ADD COLUMN replacement_id TEXT REFERENCES web_sessions(id)")
        if "default_note_access" not in session_columns:
            db.execute("ALTER TABLE web_sessions ADD COLUMN default_note_access INTEGER NOT NULL DEFAULT 0")
        if "pinned_project" not in session_columns:
            db.execute("ALTER TABLE web_sessions ADD COLUMN pinned_project TEXT REFERENCES projects(id) ON DELETE SET NULL")
        if "device_hash" not in session_columns:
            db.execute("ALTER TABLE web_sessions ADD COLUMN device_hash TEXT")
        if "is_ai" not in session_columns:
            db.execute("ALTER TABLE web_sessions ADD COLUMN is_ai INTEGER NOT NULL DEFAULT 0")
        if "can_manage_permissions" not in session_columns:
            db.execute("ALTER TABLE web_sessions ADD COLUMN can_manage_permissions INTEGER NOT NULL DEFAULT 0")
            initial_manager = db.execute(
                "SELECT id FROM web_sessions WHERE revoked_at IS NULL AND expires_at>? ORDER BY created_at,id LIMIT 1",
                (stamp(),)).fetchone()
            if initial_manager:
                db.execute("UPDATE web_sessions SET can_manage_permissions=1 WHERE id=?", (initial_manager["id"],))
        note_columns = {row["name"] for row in db.execute("PRAGMA table_info(notes)")}
        if "line_numbers" not in note_columns:
            db.execute("ALTER TABLE notes ADD COLUMN line_numbers INTEGER NOT NULL DEFAULT 1")
        if not access_matrix_existed:
            db.execute("""
                INSERT OR IGNORE INTO session_note_access(session_id,note_id,allowed,updated_at)
                SELECT s.id,n.id,1,? FROM web_sessions s CROSS JOIN notes n
                WHERE s.revoked_at IS NULL AND s.expires_at>?
            """, (stamp(),stamp()))


    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            sql = (ROOT / "migrations/001_imported_line_numbers.sql").read_text()
            now = stamp()
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement, {"now": now})
                    statement = ""
            db.execute("PRAGMA user_version=1")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(timespec="seconds")


def error(message: str, status: int = 400) -> tuple[int, dict]:
    return status, {"error": message}


def note_depth(db: sqlite3.Connection, note_id: str, seen: set[str] | None = None) -> int:
    seen = seen or set()
    if note_id in seen:
        raise ValueError("Circular note hierarchy detected")
    seen.add(note_id)
    row = db.execute("SELECT parent_id FROM notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise ValueError("Parent note does not exist")
    return 1 if row["parent_id"] is None else note_depth(db, row["parent_id"], seen) + 1


def row_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class RevisionConflict(Exception):
    pass


def extract_pdf_text(document: bytes) -> str:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "import.pdf"
        output = Path(directory) / "note.txt"
        source.write_bytes(document)
        try:
            result = subprocess.run(
                ["prlimit", "--as=536870912", "--fsize=4000000", "--cpu=20", "--",
                 "pdftotext", "-enc", "UTF-8", "-nopgbrk", str(source), str(output)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("This PDF took too long to extract. Try a shorter document.") from None
        except FileNotFoundError:
            raise ValueError("PDF import requires poppler-utils and util-linux on the server.") from None
        if result.returncode:
            raise ValueError("Could not extract this PDF. It may be locked, damaged, or too large.")
        body = output.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError("This PDF has no selectable text. Run OCR on the scan first.")
        return body


def create_note_record(project_id: str, parent_id: str | None, title: str, actor_id: str | None,
                       session_id: str | None = None, body: str = "", *, ai=False) -> dict:
    if len(body) > 1_000_000:
        raise ValueError("Imported text exceeds the one-million-character note limit")
    title = clean_title(title)
    note_id, now = secrets.token_hex(16), stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if ai:
            if not db.execute("SELECT 1 " + ai_api.PROJECTS + " AND p.id=?", (session_id,now,project_id)).fetchone():
                raise LookupError("Project not found")
            if parent_id and not db.execute("SELECT 1 " + ai_api.NOTES + " AND n.id=? AND n.project_id=?",
                                            (session_id,now,parent_id,project_id)).fetchone():
                raise LookupError("Parent note not found")
        if db.execute("SELECT 1 FROM projects WHERE archived=0 AND id=?", (project_id,)).fetchone() is None:
            raise LookupError("Project not found")
        if parent_id:
            parent = db.execute("SELECT project_id FROM notes WHERE status='active' AND id=?", (parent_id,)).fetchone()
            if parent is None or parent["project_id"] != project_id:
                raise ValueError("Parent must belong to the same project")
            if note_depth(db, parent_id) >= MAX_NOTE_DEPTH:
                raise ValueError("Notes can have at most three levels")
        db.execute("INSERT INTO notes(id,project_id,parent_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (note_id,project_id,parent_id,title,body,now,now))
        db.execute("""INSERT INTO session_note_access(session_id,note_id,allowed,updated_at)
            SELECT id,?,1,? FROM web_sessions WHERE id=? OR
            (default_note_access=1 AND revoked_at IS NULL AND expires_at>?)""", (note_id,now,session_id,now))
        db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title) VALUES(?,?,?,?,?,?,?,?)",
                   (secrets.token_hex(16),"ai" if ai else "user",session_id if ai else actor_id,note_id,"created",now,1,title))
        row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return row_dict(row)


def save_note_record(note_id: str, title: str, body: str, actor_id: str | None, expected_revision: int | None = None,
                     line_numbers: bool | None = None, *, ai_session=None) -> dict:
    title = clean_title(title)
    if not isinstance(body, str) or len(body) > 1_000_000:
        raise ValueError("Note content must be text under 1 MB")
    if line_numbers is not None and not isinstance(line_numbers, bool):
        raise ValueError("line_numbers must be a boolean")
    now = stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if ai_session and not db.execute("SELECT 1 " + ai_api.NOTES + " AND n.id=?", (ai_session, now, note_id)).fetchone():
            raise LookupError("Note not found")
        before = db.execute("SELECT revision,title,body,line_numbers FROM notes WHERE status='active' AND id=?", (note_id,)).fetchone()
        if before is None:
            raise LookupError("Note not found")
        if expected_revision is not None and before["revision"] != expected_revision:
            raise RevisionConflict("The server copy changed. Your local draft is preserved; reload before continuing to sync.")
        next_line_numbers = before["line_numbers"] if line_numbers is None else int(line_numbers)
        if before["title"] != title or before["body"] != body or before["line_numbers"] != next_line_numbers:
            revision = before["revision"] + 1
            db.execute("UPDATE notes SET title=?,body=?,line_numbers=?,updated_at=?,revision=? WHERE id=?",
                       (title,body,next_line_numbers,now,revision,note_id))
            db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title) VALUES(?,?,?,?,?,?,?,?)",
                       (secrets.token_hex(16),"ai" if ai_session else "user",ai_session or actor_id,note_id,"edited",now,revision,title))
        row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return row_dict(row)


def organization_access(db, session, kind, item, action):
    if kind == "project":
        scope = "SELECT id FROM notes WHERE project_id=?"
    elif action == "rename":
        scope = "SELECT id FROM notes WHERE id=?"
    else:
        scope = """WITH RECURSIVE branch(id) AS (
            SELECT id FROM notes WHERE id=? UNION SELECT n.id FROM notes n JOIN branch ON n.parent_id=branch.id
        ) SELECT id FROM branch"""
    row = db.execute(f"""SELECT COUNT(*) total, COUNT(a.note_id) permitted FROM ({scope}) n
        LEFT JOIN session_note_access a ON a.note_id=n.id AND a.session_id=? AND a.allowed=1""",
        (item,session)).fetchone()
    return row["total"] > 0 and row["total"] == row["permitted"]


def organize_item(db, data, actor, session=None):
    kind, item, action = data.get("kind"), data.get("id"), data.get("action")
    if kind not in ("project", "note") or not isinstance(item,str):
        raise ValueError("Choose a project or note")
    table = "projects" if kind == "project" else "notes"
    db.execute("BEGIN IMMEDIATE")
    if session is not None and not organization_access(db,session,kind,item,action):
        raise ValueError("This action requires access to every affected note.")
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item,)).fetchone()
    if not row:
        raise ValueError("Item no longer exists")
    marker = "archived:" + item
    archived = row["archived"] if kind == "project" else row["status"] != "active"
    if archived != (action in ("restore", "delete")):
        raise ValueError("Item has changed. Refresh the list.")
    if archived and kind == "note" and row["status"] != marker:
        raise ValueError("Restore the containing archive first")
    now = stamp()
    tree = """WITH RECURSIVE branch(id,depth) AS (
        SELECT id,1 FROM notes WHERE id=? UNION ALL
        SELECT n.id,b.depth+1 FROM notes n JOIN branch b ON n.parent_id=b.id
    ) """
    if action == "rename":
        revision = ",revision=revision+1" if kind == "note" else ""
        db.execute(f"UPDATE {table} SET title=?,updated_at=?{revision} WHERE id=?",
                   (clean_title(data.get("title")),now,item))
    elif action == "archive":
        if kind == "project":
            db.execute("UPDATE projects SET archived=1 WHERE id=?", (item,))
            db.execute("UPDATE notes SET status=? WHERE project_id=? AND status='active'", (marker,item))
        else:
            db.execute(tree+"UPDATE notes SET status=? WHERE id IN (SELECT id FROM branch) AND status='active'", (item,marker))
    elif action == "restore":
        if kind == "project":
            db.execute("UPDATE projects SET archived=0 WHERE id=?", (item,))
        else:
            if not db.execute("SELECT 1 FROM projects WHERE id=? AND archived=0",(row["project_id"],)).fetchone():
                raise ValueError("Restore the project first")
            if row["parent_id"] and not db.execute("SELECT 1 FROM notes WHERE id=? AND status='active'",(row["parent_id"],)).fetchone():
                raise ValueError("Restore the parent note first")
        db.execute("UPDATE notes SET status='active' WHERE status=?", (marker,))
    elif action == "delete":
        db.execute(f"DELETE FROM {table} WHERE id=?", (item,))
    elif action == "move" and kind == "note":
        project, parent = data.get("project_id"), data.get("parent_id") or None
        if not db.execute("SELECT 1 FROM projects WHERE id=? AND archived=0",(project,)).fetchone():
            raise ValueError("Choose an active project")
        if session is not None:
            destination = db.execute("""SELECT 1 FROM notes n JOIN session_note_access a ON a.note_id=n.id
                WHERE a.session_id=? AND a.allowed=1 AND n.project_id=? AND n.status='active'
                AND (? IS NULL OR n.id=?) LIMIT 1""", (session,project,parent,parent)).fetchone()
            if not destination:
                raise ValueError("Choose an accessible destination")
        branch = db.execute(tree+"SELECT id,depth FROM branch",(item,)).fetchall()
        if parent:
            if parent in {r["id"] for r in branch}:
                raise ValueError("A note cannot move under itself or its descendants")
            if not db.execute("SELECT 1 FROM notes WHERE id=? AND project_id=? AND status='active'",(parent,project)).fetchone():
                raise ValueError("Choose a parent in the destination project")
            if note_depth(db,parent)+max(r["depth"] for r in branch)>MAX_NOTE_DEPTH:
                raise ValueError("This move would exceed three note levels")
        db.execute(tree+"UPDATE notes SET project_id=? WHERE id IN (SELECT id FROM branch)",(item,project))
        db.execute("UPDATE notes SET parent_id=?,updated_at=? WHERE id=?",(parent,now,item))
    else:
        raise ValueError("Unknown action")
    db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,subject_title) VALUES(?,?,?,?,?,?,?)",
               (secrets.token_hex(16),"user",actor,item if kind=="note" else None,kind+" "+action,now,row["title"]))


VISIBLE_PROJECT = """SELECT p.id FROM projects p WHERE p.archived=0 AND p.id=? AND
        (? OR EXISTS (SELECT 1 FROM notes n JOIN session_note_access a ON a.note_id=n.id
            WHERE n.project_id=p.id AND n.status='active' AND a.session_id=? AND a.allowed=1))"""


def set_project_pin(db, session, manager, project_id, previous):
    db.execute("BEGIN IMMEDIATE")
    current = db.execute("SELECT pinned_project FROM web_sessions WHERE id=?", (session,)).fetchone()[0]
    if current and not db.execute(VISIBLE_PROJECT, (current,manager,session)).fetchone():
        current = None
    if current != previous:
        raise RevisionConflict("Your pinned project changed. Reopen the project menu.")
    if project_id is not None and not db.execute(VISIBLE_PROJECT, (project_id,manager,session)).fetchone():
        raise LookupError("Project not found")
    db.execute("UPDATE web_sessions SET pinned_project=? WHERE id=?", (project_id,session))


def redeem_token(token: str, device="", confirmed_id=None) -> dict | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = stamp()
        row = db.execute("SELECT * FROM api_tokens WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?", (digest,now)).fetchone()
        if not row:
            return None
        target = None
        if row["replacement_id"]:
            target = db.execute("SELECT * FROM web_sessions WHERE id=? AND user_id=? AND is_ai=0",
                                (row["replacement_id"], row["user_id"])).fetchone()
            if not target:
                return None
        elif isinstance(device, str) and device:
            target = db.execute("SELECT * FROM web_sessions WHERE device_hash=? AND user_id=? AND is_ai=0",
                                (hashlib.sha256(device.encode()).hexdigest(), row["user_id"])).fetchone()
            if target and target["revoked_at"]:
                return None
        if confirmed_id is not None and (not target or target["id"] != confirmed_id):
            return None
        if target and confirmed_id is None:
            return {"confirm_reconnect": target["id"], "client_name": target["client_name"]}
        session_value = "sess_" + secrets.token_urlsafe(40)
        device_value = secrets.token_urlsafe(40)
        login_hash = hashlib.sha256(session_value.encode()).hexdigest()
        device_hash = hashlib.sha256(device_value.encode()).hexdigest()
        expires = token_expiry()
        db.execute("UPDATE api_tokens SET used_at=? WHERE id=?", (now,row["id"]))
        if target:
            session_id, name, manager = target["id"], target["client_name"], target["can_manage_permissions"]
            db.execute("UPDATE web_sessions SET token_hash=?,device_hash=?,last_used_at=?,expires_at=?,revoked_at=NULL WHERE id=?",
                       (login_hash,device_hash,now,expires,session_id))
            db.execute("UPDATE api_tokens SET revoked_at=? WHERE replacement_id=? AND used_at IS NULL", (now,session_id))
        else:
            session_id, name, manager = secrets.token_hex(16), row["name"], row["can_manage_permissions"]
            db.execute("INSERT INTO web_sessions(id,user_id,token_hash,device_hash,client_name,created_at,expires_at,last_used_at,can_manage_permissions) VALUES(?,?,?,?,?,?,?,?,?)",
                       (session_id,row["user_id"],login_hash,device_hash,name,now,expires,now,manager))
            if manager:
                db.execute("INSERT INTO session_note_access(session_id,note_id,allowed,updated_at) SELECT ?,id,1,? FROM notes",
                           (session_id,now))
        return {"session":session_value,"device":device_value,"session_id":session_id,"client_name":name,
                "can_manage_permissions":bool(manager)}


class Handler(BaseHTTPRequestHandler):
    server_version = "GeekyJimmyBrainstorming/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep request logs useful without logging request bodies or credentials.
        super().log_message(fmt, *args)

    def add_security_headers(self, *, referrer_policy="no-referrer") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", referrer_policy)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://esm.sh; style-src 'self' 'unsafe-inline'; connect-src 'self' https://esm.sh; img-src 'self' data: blob:; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def send_json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if status == 401 and urlparse(self.path).path == "/mcp":
            self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{mcp_oauth.ISSUER}/.well-known/oauth-protected-resource/mcp", scope="notes"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def body_json(self, limit=2_000_000) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= limit:
            raise ValueError("Request body is too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def is_authorized(self, *, ai=False) -> bool:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with connect() as db:
            session = db.execute("SELECT id,user_id,can_manage_permissions,can_backup FROM web_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>? AND is_ai=?", (digest, stamp(), int(ai))).fetchone()
            if ai and not session:
                session = db.execute("""SELECT s.id,s.user_id,s.can_manage_permissions,s.can_backup
                    FROM oauth_grants g JOIN web_sessions s ON s.id=g.session
                    WHERE g.digest=? AND g.kind='access' AND g.expires>CAST(strftime('%s','now') AS INTEGER)
                    AND s.is_ai=1 AND s.revoked_at IS NULL AND s.expires_at>?""", (digest, stamp())).fetchone()
            if session:
                db.execute("UPDATE web_sessions SET last_used_at=? WHERE id=?", (stamp(), session["id"]))
                self.can_backup = bool(session["can_backup"])
                self.session_id = session["id"]
                self.session_user_id = session["user_id"]
                self.can_manage_permissions = bool(session["can_manage_permissions"])
                return True
        self.session_id = None
        self.session_user_id = None
        self.can_manage_permissions = False
        return False

    def has_note_access(self, note_id: str) -> bool:
        if not self.session_id:
            return False
        with connect() as db:
            row = db.execute("SELECT a.allowed FROM session_note_access a JOIN notes n ON n.id=a.note_id WHERE n.status='active' AND a.session_id=? AND a.note_id=?",
                             (self.session_id,note_id)).fetchone()
        return bool(row and row["allowed"])

    def require_permission_manager(self) -> bool:
        if self.can_manage_permissions:
            return True
        self.send_json(403, {"error":"This client cannot manage note permissions"})
        return False

    def note_picture(self, note_id, picture_id=None):
        if not self.has_note_access(note_id):
            self.send_json(404, {"error": "Note not found"})
            return
        try:
            if picture_id:
                with connect() as db:
                    picture, mime = pictures.read(db, note_id, picture_id)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(picture)))
                self.send_header("Cache-Control", "private, no-store")
                self.add_security_headers()
                self.end_headers()
                self.wfile.write(picture)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8 * 1024 * 1024:
                self.send_json(413, {"error": "Choose a picture smaller than 8 MiB after resizing"})
                return
            picture = self.rfile.read(size)
            if len(picture) != size:
                raise ValueError("Incomplete picture upload")
            ext = pictures.format_of(picture)
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                picture_id = pictures.store(db, note_id, picture, ext, stamp())
            self.send_json(201, {"id": picture_id})
        except (FileNotFoundError, LookupError):
            self.send_json(404, {"error": "Picture unavailable"})
        except OSError:
            self.send_json(503, {"error": "Could not access the Google Drive picture folder"})

    def handle_mcp(self):
        origin = self.headers.get("Origin")
        if origin and origin != "https://" + self.headers.get("Host", ""):
            self.send_json(403, {"error": "Origin denied"})
            return
        authenticated = bool(self.headers.get("Authorization")) and self.is_authorized(ai=True)
        if self.headers.get("Authorization") and not authenticated:
            self.send_json(401, {"error": "An active AI bearer token is required"})
            return
        try:
            message = self.body_json(12_000_000 if authenticated else 2_000_000)
        except (ValueError, UnicodeError):
            self.send_json(400, {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Invalid JSON"}})
            return

        # ChatGPT scans public tool schemas before an account is linked.
        discovery = message.get("method") in ("initialize", "tools/list", "notifications/initialized", "ping")
        if not discovery and not authenticated:
            self.send_json(401, {"error": "An active AI bearer token is required"})
            return

        def invoke(name, args):
            try:
                return ai_api.call(name, args, self.session_id, connect, stamp, clean_title,
                                   create_note_record, save_note_record)
            except RevisionConflict:
                raise ValueError("Revision conflict. Read the note again before editing.") from None

        reply = ai_api.dispatch(message, invoke)
        if reply is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json(200, reply)

    def synthesize_speech(self, data):
        preview = data.get("preview") is True
        if preview:
            passage = "Here’s a thought. What would you like to work on today?"
        else:
            if not self.has_note_access(str(data.get("note_id", ""))):
                self.send_json(404, {"error": "Note not found"})
                return
            passage = data.get("text")
            if not isinstance(passage, str) or not passage.strip() or len(passage.encode("utf-8")) > 1600:
                raise ValueError("Choose a short passage to read")
        provider = data.get("provider", "google")
        if provider not in speech_providers():
            self.send_json(503, {"error": "This speech provider is disabled on the server."})
            return
        if provider == "azure":
            import requests
            voice = data.get("voice", "en-US-JennyNeural")
            if voice not in ("en-US-Phoebe:DragonHDLatestNeural", "en-US-JennyNeural", "en-US-GuyNeural"):
                raise ValueError("Choose an available Azure voice")
            url = azure_speech_endpoint()
            ssml = ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
                    f'<voice name="{voice}">{escape(passage)}</voice></speak>')
            try:
                result = requests.post(url, data=ssml.encode(), headers={
                    "Ocp-Apim-Subscription-Key": os.environ["AZURE_SPEECH_KEY"].strip(),
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                }, timeout=20, allow_redirects=False)
            except requests.RequestException:
                self.send_json(503, {"error": "Could not reach Azure Speech."})
                return
            if result.status_code == 200 and result.content:
                self.send_json(200, {"audioContent": base64.b64encode(result.content).decode("ascii")})
            else:
                self.send_json(502, {"error": f"Azure Speech returned HTTP {result.status_code}. Check the Speech key, region and quota."})
            return
        import google.auth
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from requests.exceptions import RequestException
        try:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            with AuthorizedSession(credentials) as client:
                result = client.post("https://texttospeech.googleapis.com/v1/text:synthesize",
                                     json={"input": {"text": passage},
                                           "voice": {"languageCode": "en-US", "name": "en-US-Chirp-HD-F"},
                                           "audioConfig": {"audioEncoding": "MP3"}}, timeout=20)
                if result.ok:
                    self.send_json(200, {"audioContent": result.json()["audioContent"]})
                else:
                    self.send_json(502, {"error": f"Google Speech returned HTTP {result.status_code}. Check API access, billing and quota."})
        except (GoogleAuthError, RequestException, OSError):
            self.send_json(503, {"error": "Google Speech is unavailable. Check the server credentials and connection."})

    def actor_id(self) -> str | None:
        digest = hashlib.sha256(self.headers.get("Authorization", "").removeprefix("Bearer ").encode()).hexdigest()
        with connect() as db:
            row = db.execute("SELECT user_id FROM web_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest,stamp())).fetchone()
        return row["user_id"] if row else None

    def do_GET(self) -> None:
        if mcp_oauth.handle(self, connect, stamp):
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()
            return
        if path == "/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/setup-status":
            with connect() as db:
                count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            self.send_json(200, {"needs_setup": count == 0})
            return
        if path == "/api/session":
            authenticated = self.is_authorized()
            self.send_json(200, {"authenticated":authenticated, "speech_providers": speech_providers() if authenticated else [],
                                 "can_manage_permissions":bool(getattr(self,"can_manage_permissions",False))})
            return
        if path == "/api/sessions":
            if not self.is_authorized():
                self.send_json(401, {"error":"Sign in to continue"}); return
            if not self.require_permission_manager(): return
            with connect() as db:
                rows = db.execute("SELECT id,client_name,is_ai,created_at,expires_at,last_used_at,revoked_at,can_manage_permissions,can_backup FROM web_sessions ORDER BY created_at DESC").fetchall()
            self.send_json(200, [row_dict(r) for r in rows]); return
        if path == "/api/access-matrix":
            if not self.is_authorized():
                self.send_json(401, {"error":"Sign in to continue"}); return
            if not self.require_permission_manager(): return
            now = stamp()
            with connect() as db:
                sessions = [row_dict(row) for row in db.execute(
                    "SELECT id,client_name,is_ai,default_note_access FROM web_sessions WHERE revoked_at IS NULL AND expires_at>? ORDER BY created_at",
                    (now,)).fetchall()]
                access = {(row["session_id"],row["note_id"]):bool(row["allowed"]) for row in db.execute(
                    "SELECT session_id,note_id,allowed FROM session_note_access").fetchall()}
                projects = [row_dict(row) for row in db.execute("SELECT id,title FROM projects WHERE archived=0 ORDER BY created_at,title").fetchall()]
                for project in projects:
                    notes = db.execute("SELECT id,parent_id,title FROM notes WHERE project_id=? AND status='active' ORDER BY created_at,title",
                                       (project["id"],)).fetchall()
                    project["notes"] = []
                    for note in notes:
                        project["notes"].append({**row_dict(note),"access":{
                            session["id"]:access.get((session["id"],note["id"]),False) for session in sessions
                        }})
            self.send_json(200,{"sessions":sessions,"projects":projects}); return
        if path.startswith("/api/") and not self.is_authorized():
            self.send_json(401, {"error": "Sign in to continue"})
            return
        picture = re.fullmatch(r"/api/notes/([a-f0-9]{32})/pictures/([a-f0-9]{32})", path)
        if picture:
            self.note_picture(*picture.groups())
            return
        if path == "/api/audit-webhook":
            if not self.require_permission_manager(): return
            with connect() as db:
                self.send_json(200, discord_audit.status(db))
            return
        if path == "/api/backups":
            self.send_json(200, {"allowed":self.can_backup, **(drive_backup.status() if self.can_backup else {})})
            return
        if path == "/api/archive":
            with connect() as db:
                rows = db.execute("""
                    SELECT id,title,'project' kind,1 restorable FROM projects WHERE archived=1
                    UNION ALL
                    SELECT n.id,n.title,'note',p.archived=0 AND (n.parent_id IS NULL OR parent.status='active')
                    FROM notes n JOIN projects p ON p.id=n.project_id LEFT JOIN notes parent ON parent.id=n.parent_id
                    WHERE n.status='archived:' || n.id ORDER BY title
                """).fetchall()
                if not self.can_manage_permissions:
                    rows = [r for r in rows if organization_access(db,self.session_id,r["kind"],r["id"],"restore")]
            self.send_json(200,[dict(row) for row in rows]); return
        if path == "/api/projects":
            with connect() as db:
                projects = [row_dict(r) for r in db.execute("SELECT id,title,created_at,updated_at,archived FROM projects WHERE archived=0 ORDER BY created_at, title")]
                pin = db.execute("SELECT pinned_project FROM web_sessions WHERE id=?", (self.session_id,)).fetchone()[0]
                collapsed = {r[0] for r in db.execute("SELECT project_id FROM collapsed_projects WHERE session_id=?", (self.session_id,))}
                for project in projects:
                    project["collapsed"] = project["id"] in collapsed
                    project["pinned"] = project["id"] == pin
                    project["notes"] = [row_dict(r) for r in db.execute(
                        "SELECT n.* FROM notes n JOIN session_note_access a ON a.note_id=n.id "
                        "WHERE n.status='active' AND n.project_id=? AND a.session_id=? AND a.allowed=1 ORDER BY n.created_at,n.title",
                        (project["id"],self.session_id)
                    )]
                projects = [project for project in projects if self.can_manage_permissions or project["notes"]]
            self.send_json(200, projects)
            return
        if path == "/api/search":
            query = (parse_qs(parsed.query).get("q") or [""])[0].strip()
            with connect() as db:
                rows = db.execute("""
                    SELECT n.id, n.project_id, n.parent_id, n.title, n.body, n.updated_at,
                           p.title AS project_title
                    FROM notes n JOIN projects p ON p.id=n.project_id
                    JOIN session_note_access a ON a.note_id=n.id AND a.session_id=? AND a.allowed=1
                    WHERE n.status='active' AND (n.title LIKE ? OR n.body LIKE ?)
                    ORDER BY n.updated_at DESC LIMIT 100
                """, (self.session_id,f"%{query}%", f"%{query}%")).fetchall() if query else []
            self.send_json(200, [row_dict(r) for r in rows])
            return
        if path == "/api/audit":
            with connect() as db:
                rows = db.execute("""
                    SELECT e.id,e.action,e.subject_title,e.note_id,e.revision,e.occurred_at,
                           COALESCE(ai.client_name,u.username,'Unknown user') AS actor
                    FROM change_events e LEFT JOIN users u ON u.id=e.actor_id
                    LEFT JOIN web_sessions ai ON e.actor_type='ai' AND ai.id=e.actor_id
                    WHERE e.note_id IS NULL OR EXISTS (
                        SELECT 1 FROM session_note_access a WHERE a.note_id=e.note_id AND a.session_id=? AND a.allowed=1
                    )
                    ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT 200
                """, (self.session_id,)).fetchall()
            self.send_json(200, [row_dict(r) for r in rows]); return
        if path.startswith("/api/notes/"):
            note_id = path.rsplit("/", 1)[-1]
            with connect() as db:
                row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None or not self.has_note_access(note_id):
                self.send_json(404, {"error": "Note not found"})
            else:
                self.send_json(200, row_dict(row))
            return
        if path.startswith("/api/"):
            self.send_json(404, {"error": "Not found"})
            return
        self.serve_static(path)

    def do_POST(self) -> None:
        if mcp_oauth.handle(self, connect, stamp):
            return
        path = urlparse(self.path).path
        if path == "/mcp":
            self.handle_mcp()
            return
        if path == "/api/logout":
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            with connect() as db:
                db.execute("UPDATE web_sessions SET expires_at=? WHERE token_hash=? AND is_ai=0 AND revoked_at IS NULL", (stamp(),hashlib.sha256(supplied.encode()).hexdigest()))
            self.send_json(200, {"signed_out":True}); return
        if path in ("/api/setup", "/api/login"):
            try:
                data = self.body_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "Invalid request"})
                return
            if path == "/api/setup":
                username = str(data.get("username", "")).strip()
                if len(username) < 2 or len(username) > 64:
                    self.send_json(400, {"error": "Use a username of 2–64 characters"}); return
                now, user_id = stamp(), secrets.token_hex(16)
                try:
                    with connect() as db:
                        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                            self.send_json(409, {"error": "Initial setup is already complete"}); return
                        db.execute("INSERT INTO users(id,username,created_at) VALUES(?,?,?)", (user_id,username,now))
                except sqlite3.IntegrityError:
                    self.send_json(409, {"error": "That username is already in use"}); return
                self.send_json(201, {"created": True, "message": "Account created. Generate a connection token in the CLI."}); return
            token_input = str(data.get("token", "")).strip()
            if token_input:
                session = redeem_token(token_input, data.get("device"), data.get("confirmed_id"))
                if session:
                    self.send_json(200, {**session, "speech_providers": speech_providers()}); return
            self.send_json(401, {"error": "This one-use token is invalid, expired, or has already been used"})
            return
        if not self.is_authorized():
            self.send_json(401, {"error": "Sign in to continue"})
            return
        try:
            upload = re.fullmatch(r"/api/notes/([a-f0-9]{32})/pictures", path)
            if upload:
                self.note_picture(upload[1])
                return
            importing = path == "/api/notes/import"
            if importing:
                params = parse_qs(urlparse(self.path).query)
                data = {key: values[0] for key, values in params.items() if key in ("project_id", "title")}
            else:
                data = self.body_json()
            if path == "/api/default-note-access":
                if not self.require_permission_manager(): return
                allowed = data.get("allowed")
                if not isinstance(allowed, bool) or not isinstance(data.get("session_id"), str):
                    raise ValueError("Choose a session and its default access")
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    session = db.execute("SELECT client_name,default_note_access FROM web_sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                                         (data["session_id"],stamp())).fetchone()
                    if not session:
                        raise LookupError("Active session not found")
                    if bool(session["default_note_access"]) != allowed:
                        db.execute("UPDATE web_sessions SET default_note_access=? WHERE id=?", (int(allowed),data["session_id"]))
                        db.execute("""INSERT INTO change_events(id,actor_type,actor_id,action,occurred_at,subject_title)
                            VALUES(?,'user',?,?,?,?)""", (secrets.token_hex(16), self.session_user_id,
                            "new note default enabled" if allowed else "new note default disabled", stamp(), session["client_name"]))
                self.send_json(200, {"allowed":allowed}); return
            if path == "/api/project-collapse":
                project_id, collapsed = data.get("project_id"), data.get("collapsed")
                if not isinstance(project_id, str) or not isinstance(collapsed, bool):
                    raise ValueError("Choose a project and its sidebar state")
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if not db.execute(VISIBLE_PROJECT, (project_id,self.can_manage_permissions,self.session_id)).fetchone():
                        raise LookupError("Project not found")
                    if collapsed:
                        db.execute("INSERT OR IGNORE INTO collapsed_projects VALUES(?,?)", (self.session_id,project_id))
                    else:
                        db.execute("DELETE FROM collapsed_projects WHERE session_id=? AND project_id=?", (self.session_id,project_id))
                self.send_json(200, {"collapsed":collapsed}); return
            if path == "/api/project-pin":
                with connect() as db:
                    try:
                        set_project_pin(db, self.session_id, self.can_manage_permissions, data["project_id"], data["previous"])
                    except KeyError:
                        raise ValueError("Missing project selection")
                    except RevisionConflict as exc:
                        self.send_json(409, {"error":str(exc)}); return
                self.send_json(200, {"saved":True}); return
            if path == "/api/device-binding":
                device = data.get("device")
                if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", device):
                    raise ValueError("Invalid device credential")
                with connect() as db:
                    updated = db.execute("UPDATE web_sessions SET device_hash=? WHERE id=? AND device_hash IS NULL",
                               (hashlib.sha256(device.encode()).hexdigest(), self.session_id))
                self.send_json(200, {"saved": bool(updated.rowcount)}); return
            if path == "/api/audit-webhook":
                if not self.require_permission_manager(): return
                with connect() as db:
                    self.send_json(200, discord_audit.configure(db, data.get("url")))
                return
            if path == "/api/speech":
                self.synthesize_speech(data)
                return
            if path == "/api/backup-permission":
                if not self.require_permission_manager(): return
                allowed = data.get("allowed")
                if not isinstance(allowed,bool):
                    raise ValueError("Choose a backup permission")
                with connect() as db:
                    cur = db.execute("UPDATE web_sessions SET can_backup=? WHERE id=? AND is_ai=0 AND revoked_at IS NULL AND expires_at>?",
                                     (int(allowed),data.get("id"),stamp()))
                    if not cur.rowcount:
                        raise ValueError("Choose an active session")
                self.send_json(200, {"saved":True}); return
            if path == "/api/backups":
                if not self.can_backup:
                    self.send_json(403, {"error":"Full backup permission required"}); return
                self.send_json(200, drive_backup.action(self.session_id,data)); return
            if path == "/api/organize":
                with connect() as db:
                    organize_item(db, data, self.session_user_id, None if self.can_manage_permissions else self.session_id)
                self.send_json(200, {"saved": True}); return
            if path == "/api/projects":
                if not self.require_permission_manager(): return
                title = clean_title(data.get("title"))
                project_id, now = secrets.token_hex(16), stamp()
                with connect() as db:
                    db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES(?,?,?,?)", (project_id,title,now,now))
                self.send_json(201, {"id": project_id, "title": title, "created_at": now})
                return
            if path == "/api/notes" or importing:
                project_id = str(data.get("project_id", ""))
                parent_id = data.get("parent_id") or None
                if not self.can_manage_permissions:
                    if parent_id and not self.has_note_access(str(parent_id)):
                        self.send_json(404,{"error":"Parent note not found"}); return
                    if not parent_id:
                        with connect() as db:
                            visible = db.execute("SELECT 1 FROM notes n JOIN session_note_access a ON a.note_id=n.id "
                                                 "WHERE n.status='active' AND n.project_id=? AND a.session_id=? AND a.allowed=1 LIMIT 1",
                                                 (project_id,self.session_id)).fetchone()
                        if not visible:
                            self.send_json(404,{"error":"Project not found"}); return
                actor = self.actor_id()
                body = ""
                if importing:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= size <= 20 * 1024 * 1024:
                        raise ValueError("Choose a file under 20 MiB")
                    document = self.rfile.read(size)
                    if len(document) != size:
                        raise ValueError("File upload was interrupted")
                    if self.headers.get_content_type() == "application/pdf":
                        body = extract_pdf_text(document)
                    elif self.headers.get_content_type() == "text/markdown":
                        body = document.decode("utf-8-sig")
                    else:
                        raise ValueError("Choose a PDF or Markdown file")
                note = create_note_record(project_id,parent_id,data.get("title"),actor,self.session_id,body)
                self.send_json(201,note)
                return
        except LookupError as exc:
            self.send_json(404,{"error":str(exc)}); return
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid request"})
            return
        self.send_json(404, {"error": "Not found"})

    def do_PUT(self) -> None:
        if not self.is_authorized():
            self.send_json(401, {"error": "Sign in to continue"}); return
        if urlparse(self.path).path == "/api/access-matrix":
            if not self.require_permission_manager(): return
            try:
                data = self.body_json()
                note_ids = data.get("note_ids", [data.get("note_id")])
                session_id = str(data.get("session_id", ""))
                allowed = data.get("allowed")
                if (not isinstance(note_ids, list) or not note_ids or
                    not all(isinstance(i, str) and re.fullmatch(r"[a-f0-9]{32}", i) for i in note_ids) or
                    not re.fullmatch(r"[a-f0-9]{32}", session_id)):
                    raise ValueError("Choose notes and a session")
                if not isinstance(allowed, bool):
                    raise ValueError("allowed must be a boolean")
                now = stamp()
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    session = db.execute("SELECT id FROM web_sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                                         (session_id,now)).fetchone()
                    if not session:
                        self.send_json(404,{"error":"Active session not found"}); return
                    for note_id in set(note_ids):
                        note = db.execute("""
                            SELECT n.title,n.revision,coalesce(a.allowed,0) AS allowed FROM notes n
                            JOIN projects p ON p.id=n.project_id
                            LEFT JOIN session_note_access a ON a.note_id=n.id AND a.session_id=?
                            WHERE n.id=? AND n.status='active' AND p.archived=0
                        """, (session_id,note_id)).fetchone()
                        if not note:
                            raise ValueError("A note is no longer available. Reload the matrix.")
                        if bool(note["allowed"]) == allowed:
                            continue
                        db.execute("INSERT INTO session_note_access(session_id,note_id,allowed,updated_at) VALUES(?,?,?,?) "
                                   "ON CONFLICT(session_id,note_id) DO UPDATE SET allowed=excluded.allowed,updated_at=excluded.updated_at",
                                   (session_id,note_id,int(allowed),now))
                        action = "access granted" if allowed else "access denied"
                        db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title) VALUES(?,?,?,?,?,?,?,?)",
                                   (secrets.token_hex(16),"user",self.session_user_id,note_id,action,now,note["revision"],note["title"]))
                result = {"session_id":session_id,"allowed":allowed}
                if "note_ids" not in data:
                    result["note_id"] = note_ids[0]
                self.send_json(200,result); return
            except (ValueError,json.JSONDecodeError) as exc:
                self.send_json(400,{"error":str(exc) or "Invalid request"}); return
        match = re.fullmatch(r"/api/notes/([a-f0-9]{32})", urlparse(self.path).path)
        if not match:
            self.send_json(404, {"error": "Not found"}); return
        if not self.has_note_access(match.group(1)):
            self.send_json(404,{"error":"Note not found"}); return
        try:
            data = self.body_json()
            expected = data.get("expected_revision")
            if expected is not None and not isinstance(expected,int):
                raise ValueError("expected_revision must be an integer")
            saved = save_note_record(match.group(1),data.get("title"),data.get("body", ""),self.actor_id(),expected,
                                     data.get("line_numbers"))
            self.send_json(200,saved)
        except RevisionConflict as exc:
            self.send_json(409,{"error":str(exc)})
        except LookupError as exc:
            self.send_json(404,{"error":str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid request"})

    def do_DELETE(self) -> None:
        if not self.is_authorized():
            self.send_json(401, {"error": "Sign in to continue"}); return
        path = urlparse(self.path).path
        if path.startswith("/api/sessions/"):
            if not self.require_permission_manager(): return
            session_id = path.rsplit("/",1)[-1]
            with connect() as db:
                db.execute("UPDATE api_tokens SET revoked_at=? WHERE replacement_id=? AND used_at IS NULL", (stamp(),session_id))
                cur = db.execute("UPDATE web_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (stamp(),session_id))
            self.send_json(200 if cur.rowcount else 404, {"disconnected": bool(cur.rowcount)}); return
        target = re.fullmatch(r"/api/(projects|notes)/([a-f0-9]{32})", path)
        if target:
            if not self.require_permission_manager(): return
            try:
                with connect() as db:
                    organize_item(db, {"kind": "project" if target[1]=="projects" else "note",
                                 "id": target[2], "action":"archive"}, self.session_user_id)
                self.send_json(200, {"archived":True})
            except ValueError as exc:
                self.send_json(400, {"error":str(exc)})
            return
        self.send_json(404, {"error": "Not found"})

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC / "index.html":
            self.send_error(HTTPStatus.NOT_FOUND); return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND); return
        mime = {".html":"text/html; charset=utf-8", ".js":"text/javascript; charset=utf-8", ".css":"text/css; charset=utf-8", ".json":"application/json; charset=utf-8", ".svg":"image/svg+xml", ".webmanifest":"application/manifest+json"}.get(target.suffix, "application/octet-stream")
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        app_shell_files = {"index.html", "app.js", "styles.css", "manifest.webmanifest", "sw.js"}
        self.send_header("Cache-Control", "no-cache" if target.name in app_shell_files else "public, max-age=3600")
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(raw)


def clean_title(value: object) -> str:
    title = str(value or "").strip()
    if not title or len(title) > 160:
        raise ValueError("Enter a title between 1 and 160 characters")
    return title


def local_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def create_token(name: str, can_manage_permissions: bool = False, is_ai: bool = False, replacement_id=None) -> None:
    with connect() as db:
        user = db.execute("SELECT id,username FROM users ORDER BY created_at LIMIT 1").fetchone()
        if user is None:
            raise SystemExit("No account exists yet. Create your account in the web app first.")
        if is_ai:
            if can_manage_permissions:
                raise SystemExit("AI sessions cannot manage permissions.")
            token = "ai_" + secrets.token_urlsafe(40)
            expires = token_expiry()
            db.execute("INSERT INTO web_sessions(id,user_id,token_hash,client_name,created_at,expires_at,is_ai) "
                       "VALUES(?,?,?,?,?,?,1)", (secrets.token_hex(16), user["id"], hashlib.sha256(token.encode()).hexdigest(),
                                                name, stamp(), expires))
            print("AI bearer token (shown once):")
            print(token)
            print(f"Expires: {local_timestamp(expires)}")
            print("Grant note access in Settings. Use this bearer token with /mcp.")
            return
        if replacement_id:
            target = db.execute("SELECT client_name,can_manage_permissions FROM web_sessions WHERE id=? AND user_id=? AND is_ai=0",
                                (replacement_id,user["id"])).fetchone()
            if not target:
                raise SystemExit("Choose an existing browser session ID from session list.")
            name, can_manage_permissions = target
            db.execute("UPDATE api_tokens SET revoked_at=? WHERE replacement_id=? AND used_at IS NULL", (stamp(),replacement_id))
        elif not can_manage_permissions:
            has_sessions = db.execute("SELECT 1 FROM web_sessions WHERE is_ai=0 LIMIT 1").fetchone()
            if not has_sessions:
                can_manage_permissions = True
        token = secrets.token_urlsafe(36)
        token_id, now, expires = secrets.token_hex(16), stamp(), token_expiry()
        db.execute("INSERT INTO api_tokens(id,user_id,name,token_hash,created_at,expires_at,can_manage_permissions,replacement_id) VALUES(?,?,?,?,?,?,?,?)",
                   (token_id,user["id"],name,hashlib.sha256(token.encode()).hexdigest(),now,expires,int(can_manage_permissions),replacement_id))
    print("Connection token (shown once):")
    print(token)
    print(f"Expires: {local_timestamp(expires)}")
    print("Permission manager: " + ("yes" if can_manage_permissions else "no"))
    print("Paste this token into the web app's connection screen. Keep it private.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Geeky Jimmy's Brainstorming Tool")
    subparsers = parser.add_subparsers(dest="command")
    token_parser = subparsers.add_parser("token", help="Manage connection tokens")
    token_commands = token_parser.add_subparsers(dest="token_action", required=True)
    create_parser = token_commands.add_parser("create", help="Create a 30-day connection token")
    create_parser.add_argument("--name", default="", help="Label for this device")
    roles = create_parser.add_mutually_exclusive_group()
    roles.add_argument("--replace-session", metavar="ID", help="Issue a single-use replacement for this browser session")
    roles.add_argument("--ai", action="store_true", help="Create an AI bearer session limited to checked notes")
    roles.add_argument("--manage-permissions", action="store_true", help="Allow this client to manage note access")
    revoke_parser = token_commands.add_parser("revoke", help="Revoke an active token by ID")
    revoke_parser.add_argument("token_id")
    token_commands.add_parser("list", help="List active token names, IDs, and expiry dates")
    session_parser = subparsers.add_parser("session", help="List connected devices")
    session_commands = session_parser.add_subparsers(dest="session_action", required=True)
    session_commands.add_parser("list", help="List all recorded connection sessions")
    audit_parser = subparsers.add_parser("audit", help="Read the note change audit log")
    audit_commands = audit_parser.add_subparsers(dest="audit_action", required=True)
    audit_list = audit_commands.add_parser("list", help="List recent note changes")
    audit_list.add_argument("--limit", type=int, default=50)
    snapshot_parser = subparsers.add_parser("snapshot", help="Create a consistent SQLite snapshot")
    snapshot_parser.add_argument("--output", required=True, help="Destination path for the snapshot")
    args = parser.parse_args()
    init_db()
    if args.command == "token":
        if args.token_action == "create":
            create_token(args.name.strip()[:80] or ("AI client" if args.ai else "Web app"), args.manage_permissions, args.ai, args.replace_session)
        elif args.token_action == "list":
            with connect() as db:
                rows = db.execute("SELECT id,name,created_at,expires_at,can_manage_permissions FROM api_tokens WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC", (stamp(),)).fetchall()
            if not rows:
                print("No active connection tokens.")
            for row in rows:
                role = "  permission-manager" if row["can_manage_permissions"] else ""
                print(f"{row['id']}  {row['name']}{role}  expires {local_timestamp(row['expires_at'])}")
        elif args.token_action == "revoke":
            with connect() as db:
                cur = db.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL", (stamp(),args.token_id))
            print("Token revoked." if cur.rowcount else "No unused token found with that ID.")
        return
    if args.command == "session":
        with connect() as db:
            rows = db.execute(
                "SELECT id,client_name,is_ai,created_at,last_used_at,expires_at,revoked_at,can_manage_permissions "
                "FROM web_sessions ORDER BY created_at DESC",
            ).fetchall()
        if not rows:
            print("No recorded connections.")
        now = stamp()
        for row in rows:
            status = "revoked" if row["revoked_at"] else "expired" if row["expires_at"] <= now else "active"
            last_used = local_timestamp(row["last_used_at"]) if row["last_used_at"] else "never"
            revoked = f"  revoked {local_timestamp(row['revoked_at'])}" if row["revoked_at"] else ""
            role = "  permission-manager" if row["can_manage_permissions"] else ""
            if row["is_ai"]:
                role = "  AI"
            print(f"{row['id']}  {row['client_name']}  {status}{role}  connected {local_timestamp(row['created_at'])}  last used {last_used}  expires {local_timestamp(row['expires_at'])}{revoked}")
        return
    if args.command == "audit":
        limit = max(1,min(args.limit,2000))
        with connect() as db:
            rows = db.execute("""
                SELECT e.action,e.subject_title,e.revision,e.occurred_at,
                       COALESCE(ai.client_name,u.username,'Unknown user') AS actor
                FROM change_events e LEFT JOIN users u ON u.id=e.actor_id
                    LEFT JOIN web_sessions ai ON e.actor_type='ai' AND ai.id=e.actor_id
                ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT ?
            """, (limit,)).fetchall()
        if not rows:
            print("No recorded note changes.")
        for row in rows:
            print(f"{local_timestamp(row['occurred_at'])}  {row['actor']}  {row['action']:<7}  {row['subject_title'] or 'Untitled'}  rev {row['revision'] or '-'}")
        return
    if args.command == "snapshot":
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == DB_PATH.resolve():
            raise SystemExit("Snapshot destination must differ from the live database")
        source = connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        print(f"Consistent SQLite snapshot created: {destination}")
        return
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=drive_backup.scheduler,daemon=True).start()
    threading.Thread(target=discord_audit.worker, args=(connect,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
