"""OAuth for the private ChatGPT integration. A grant always belongs to an existing AI session."""
import base64
import hashlib
import html
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

TITLE = "Geeky Jimmy’s Brainstorming Tool"
ISSUER = os.environ.get("MCP_PUBLIC_ORIGIN", "https://notes.accountabilitybot.cc").rstrip("/")
if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", ISSUER):
    raise ValueError("MCP_PUBLIC_ORIGIN must be a public HTTPS origin")
RESOURCE = ISSUER + "/mcp"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE = "notes"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, method TEXT NOT NULL, redirect TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_grants (
    digest TEXT PRIMARY KEY, kind TEXT NOT NULL, client TEXT NOT NULL REFERENCES oauth_clients(id),
    session TEXT NOT NULL REFERENCES web_sessions(id) ON DELETE CASCADE,
    challenge TEXT NOT NULL, scope TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_grant_expiry ON oauth_grants(expires);
"""

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fields(raw):
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=32)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError("Duplicate parameter")
    return {k: v[0] for k, v in values.items()}


def metadata(path):
    if path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        return dict(resource=RESOURCE, resource_name=TITLE, authorization_servers=[ISSUER],
                    scopes_supported=[SCOPE], bearer_methods_supported=["header"])
    if path == "/.well-known/oauth-authorization-server":
        return dict(issuer=ISSUER, authorization_endpoint=ISSUER + "/oauth/authorize",
                    token_endpoint=ISSUER + "/oauth/token", registration_endpoint=ISSUER + "/oauth/register",
                    authorization_response_iss_parameter_supported=True,
                    response_types_supported=["code"], grant_types_supported=["authorization_code", "refresh_token"],
                    token_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post", "none"],
                    code_challenge_methods_supported=["S256"], scopes_supported=[SCOPE, "offline_access"])


def register(handler, db):
    data = handler.body_json()
    method = data.get("token_endpoint_auth_method", "client_secret_basic")
    redirects = data.get("redirect_uris")
    if not isinstance(redirects, list) or len(redirects) != 1 or not isinstance(redirects[0], str):
        handler.send_json(400, {"error": "invalid_redirect_uri"})
        return
    redirect = redirects[0]
    if redirect != CALLBACK and not re.fullmatch(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+", redirect):
        handler.send_json(400, {"error": "invalid_redirect_uri"})
        return
    if method not in ("client_secret_basic", "client_secret_post", "none"):
        handler.send_json(400, {"error": "invalid_client_metadata"})
        return
    ident = secrets.token_urlsafe(24)
    reply = dict(client_id=ident, redirect_uris=[redirect], token_endpoint_auth_method=method,
                 grant_types=["authorization_code", "refresh_token"], response_types=["code"])
    secret = ""
    if method != "none":
        secret = secrets.token_urlsafe(40)
        reply.update(client_secret=secret, client_secret_expires_at=0)
    db.execute("INSERT INTO oauth_clients VALUES(?,?,?,?)", (ident, digest(secret), method, redirect))
    db.commit()
    handler.send_json(201, reply)


def validate_request(db, p):
    if not db.execute("SELECT 1 FROM oauth_clients WHERE id=? AND redirect=?", (p.get("client_id"), p.get("redirect_uri"))).fetchone():
        raise ValueError("Unknown client")
    if (p.get("response_type") != "code"
            or p.get("resource", RESOURCE) != RESOURCE or p.get("code_challenge_method") != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", p.get("code_challenge", ""))):
        raise ValueError("Invalid authorization request")
    scopes = set(p.get("scope", SCOPE).split())
    if not scopes <= {SCOPE, "offline_access"} or SCOPE not in scopes:
        raise ValueError("Invalid scope")


def grant(db, kind, client, session, scope, seconds, challenge=""):
    raw = secrets.token_urlsafe(40)
    db.execute("INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,?)",
               (digest(raw), kind, client, session, challenge, scope, int(time.time()) + seconds))
    return raw


def authorize(handler, db, p, posted, stamp):
    validate_request(db, p)
    if not posted:
        inputs = "".join(f'<input type="hidden" name="{html.escape(k, quote=True)}" value="{html.escape(v, quote=True)}">'
                         for k, v in p.items() if k in {"client_id", "redirect_uri", "response_type", "resource",
                                                        "scope", "state", "code_challenge_method", "code_challenge"})
        raw = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Connect ChatGPT · {TITLE}</title><main><h1>Connect ChatGPT to {TITLE}</h1>
<p>Enter the existing AI token for the connection you want ChatGPT to use.
Its note permissions apply. ChatGPT can also create projects and notes.</p>
<form method="post" action="/oauth/authorize">{inputs}
<label>Brainstorming AI token <input type="password" name="ai_token" required autocomplete="off"></label>
<button>Authorize ChatGPT</button></form></main>""".encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        # An HTML form POST under no-referrer can send Origin: null.
        handler.add_security_headers(referrer_policy="same-origin")
        handler.end_headers()
        handler.wfile.write(raw)
        return
    if handler.headers.get("Origin") != ISSUER:
        handler.send_json(403, {"error": "Authorization form origin denied. Reopen the connection from ChatGPT and try again."})
        return
    session = db.execute("SELECT id FROM web_sessions WHERE token_hash=? AND is_ai=1 AND revoked_at IS NULL AND expires_at>?",
                         (digest(p.get("ai_token", "").strip()), stamp())).fetchone()
    if not session:
        handler.send_json(403, {"error": "Use an active Brainstorming AI token. Go back to try again."})
        return
    db.execute("DELETE FROM oauth_grants WHERE expires<=?", (int(time.time()),))
    code = grant(db, "code", p["client_id"], session["id"], p.get("scope", SCOPE), 300, p["code_challenge"])
    db.commit()
    result = dict(code=code, iss=ISSUER)
    if "state" in p:
        result["state"] = p["state"]
    handler.send_response(303)
    handler.send_header("Location", p["redirect_uri"] + "?" + urlencode(result))
    handler.send_header("Cache-Control", "no-store")
    handler.add_security_headers()
    handler.end_headers()


def exchange(handler, db, p, stamp):
    ident, secret = p.get("client_id", ""), p.get("client_secret", "")
    method = "client_secret_post" if "client_secret" in p else "none"
    auth = handler.headers.get("Authorization")
    if auth:
        if not auth.startswith("Basic ") or "client_secret" in p:
            raise ValueError("Invalid client authentication")
        ident, secret = base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
        method = "client_secret_basic"
    client = db.execute("SELECT * FROM oauth_clients WHERE id=?", (ident,)).fetchone()
    if not client or client["method"] != method or not secrets.compare_digest(client["secret_hash"], digest(secret)):
        handler.send_json(401, {"error": "invalid_client"})
        return
    kind = p.get("grant_type")
    if kind not in ("authorization_code", "refresh_token"):
        handler.send_json(400, {"error": "unsupported_grant_type"})
        return
    code = kind == "authorization_code"
    key = digest(p.get("code" if code else "refresh_token", ""))
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("""SELECT g.* FROM oauth_grants g JOIN web_sessions s ON s.id=g.session
        WHERE g.digest=? AND g.kind=? AND g.client=? AND g.expires>? AND s.is_ai=1
        AND s.revoked_at IS NULL AND s.expires_at>?""", (key, "code" if code else "refresh", ident, int(time.time()), stamp())).fetchone()
    valid = row and p.get("resource", RESOURCE) == RESOURCE
    if valid and code:
        verifier = p.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        valid = (p.get("redirect_uri") == client["redirect"] and re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                 and secrets.compare_digest(challenge, row["challenge"]))
    if not valid:
        handler.send_json(400, {"error": "invalid_grant"})
        return
    db.execute("DELETE FROM oauth_grants WHERE digest=? OR expires<=?", (key, int(time.time())))
    scope = row["scope"]
    access = grant(db, "access", ident, row["session"], scope, 3600)
    refresh = grant(db, "refresh", ident, row["session"], scope, 30 * 86400)
    db.commit()
    handler.send_json(200, dict(access_token=access, refresh_token=refresh, token_type="Bearer", expires_in=3600, scope=scope))


def handle(handler, connect, stamp):
    url = urlsplit(handler.path)
    posted = handler.command == "POST"
    info = metadata(url.path)
    if info and not posted:
        handler.send_json(200, info)
        return True
    if url.path not in ("/oauth/authorize", "/oauth/token", "/oauth/register"):
        return False
    try:
        with connect() as db:
            if posted and url.path == "/oauth/register":
                register(handler, db)
            elif posted:
                size = int(handler.headers.get("Content-Length", "0"))
                if handler.headers.get_content_type() != "application/x-www-form-urlencoded" or not 0 < size <= 16384:
                    raise ValueError("Invalid form")
                p = fields(handler.rfile.read(size).decode())
                if url.path == "/oauth/token":
                    exchange(handler, db, p, stamp)
                else:
                    authorize(handler, db, p, True, stamp)
            elif url.path == "/oauth/authorize":
                authorize(handler, db, fields(url.query), False, stamp)
            else:
                handler.send_json(405, {"error": "POST required"})
    except ValueError:
        handler.send_json(400, {"error": "invalid_request"})
    return True
