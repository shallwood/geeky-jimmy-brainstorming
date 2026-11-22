# Back up the workspace to Google Drive

Run these on puppyshome as your Ubuntu login. Docker only mounts the backup folder; Google credentials stay in your host rclone config.

1. Install the host tools: `sudo apt update && sudo apt install rclone fuse3 age`. Run `rclone config` **as your normal user**. Create a remote named `gdrive`, choose Google Drive, leave client ID/secret blank to use the built-in client, choose the drive.file scope for app-created files, and skip advanced configuration. Answer No to automatic browser authorization on headless Ubuntu. On your Mac, install rclone (`brew install rclone`) and run the exact authorize command Ubuntu prints. Sign in to Google and paste the token into the Ubuntu terminal. Choose No for shared drive if using personal Drive, confirm and quit. See https://rclone.org/remote_setup/.
2. Create the remote folder and host mountpoint:

   ```sh
   rclone mkdir 'gdrive:Brainstorming Backups'
   sudo install -d -m 0700 -o "$USER" -g "$(id -gn)" /mnt/gdrive
   ```

   Enable `user_allow_other` in `/etc/fuse.conf` (needed for the container's separate UID). For a persistent mount, install this project's user service:

   ```sh
   mkdir -p ~/.config/systemd/user
   cp brainstorming-drive.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now brainstorming-drive
   sudo loginctl enable-linger "$USER"
   findmnt -T /mnt/gdrive -o TARGET,FSTYPE
   ```

   Confirm the filesystem is `fuse.rclone`. Only the backup subfolder is exposed. With --allow-other the mounted backup folder is accessible to other local users. Its backup files are encrypted; keep the private recovery key outside the mount.

3. Generate a recovery key **once**, outside the project: `age-keygen -o ~/brainstorming-recovery.agekey`. Save a second private copy. Run `age-keygen -y ~/brainstorming-recovery.agekey` to print the public recipient.
4. Add these lines to your existing app `.env`, replacing the public recipient:

   ```dotenv
   BACKUP_AGE_RECIPIENT=age1YOUR_PUBLIC_RECIPIENT
   COMPOSE_FILE=compose.yaml:compose.drive.yaml
   ```

   Run `docker compose up -d --build` **after** mounting Drive. If the mount restarts or Docker started first after reboot, run `docker compose up -d --force-recreate brainstorming` after the mount is ready. The app refuses to back up to an ordinary local directory.
5. In Settings → Device sessions, enable **Manage full backups** for your existing trusted session. Settings → Google Drive backups offers manual, every 24 hours, or every 7 days. Each backup contains the full SQLite snapshot, Markdown, and a manifest covering active and archived notes. The app reports when it has written to the mount. rclone handles the final cloud upload; check with `rclone ls 'gdrive:Brainstorming Backups'` or check `journalctl --user -u brainstorming-drive` for upload errors. Backups are retained until you remove them. The temporary VFS cache needs free host disk space.
   
## Recovery drill

Download a .zip.age from Drive. In a new private directory on a machine with age and unzip:

```sh
umask 077
age -d -i ~/brainstorming-recovery.agekey -o workspace.zip BACKUP.zip.age
unzip workspace.zip
python3 -c 'import sqlite3; db=sqlite3.connect("file:brainstorming.sqlite3?mode=ro",uri=True); print(db.execute("pragma integrity_check").fetchone(),db.execute("select count(*) from notes").fetchone())'
```

To start a separate instance on Ubuntu, use the same source and a fresh Compose project name, volume and host port. From the app directory run these commands, replacing the recovery path. Choose an unused project name and port.

```sh
HOST_PORT=18081 docker compose -f compose.yaml -p brainstorming-recovery run --rm -T brainstorming sh -c 'test ! -e /data/brainstorming.sqlite3 && cat > /data/brainstorming.sqlite3' < /path/to/recovery/brainstorming.sqlite3
HOST_PORT=18081 docker compose -f compose.yaml -p brainstorming-recovery up -d
```

Before using a restored instance as production, review its device sessions: a snapshot restores the session permissions and revocations as they stood at backup time. Pair Drive again if you want scheduled backups on the restored host.
