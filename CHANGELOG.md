Created by: Codex | 11/22/2026 | 6:25 AM EST

# Geeky Jimmy’s Brainstorming Tool changelog

## Browser sessions, project pins and image attachments

- A remembered browser profile can redeem a fresh token to reconnect to its original permission-matrix row after confirmation. The owner can issue a single-use token to transfer or restore a specific browser session. Reconnection rotates both credentials.
- Right-click or hold a project to pin it for this session. Replacing a pin names both projects and requires confirmation. Cancel keeps the existing pin.
- Authorized AI clients can insert a PNG, JPEG or WebP into a permitted note and retrieve a linked image. Uploads use the existing Drive picture folder, revision checks, picture preview and audit log.

Added by: Codex | 11/22/2026 | 6:26 AM EST

## Default access for new notes

Permission managers can select additional browser and AI sessions under Settings → Note access by session → Default for new notes. Each new note, imported note or subnote grants access to its creator and the selected active sessions. Each checkbox saves immediately and applies across projects to future creations.

Added by: Codex | 11/22/2026 | 6:33 AM EST

## Sidebar project state

Collapsing or expanding a project saves the choice for the current session. Refreshing the sidebar, reopening the app or restoring the last note respects that choice.

Added by: Codex | 11/22/2026 | 6:37 AM EST

## Scrollable note access matrix

The matrix scrolls inside a viewport capped at 60% of the screen height or 30 rem, whichever is smaller. Session headings stay at the top while scrolling down, and note names stay on the left while scrolling across.


Added by: Codex | 11/22/2026 | 6:48 AM EST

## 48-hour retrospective — 2026-11-20 to 2026-11-22: earlier foundation and integrations

Retrospective record entered now for work in the preceding 48 hours. Work dates are separate from this recording timestamp; no unrecorded execution times are inferred. Status reflects the evidence available when the work was done, not a new deployment or live audit. The four entries above already cover browser reconnection, project pins, AI image attachments, default note access, collapsed-project state and the scrollable permission matrix; they are preserved without duplication.

### Notes workspace and document handling

- Built the Docker-hosted PWA with project sidebar, three-level notes, Markdown editing/formatting, local drafts, automatic sync, search, per-note line numbers and theme controls. Added Markdown/PDF import, note/project organization, archiving/restoring and on-device read aloud.
- Implemented named browser sessions, one-use connection tokens, per-session note permissions, permission-manager controls and edit auditing. The server enforces the note access matrix.
- Added inline image markers with authenticated popup viewing, browser resizing, duplicate reuse and storage in the existing Drive picture folder. Encrypted backups retain image references; the separate picture folder must also be preserved for recovery.

### Encrypted Drive backups and local timestamps

- Added encrypted full-workspace backups through the host's rclone Drive mount, with manual, daily and seven-day scheduling and a separate backup-management permission. Added a reusable latest-backup status text file.
- Kept database timestamps in UTC, displayed browser timestamps in the viewer's timezone and used the host's local timezone for CLI/backup display.
- Status recorded by the originating chat: the owner confirmed the Drive mount and an encrypted backup arriving on the Mac. A complete restore/decryption drill remained unverified. No new backup or restore was run for this backfill.

### Optional cloud read aloud

- Added optional Google speech support using the configured voice, separate credentials and explicit selection. Google billing/credentials remained disabled by owner choice; live Google playback was not verified.
- Added Azure voice selection/preview and support for a configured resource endpoint. The owner confirmed Jenny playback. Other voices still depend on the resource/region's capabilities; this entry does not claim that each voice was verified.

### AI API, OAuth and scoped documentation imports

- Added per-AI bearer sessions and MCP read/edit/create operations enforced by the existing note matrix, with revision checks and revocation. Added OAuth discovery/authorization linked to the selected AI session and corrected the same-origin authorization form policy.
- A live permitted connection was subsequently verified. Imported 28 Markdown documents into the relevant app projects and verified their contents against source files. No note permissions were broadened for this log backfill.
- Added AI line-number controls and a one-time line-number migration for the 28 imported documents, preserving later individual choices.

### Discord audit mirroring and activation status

- Added permission-manager webhook configuration and queued mirroring of new audit entries, including attribution, action and timestamp. Retries, restart recovery and webhook replacement/disconnection behaviour were tested with synthetic data.
- Historical validation reports include eight backend tests for audit/line-number work and twelve backend tests after the later session/sidebar changes. This documentation task does not rerun or upgrade those checks.
- Recent app changes were supplied for the owner's Docker Compose rebuild. Live Azure playback and AI access were confirmed in the originating chat, but later sidebar/matrix changes still needed post-rebuild visual verification. A live Discord delivery was not confirmed in the reviewed work.
