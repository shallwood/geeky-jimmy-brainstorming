"""Forward committed audit rows in order, resuming from the last acknowledgement."""
from datetime import datetime
import re
import sqlite3
import threading
import http.client
import json
import math
from urllib.parse import urlsplit



SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_audit (
    id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL,
    delivered_row INTEGER NOT NULL, status TEXT NOT NULL DEFAULT ''
);
"""
URL = re.compile(r"https://(?:discord\.com|discordapp\.com)/api/(?:v[0-9]+/)?webhooks/[0-9]{1,22}/[A-Za-z0-9_-]{20,200}")
changed = threading.Event()


def status(db):
    row = db.execute("SELECT status FROM discord_audit WHERE id=1").fetchone()
    return {"configured": bool(row), "status": row["status"] if row else ""}


def configure(db, url):
    if not isinstance(url, str) or (url and not URL.fullmatch(url)):
        raise ValueError("Enter a Discord webhook URL.")
    db.execute("BEGIN IMMEDIATE")
    previous = db.execute("SELECT url FROM discord_audit WHERE id=1").fetchone()
    if not url:
        db.execute("DELETE FROM discord_audit")
    elif not previous or previous["url"] != url:
        db.execute("""INSERT OR REPLACE INTO discord_audit(id,url,delivered_row,status)
            SELECT 1,?,COALESCE(MAX(rowid),0),'Waiting for the next audit event.' FROM change_events""", (url,))
    db.commit()
    changed.set()
    return status(db)


def deliver(connect):
    with connect() as db:
        row = db.execute("""SELECT cfg.url,cfg.delivered_row,e.rowid AS sequence,e.*,
            COALESCE(ai.client_name,u.username,e.actor_type) AS actor
            FROM discord_audit cfg JOIN change_events e ON e.rowid>cfg.delivered_row
            LEFT JOIN users u ON u.id=e.actor_id
            LEFT JOIN web_sessions ai ON e.actor_type='ai' AND ai.id=e.actor_id
            ORDER BY e.rowid LIMIT 1""").fetchone()
    if not row:
        return 3
    timestamp = int(datetime.fromisoformat(row["occurred_at"]).timestamp())
    revision = f" · rev {row['revision']}" if row["revision"] is not None else ""
    content = f"<t:{timestamp}:f> · {row['actor']} · {row['action']} · {row['subject_title'] or 'Untitled'}{revision}"
    delay = 0
    cursor = row["delivered_row"]
    destination = urlsplit(row["url"])
    connection = http.client.HTTPSConnection(destination.hostname, timeout=10)
    try:
        payload = json.dumps({"content": content[:1900], "allowed_mentions": {"parse": []}})
        connection.request("POST", destination.path + "?wait=true", payload,
                           {"Content-Type": "application/json", "User-Agent": "Brainstorming-Audit/1.0"})
        response = connection.getresponse()
        if 200 <= response.status < 300:
            cursor = row["sequence"]
            message = "Last audit event delivered."
        else:
            message = f"Delivery pending (Discord HTTP {response.status})."
            delay = 60
            if response.status == 429:
                try:
                    retry = float(response.getheader("Retry-After") or json.loads(response.read(4096))["retry_after"])
                    if math.isfinite(retry):
                        delay = min(threading.TIMEOUT_MAX, max(1, retry))
                except (ValueError, KeyError, TypeError):
                    pass
    except (OSError, http.client.HTTPException):
        message, delay = "Delivery pending. Could not reach Discord.", 60
    finally:
        connection.close()
    with connect() as db:
        db.execute("""UPDATE discord_audit SET delivered_row=?,status=?
            WHERE id=1 AND url=? AND delivered_row=?""", (cursor, message, row["url"], row["delivered_row"]))
    return delay


def worker(connect):
    while True:
        changed.clear()
        try:
            delay = deliver(connect)
        except sqlite3.Error:
            delay = 5
        changed.wait(delay)
