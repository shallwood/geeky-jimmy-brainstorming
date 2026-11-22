# Geeky Jimmy’s Brainstorming Tool

A private, local-first Markdown workspace for ideas and projects. This is a standalone app and is not part of Accountability Bot.

## Stage 1.1

This first increment includes a Docker-hosted PWA, project sidebar, three-level note hierarchy, Markdown editor and formatting toolbar, local draft storage, automatic server sync, search, light/dark/system themes, one-use device tokens, per-note line-number preferences, per-session note access controls, and a CLI-readable edit audit log.

The UI follows the approved mockup’s full-screen editor, compact branded header, and project tree. Its default theme uses phosphor green on black; the light theme uses black on white with green accents. The PWA uses the Geeky Jimmy app icon in `static/` for the browser, iOS home screen, and installed app.

Formatting stays Markdown-native: `**bold**`, `*italic*`, `__underline__` (Discord style), `~~strike~~`, backticks for inline code, and `==highlight==` for highlighting (a common extension because core Markdown has no highlight syntax).

## Start the app

On the Ubuntu Docker host, in this repository directory:

```sh
docker compose up -d --build
```

The container listens on `127.0.0.1:8081` by default (configurable with `HOST_PORT` in `.env`). The named Docker volume `brainstorming_data` stores the SQLite database. The image runs as a non-root user with a read-only root filesystem and no Linux capabilities.

## Private Tailscale access

1. Install and sign in to Tailscale on the Ubuntu host. Enable HTTPS certificates for the tailnet if Tailscale prompts you.
2. Start private HTTPS forwarding to the loopback-only app:

   ```sh
   tailscale serve --bg 8081
   tailscale serve status
   ```

3. Open the HTTPS `*.ts.net` address reported by Tailscale from a device on the same tailnet. Do not configure Tailscale Funnel or a public reverse proxy for this app.
4. On a supported browser, install the PWA (on iPhone/iPad: Safari Share → Add to Home Screen).

Tailscale Serve forwards to a local service and provisions HTTPS certificates for tailnet access; its current command and HTTPS requirements are documented by [Tailscale](https://tailscale.com/docs/reference/tailscale-cli/serve) and [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve).

## First connection and one-use tokens

On first open, create the local account name. The token itself is always generated on the Docker host; the web UI never creates or displays one.

Generate one token for each device/client and enter it in that device’s connection screen:

```sh
docker compose exec brainstorming python server.py token create --name "iPhone"
```

New sessions start without access to notes. In Settings → Note access by session, check the notes each active session may read and edit. To connect a trusted control device that can manage the matrix, create its token with `--manage-permissions`.

When upgrading an existing database, current note access is preserved. The earliest active session becomes the initial permission manager; other active sessions can be individually restricted in the matrix.

The token is printed once and expires 30 days after creation if unused. Entering it exchanges it for a device session lasting 30 days; the token is immediately marked used and cannot be entered again. Do not share one token between devices. Disconnect a device in Settings or sign out to revoke its session. Reconnecting requires a fresh CLI-generated token.

CLI token commands:

```sh
docker compose exec brainstorming python server.py token list
docker compose exec brainstorming python server.py token revoke TOKEN_ID
```

The app's **Settings → Device sessions** page and the CLI command below list all recorded browser/app sessions, including active, expired, and revoked sessions:

```sh
docker compose exec brainstorming python server.py session list
```

## AI clients

Create a separate connection for each AI on the Docker host:

```sh
docker compose exec brainstorming python server.py token create --name "Claude" --ai
```

The CLI displays a 30-day bearer token once. Register the app's HTTPS `/mcp` URL in a client supporting Streamable HTTP MCP and configure its Authorization header as `Bearer <token>`. For ChatGPT, open **Plugins → + → Add custom MCP server**, use **Geeky Jimmy’s Brainstorming Tool** as the name, enter `https://notes.accountabilitybot.cc/mcp`, and select **OAuth**. Use automatic discovery and dynamic client registration. When Brainstorming prompts you, enter the existing AI token for this connection. The link uses that same session and its current matrix entries. The server supports the ChatGPT callback and authorization code flow with S256 PKCE. Access tokens last one hour and refresh tokens rotate. Both stop working when the underlying AI session expires or is revoked.

If hosting on another domain, set `MCP_PUBLIC_ORIGIN` to the external HTTPS origin before recreating the container. Route `/.well-known/`, `/oauth/`, and `/mcp` on that origin to Brainstorming.

The AI session appears immediately in Settings → Device sessions and in the note access matrix, labeled AI. Check individual notes to grant read and edit access. List, search, read and edit operations recheck the active session and matrix on the server. Edits require the current revision. Use `read_note` to check a note’s line-number setting, then `set_line_numbers` with its ID, expected revision, and a boolean `line_numbers` value. An AI can start an empty project, add a root note in a visible project, or add a subnote under a permitted parent. Its newly created notes get their own checked entries for that AI. Access to siblings and descendants is independent. Disconnecting the session revokes its token.

## Pictures in notes

Choose **Insert picture** at the cursor, paste a picture, or drop one into the editor. Tap its picture icon to open the screen-sized preview. The browser stores a resized still image (up to 2048 pixels on the longest edge) in the mounted Drive folder under `Note pictures`. Identical uploads reuse the same picture. Viewing requires access to the original note and a working Drive mount. Drive users with direct folder access can open the picture files.

The encrypted backup contains the note text and attachment references. Keep the Drive picture folder alongside the backups when restoring or migrating. Removing a marker leaves its file available for older backups.

## Audit log

Note creation, saved edits, deletion, and session access changes record the account name, timestamp, note title, action, and revision. Read the most recent events from the Docker host:

```sh
docker compose exec brainstorming python server.py audit list --limit 50
```

The audit log is stored in the persistent SQLite database. A permission manager can open **Settings → Discord audit log** and save a channel webhook. New audit entries include the actor, action, title, revision when available, and a timestamp displayed in the Discord viewer’s local time zone. Delivery retries in the background and resumes after restarts. Disconnecting or replacing the webhook discards pending deliveries to the previous channel.

## Manual snapshot

Create a transactionally consistent SQLite snapshot in the data volume:

```sh
docker compose exec brainstorming python server.py snapshot --output /data/brainstorming-snapshot.sqlite3
```

Copy it out of the container to your own protected backup location:

```sh
container_id=$(docker compose ps -q brainstorming)
docker cp "$container_id:/data/brainstorming-snapshot.sqlite3" ./brainstorming-snapshot.sqlite3
```

For encrypted Google Drive backups, scheduling, and recovery, see [Backups](BACKUPS.md).

## Development and checks

The Python server uses only the standard library. CodeMirror 6 and its Markdown language support are loaded as ES modules from esm.sh on first use; the service worker caches successful module responses for later offline use. A native textarea fallback remains available if the editor modules cannot load.

Run the API and hierarchy tests with:

```sh
python3 -m unittest discover -s tests -v
```

The PWA requires HTTPS when accessed from another device; `localhost` is also a secure browser context for local development.

## Data and Git safety

User data lives in the Docker volume and never in this repository. `.gitignore` excludes local data directories, SQLite databases, exports, attachment storage, and credentials. Start from `.env.example` only for non-secret settings; do not commit tokens, Tailscale details, or private notes.

## Current gaps

- Stable `[[note]]` links and tag editing are reserved for a follow-up increment.
- Audit events are available through the CLI only.
- The service worker caches the app shell and same-origin files plus CodeMirror module responses; local note drafts are kept in IndexedDB and sync when the server returns. Browser storage may still be cleared by the device OS.
- Native clients and OAuth-based AI connectors are pending.
- Choose and add the repository licence before publicly publishing; MIT was suggested in the brief, but needs the owner’s approval.

## GitHub

The GitHub target supplied for this project is `https://github.com/shallwood/geeky-jimmy-brainstorming.git`. This mounted workspace blocks writes to `.git`, so Git initialization and setting `origin` could not be completed here. From a normal writable checkout, initialize and publish after choosing the licence:

```sh
git init -b main
git add .
git commit -m "Build Stage 1.1 brainstorming editor"
git remote add origin https://github.com/shallwood/geeky-jimmy-brainstorming.git
git push -u origin main
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Azure Read Aloud

Read Aloud defaults to the browser's existing voice. On `puppyshome`, set `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION` on separate lines in `/etc/brainstorming/secrets.env`. For a Foundry resource, set `AZURE_SPEECH_ENDPOINT=https://RESOURCE-NAME.cognitiveservices.azure.com/tts/cognitiveservices/v1` using your resource name. If omitted, the app uses the regional Speech endpoint. From the project directory, load both the project settings and the host secrets whenever recreating the container:

```sh
docker compose --env-file .env --env-file /etc/brainstorming/secrets.env up -d --build
```

Refresh the app and choose Phoebe Dragon HD, Jenny, or Guy under **Settings → Read aloud**. Phoebe requires a Speech resource in a region that supports HD voices. Preview uses a fixed sample. Reading a note sends each spoken passage to Microsoft.

## Google Read Aloud

In a billing-enabled Google Cloud project, enable the Cloud Text-to-Speech API. On the Ubuntu host, use a dedicated Google credential JSON file outside the repository that is readable by the app's container user. This can be an Application Default Credentials file with a quota project (the caller needs Service Usage Consumer on that project), or a dedicated service account credential. Keep the credential private and outside the Drive mount.

Add the absolute file path to the project's existing `.env` and include the speech overlay alongside the Drive overlay:

```dotenv
GOOGLE_TTS_CREDENTIALS_FILE=/absolute/path/to/speech-credentials.json
COMPOSE_FILE=compose.yaml:compose.drive.yaml:compose.speech.yaml
```

Run `docker compose up -d --build`. In Settings → Read aloud, select **Google Cloud · en-US-Chirp-HD-F** and tap **Preview voice**. Each browser remembers its choice. Read aloud sends one short passage at a time to Google through the signed-in server. Google playback supports pause, resume, previous and next passage. Stopping cursor-based reading estimates the current word from elapsed audio time.

### Reconnect or move a browser session

After upgrading, open the app once in each signed-in browser profile to register its device credential. On the next sign-in, enter a fresh one-use browser token and confirm the existing connection name. Reconnecting rotates the login and device credentials while retaining the session ID, access matrix and preferences. Signing out expires the login. Disconnecting a session in Settings revokes it.

To move a connection to a new browser, recover after clearing site data, or explicitly restore a revoked browser session, run these on the Docker host:

```sh
docker compose exec brainstorming python server.py session list
docker compose exec brainstorming python server.py token create --replace-session SESSION_ID
```

Enter the resulting token in the destination browser and confirm the name. In the sidebar, right-click or hold a project to pin or unpin it for this connection. Replacing a visible pin requires confirmation.

### AI image attachments

Refresh the MCP tool list after deployment. Use `attach_image` with a note ID, expected revision, and a ChatGPT file or base64 image bytes. An optional zero-based Unicode character offset specifies where to insert the marker. The default is the end of the note. Supported still images are PNG, JPEG and WebP, up to 8 MiB and 16 megapixels. `read_image` requires the note ID and its linked attachment ID. Both tools enforce the current note matrix. Successful uploads create an attributed audit event. Image bytes live in the existing mounted Drive picture folder. Restore that folder alongside the SQLite snapshot to preserve previews.
