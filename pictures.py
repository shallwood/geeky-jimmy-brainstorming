"""Shared picture files and note attachment references."""
import base64
import hashlib
import http.client
import re
from urllib.parse import urlsplit
import io
import os
from pathlib import Path
import secrets
import tempfile

from PIL import Image

import drive_backup

LIMIT = 8 * 1024 * 1024


def folder():
    if not drive_backup.mounted():
        raise OSError("Google Drive is offline")
    return drive_backup.TARGET / "Note pictures"


def format_of(data):
    if not 0 < len(data) <= LIMIT:
        raise ValueError("Choose a picture of 8 MiB or smaller")
    try:
        with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as image:
            if image.width * image.height > 16_000_000 or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Choose a still image under 16 megapixels")
            ext = image.format.lower()
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
        raise ValueError("Choose a valid PNG, JPEG or WebP image") from exc
    return ext


def read(db, note, attachment):
    row = db.execute("""SELECT storage_key,mime_type FROM attachments JOIN note_attachments
        ON attachments.id=attachment_id WHERE note_id=? AND attachment_id=?""", (note, attachment)).fetchone()
    if not row:
        raise LookupError("Picture unavailable")
    return (folder() / row["storage_key"]).read_bytes(), row["mime_type"]


def store(db, note, data, ext, now, after_link=None):
    # Caller holds the SQLite write lock. Publish and link a complete file before commit.
    directory = folder()
    directory.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    key = digest + "." + ext
    path = directory / key
    published = False
    existing = db.execute("SELECT id FROM attachments WHERE storage_key=?", (key,)).fetchone()
    try:
        if not path.is_file():
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as out:
                temp = Path(out.name)
            try:
                temp.write_bytes(data)
                os.replace(temp, path)
                published = True
            finally:
                temp.unlink(missing_ok=True)
        ident = existing["id"] if existing else secrets.token_hex(16)
        if not existing:
            db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?)",
                       (ident,key,key,"image/" + ext,len(data),digest,now))
        db.execute("INSERT OR IGNORE INTO note_attachments(note_id,attachment_id) VALUES(?,?)", (note,ident))
        if after_link:
            after_link(ident)
        db.commit()
        return ident
    except Exception:
        if published and not existing:
            path.unlink(missing_ok=True)
        raise


def chat_file(ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("file_id"), str) or not ref["file_id"]:
        raise ValueError("Select a file attached to this conversation")
    if not isinstance(ref.get("download_url"), str):
        raise ValueError("Missing file download URL")
    url = urlsplit(ref["download_url"])
    host = url.hostname or ""
    trusted = (host == "files.oaiusercontent.com" or host.endswith(".oaiusercontent.com")
               or re.fullmatch(r"(?:oai[a-z0-9]*|sdmnt[a-z0-9]*)\.blob\.core\.windows\.net", host)
               or host == "chatgpt.com" and url.path.startswith("/backend-api/files/"))
    if not trusted or url.scheme != "https" or url.port not in (443, None) or url.username or url.password:
        raise ValueError("Unsupported ChatGPT download host. Use a fresh attachment or send base64 image data.")
    connection = http.client.HTTPSConnection(host, timeout=15)
    try:
        connection.request("GET", url.path + ("?" + url.query if url.query else ""))
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Could not retrieve the attachment. Select it again.")
        return response.read(LIMIT + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("Could not download the attached file") from exc
    finally:
        connection.close()


def ai_tool(name, args, session, connect, stamp, permitted_notes):
    note = args["note_id"]
    try:
        if name == "attach_image":
            with connect() as check:
                if not check.execute("SELECT 1 " + permitted_notes + " AND n.id=?", (session,stamp(),note)).fetchone():
                    raise LookupError("Note not found")
            if ("file" in args) == ("data_base64" in args):
                raise ValueError("Supply one attached file or base64 image")
            data = chat_file(args["file"]) if "file" in args else base64.b64decode(args["data_base64"], validate=True)
            ext = format_of(data)
        with connect() as db:
            if name == "attach_image":
                db.execute("BEGIN IMMEDIATE")
            now = stamp()
            row = db.execute("SELECT n.body,n.title,n.revision " + permitted_notes + " AND n.id=?",
                             (session, now, note)).fetchone()
            if not row:
                raise LookupError("Note not found")
            if name == "read_image":
                data, mime = read(db, note, args["attachment_id"])
                return {"type":"image", "mimeType":mime, "data":base64.b64encode(data).decode()}
            if args["expected_revision"] != row["revision"]:
                raise ValueError("Revision conflict. Read the note again before attaching.")
            body = row["body"]
            offset = args.get("offset", len(body))
            if not 0 <= offset <= len(body) or len(body) + len("![Picture](picture:)") + 32 > 1_000_000:
                raise ValueError("Invalid insertion position or note size")
            revision = row["revision"] + 1

            def insert_marker(attachment):
                marker = f"![Picture](picture:{attachment})"
                db.execute("UPDATE notes SET body=?,revision=?,updated_at=? WHERE id=?",
                           (body[:offset] + marker + body[offset:],revision,now,note))
                db.execute("""INSERT INTO change_events(id,actor_type,actor_id,note_id,action,occurred_at,revision,subject_title)
                    VALUES(?,'ai',?,?,'attached image',?,?,?)""",
                           (secrets.token_hex(16),session,note,now,revision,row["title"]))

            attachment = store(db, note, data, ext, now, insert_marker)
            return {"note_id":note,"attachment_id":attachment,"revision":revision}
    except OSError as exc:
        raise ValueError("Picture storage unavailable") from exc
