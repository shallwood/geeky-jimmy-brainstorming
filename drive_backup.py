"""Encrypted workspace backups. Drive credentials live outside the workspace snapshot."""
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

DATA = Path(os.environ.get("DATA_DIR", "/data"))
TARGET = Path("/backups")
RECIPIENT = os.environ.get("BACKUP_AGE_RECIPIENT", "")


def mounted():
    # A bind of the empty mountpoint after reboot must never count as a backup.
    with open("/proc/self/mountinfo") as mounts:
        return any(line.split()[4] == str(TARGET) and line.split(" - ",1)[1].split()[0] == "fuse.rclone"
                   for line in mounts)


lock = threading.Lock()
state_lock = threading.RLock()


def settings(**changes):
    path = DATA / "drive-settings.json"
    with state_lock:
        current = json.loads(path.read_text()) if path.exists() else {}
        if changes:
            current.update(changes)
            with tempfile.NamedTemporaryFile(mode="w", dir=DATA, delete=False) as f:
                json.dump(current, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(f.name, path)
        return current


def status():
    s = settings()
    return dict(configured=bool(RECIPIENT and mounted()),
                hours=s.get("hours", 0), last=s.get("last", ""), error=s.get("error", ""), running=lock.locked())


def package(source, folder):
    snapshot = folder / "brainstorming.sqlite3"
    with closing(sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)) as live, closing(sqlite3.connect(snapshot)) as db:
        live.backup(db)
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        bundle = folder / "workspace.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, snapshot.name)
            manifest = dict(version=1, projects=[dict(r) for r in db.execute("SELECT * FROM projects")], notes=[])
            for row in db.execute("SELECT * FROM notes"):
                note = dict(row)
                path = "notes/" + note["id"] + ".md"
                z.writestr(path, note.pop("body"))
                manifest["notes"].append(dict(note, file=path))
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return bundle


def write_status_file(message, finished):
    if not mounted():
        raise ValueError("Drive is no longer mounted")
    local_time = datetime.fromtimestamp(finished).astimezone()
    with tempfile.NamedTemporaryFile(mode="w", dir=TARGET, prefix=".status-", delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(f"{local_time:%Y-%m-%d %H:%M:%S %Z (%z)}\n{message}\n")
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, TARGET / "Latest backup status.txt")
    finally:
        temp.unlink(missing_ok=True)


def run():
    if not lock.acquire(blocking=False):
        raise ValueError("A backup is already running")
    def worker():
        message = "Backup interrupted."
        finished = time.time()
        try:
            if not status()["configured"]:
                raise ValueError("Check the mounted Drive folder and the backup encryption recipient.")
            with tempfile.TemporaryDirectory(dir=DATA) as tmp:
                folder = Path(tmp)
                bundle = package((DATA/"brainstorming.sqlite3").resolve(), folder)
                cipher = folder/"backup.age"
                subprocess.run(["age","-r",RECIPIENT,"-o",str(cipher),str(bundle)],check=True,timeout=300)
                name = time.strftime("brainstorming-%Y%m%dT%H%M%S%z-",time.localtime()) + secrets.token_hex(3) + ".zip.age"
                if not mounted():
                    raise ValueError("Drive is no longer mounted")
                temp = TARGET / (name + ".partial")
                try:
                    with cipher.open("rb") as src, temp.open("xb") as dest:
                        shutil.copyfileobj(src, dest)
                        dest.flush()
                        os.fsync(dest.fileno())
                    os.rename(temp, TARGET / name)
                finally:
                    temp.unlink(missing_ok=True)
            finished = time.time()
            settings(last=datetime.fromtimestamp(finished, timezone.utc).isoformat(timespec="seconds"), error="", attempt=finished)
            message = f"Backup written to Drive mount: {name}"
        except Exception as e:
            finished = time.time()
            message = str(e) if isinstance(e, ValueError) else f"Check Drive access, encryption key and disk space ({type(e).__name__})."
            message = "Backup failed: " + message
            settings(error=message, attempt=finished)
        finally:
            try:
                if mounted():
                    write_status_file(message, finished)
            except (OSError, ValueError):
                current_error = settings().get("error", "")
                settings(error=(current_error + " Could not update Latest backup status.txt on Drive.").strip())
            finally:
                lock.release()
    threading.Thread(target=worker,daemon=True).start()


def scheduler():
    while True:
        time.sleep(30)
        s = settings()
        if s.get("hours") and time.time()-s.get("attempt",0)>=s["hours"]*3600:
            try:
                run()
            except ValueError:
                pass


def action(session, data):
    op = data.get("action")
    if op == "schedule":
        hours = data.get("hours")
        if hours not in (0, 24, 168) or (hours and not status()["configured"]):
            raise ValueError("Configure rclone and select a schedule")
        settings(hours=hours,attempt=time.time())
    elif op == "run":
        if not status()["configured"]:
            raise ValueError("Configure rclone and the backup encryption recipient first")
        run()
    else:
        raise ValueError("Unknown backup action")
    return status()
