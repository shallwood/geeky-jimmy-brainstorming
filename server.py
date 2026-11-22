"""Small single-user API and static file server for Geeky Jimmy's Brainstorming Tool."""

from __future__ import annotations

import drive_backup
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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(change_events)")}
        if "subject_title" not in columns:
            db.execute("ALTER TABLE change_events ADD COLUMN subject_title TEXT")
        if "archived" not in {r["name"] for r in db.execute("PRAGMA table_info(projects)")}:
            db.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "can_backup" not in {r["name"] for r in db.execute("PRAGMA table_info(web_sessions)")}:
            db.execute("ALTER TABLE web_sessions ADD COLUMN can_backup INTEGER NOT NULL DEFAULT 0")
        token_columns = {row["name"] for row in db.execute("PRAGMA table_info(api_tokens)")}
        if "can_manage_permissions" not in token_columns:
            db.execute("ALTER TABLE api_tokens ADD COLUMN can_manage_permissions INTEGER NOT NULL DEFAULT 0")
        session_columns = {row["name"] for row in db.execute("PRAGMA table_info(web_sessions)")}
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
                       session_id: str | None = None, body: str = "") -> dict:
    if len(body) > 1_000_000:
        raise ValueError("Imported text exceeds the one-million-character note limit")
    title = clean_title(title)
    note_id, now = secrets.token_hex(16), stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
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
        if session_id:
            db.execute("INSERT INTO session_note_access(session_id,note_id,allowed,updated_at) VALUES(?,?,1,?)",
                       (session_id,note_id,now))
        db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title) VALUES(?,?,?,?,?,?,?,?)",
                   (secrets.token_hex(16),"user",actor_id,note_id,"created",now,1,title))
        row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return row_dict(row)


def save_note_record(note_id: str, title: str, body: str, actor_id: str | None, expected_revision: int | None = None,
                     line_numbers: bool | None = None) -> dict:
    title = clean_title(title)
    if not isinstance(body, str) or len(body) > 1_000_000:
        raise ValueError("Note content must be text under 1 MB")
    if line_numbers is not None and not isinstance(line_numbers, bool):
        raise ValueError("line_numbers must be a boolean")
    now = stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
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
                       (secrets.token_hex(16),"user",actor_id,note_id,"edited",now,revision,title))
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


def redeem_token(token: str) -> dict | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row = db.execute("SELECT id,user_id,name,can_manage_permissions FROM api_tokens WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?", (digest,stamp())).fetchone()
        if not row:
            return None
        session_value = "sess_" + secrets.token_urlsafe(40)
        session_id, now, expires = secrets.token_hex(16), stamp(), token_expiry()
        db.execute("UPDATE api_tokens SET used_at=? WHERE id=? AND used_at IS NULL", (now,row["id"]))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            return None
        db.execute("INSERT INTO web_sessions(id,user_id,token_hash,client_name,created_at,expires_at,last_used_at,can_manage_permissions) VALUES(?,?,?,?,?,?,?,?)",
                   (session_id,row["user_id"],hashlib.sha256(session_value.encode()).hexdigest(),row["name"],now,expires,now,row["can_manage_permissions"]))
        if row["can_manage_permissions"]:
            db.execute("INSERT INTO session_note_access(session_id,note_id,allowed,updated_at) SELECT ?,id,1,? FROM notes",
                       (session_id,now))
        return {"session":session_value,"session_id":session_id,"client_name":row["name"],"expires_at":expires,
                "can_manage_permissions":bool(row["can_manage_permissions"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "GeekyJimmyBrainstorming/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep request logs useful without logging request bodies or credentials.
        super().log_message(fmt, *args)

    def add_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://esm.sh; style-src 'self' 'unsafe-inline'; connect-src 'self' https://esm.sh; img-src 'self' data: blob:; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def send_json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.add_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def body_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2_000_000:
            raise ValueError("Request body is too large")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def is_authorized(self) -> bool:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with connect() as db:
            session = db.execute("SELECT id,user_id,can_manage_permissions,can_backup FROM web_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest, stamp())).fetchone()
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
            if not drive_backup.mounted():
                self.send_json(503, {"error": "The Google Drive picture folder is offline"})
                return
            folder = drive_backup.TARGET / "Note pictures"
            if picture_id:
                with connect() as db:
                    row = db.execute(
                        "SELECT storage_key,mime_type FROM attachments JOIN note_attachments "
                        "ON attachments.id=attachment_id WHERE note_id=? AND attachment_id=?",
                        (note_id, picture_id)).fetchone()
                if not row:
                    raise FileNotFoundError()
                picture = (folder / row["storage_key"]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", row["mime_type"])
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
            if picture.startswith(b"RIFF") and picture[8:12] == b"WEBP":
                ext = "webp"
            elif picture.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = "png"
            else:
                raise ValueError("Unsupported picture format")
            folder.mkdir(exist_ok=True)
            digest = hashlib.sha256(picture).hexdigest()
            key = digest + "." + ext
            # Identical uploads share a file. Publish only completely written images.
            if not (folder / key).is_file():
                with tempfile.NamedTemporaryFile(dir=folder, delete=False) as out:
                    temp = Path(out.name)
                try:
                    temp.write_bytes(picture)
                    os.replace(temp, folder / key)
                finally:
                    temp.unlink(missing_ok=True)
            with connect() as db:
                db.execute("INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?,?,?)",
                           (secrets.token_hex(16), key, key, "image/" + ext, size, digest, stamp()))
                picture_id = db.execute("SELECT id FROM attachments WHERE storage_key=?", (key,)).fetchone()[0]
                db.execute("INSERT OR IGNORE INTO note_attachments(note_id,attachment_id) VALUES(?,?)",
                           (note_id, picture_id))
            self.send_json(201, {"id": picture_id})
        except FileNotFoundError:
            self.send_json(404, {"error": "Picture unavailable"})
        except OSError:
            self.send_json(503, {"error": "Could not access the Google Drive picture folder"})

    def actor_id(self) -> str | None:
        digest = hashlib.sha256(self.headers.get("Authorization", "").removeprefix("Bearer ").encode()).hexdigest()
        with connect() as db:
            row = db.execute("SELECT user_id FROM web_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest,stamp())).fetchone()
        return row["user_id"] if row else None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
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
            self.send_json(200, {"authenticated":authenticated,
                                 "can_manage_permissions":bool(getattr(self,"can_manage_permissions",False))})
            return
        if path == "/api/sessions":
            if not self.is_authorized():
                self.send_json(401, {"error":"Sign in to continue"}); return
            if not self.require_permission_manager(): return
            with connect() as db:
                rows = db.execute("SELECT id,client_name,created_at,expires_at,last_used_at,revoked_at,can_manage_permissions,can_backup FROM web_sessions ORDER BY created_at DESC").fetchall()
            self.send_json(200, [row_dict(r) for r in rows]); return
        if path == "/api/access-matrix":
            if not self.is_authorized():
                self.send_json(401, {"error":"Sign in to continue"}); return
            if not self.require_permission_manager(): return
            now = stamp()
            with connect() as db:
                sessions = [row_dict(row) for row in db.execute(
                    "SELECT id,client_name FROM web_sessions WHERE revoked_at IS NULL AND expires_at>? ORDER BY created_at",
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
                projects = [row_dict(r) for r in db.execute("SELECT * FROM projects WHERE archived=0 ORDER BY created_at, title")]
                for project in projects:
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
                           COALESCE(u.username,'Unknown user') AS actor
                    FROM change_events e LEFT JOIN users u ON u.id=e.actor_id
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
        path = urlparse(self.path).path
        if path == "/api/logout":
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            with connect() as db:
                db.execute("UPDATE web_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (stamp(),hashlib.sha256(supplied.encode()).hexdigest()))
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
                session = redeem_token(token_input)
                if session:
                    self.send_json(200, session); return
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
            if path == "/api/backup-permission":
                if not self.require_permission_manager(): return
                allowed = data.get("allowed")
                if not isinstance(allowed,bool):
                    raise ValueError("Choose a backup permission")
                with connect() as db:
                    cur = db.execute("UPDATE web_sessions SET can_backup=? WHERE id=? AND revoked_at IS NULL AND expires_at>?",
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
                note_id = str(data.get("note_id", ""))
                session_id = str(data.get("session_id", ""))
                allowed = data.get("allowed")
                if not re.fullmatch(r"[a-f0-9]{32}",note_id) or not re.fullmatch(r"[a-f0-9]{32}",session_id):
                    raise ValueError("Choose a note and session")
                if not isinstance(allowed,bool):
                    raise ValueError("allowed must be a boolean")
                now = stamp()
                with connect() as db:
                    note = db.execute("SELECT id,title,revision FROM notes WHERE id=? AND status='active'",(note_id,)).fetchone()
                    session = db.execute("SELECT id FROM web_sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                                         (session_id,now)).fetchone()
                    if not note or not session:
                        self.send_json(404,{"error":"Active note or session not found"}); return
                    previous = db.execute("SELECT allowed FROM session_note_access WHERE session_id=? AND note_id=?",
                                          (session_id,note_id)).fetchone()
                    db.execute("INSERT INTO session_note_access(session_id,note_id,allowed,updated_at) VALUES(?,?,?,?) "
                               "ON CONFLICT(session_id,note_id) DO UPDATE SET allowed=excluded.allowed,updated_at=excluded.updated_at",
                               (session_id,note_id,int(allowed),now))
                    if previous is None or bool(previous["allowed"]) != allowed:
                        action = "access granted" if allowed else "access denied"
                        db.execute("INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title) VALUES(?,?,?,?,?,?,?,?)",
                                   (secrets.token_hex(16),"user",self.session_user_id,note_id,action,now,note["revision"],note["title"]))
                self.send_json(200,{"note_id":note_id,"session_id":session_id,"allowed":allowed}); return
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


def create_token(name: str, can_manage_permissions: bool = False) -> None:
    with connect() as db:
        user = db.execute("SELECT id,username FROM users ORDER BY created_at LIMIT 1").fetchone()
        if user is None:
            raise SystemExit("No account exists yet. Create your account in the web app first.")
        if not can_manage_permissions:
            has_sessions = db.execute("SELECT 1 FROM web_sessions LIMIT 1").fetchone()
            if not has_sessions:
                can_manage_permissions = True
        token = secrets.token_urlsafe(36)
        token_id, now, expires = secrets.token_hex(16), stamp(), token_expiry()
        db.execute("INSERT INTO api_tokens(id,user_id,name,token_hash,created_at,expires_at,can_manage_permissions) VALUES(?,?,?,?,?,?,?)",
                   (token_id,user["id"],name,hashlib.sha256(token.encode()).hexdigest(),now,expires,int(can_manage_permissions)))
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
    create_parser.add_argument("--name", default="Web app", help="Label for this device")
    create_parser.add_argument("--manage-permissions", action="store_true", help="Allow this client to manage note access")
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
            create_token(args.name.strip()[:80] or "Web app", args.manage_permissions)
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
                "SELECT client_name,created_at,last_used_at,expires_at,revoked_at,can_manage_permissions "
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
            print(f"{row['client_name']}  {status}{role}  connected {local_timestamp(row['created_at'])}  last used {last_used}  expires {local_timestamp(row['expires_at'])}{revoked}")
        return
    if args.command == "audit":
        limit = max(1,min(args.limit,2000))
        with connect() as db:
            rows = db.execute("""
                SELECT e.action,e.subject_title,e.revision,e.occurred_at,
                       COALESCE(u.username,'Unknown user') AS actor
                FROM change_events e LEFT JOIN users u ON u.id=e.actor_id
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
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
