"""Stateless MCP note tools. Authority always comes from the authenticated session."""
import json
import secrets

import pictures
from mcp_oauth import SCOPE, TITLE

ACTIVE = "s.id=? AND s.is_ai=1 AND s.revoked_at IS NULL AND s.expires_at>?"
NOTES = f"""FROM notes n JOIN projects p ON p.id=n.project_id
    JOIN session_note_access a ON a.note_id=n.id AND a.allowed=1
    JOIN web_sessions s ON s.id=a.session_id
    WHERE {ACTIVE} AND n.status='active' AND p.archived=0"""
PROJECTS = f"""FROM projects p JOIN web_sessions s ON {ACTIVE}
    WHERE p.archived=0 AND ((p.ai_creator=s.id AND NOT EXISTS
       (SELECT 1 FROM notes n WHERE n.project_id=p.id))
    OR EXISTS (SELECT 1 FROM notes n JOIN session_note_access a ON a.note_id=n.id
       WHERE n.project_id=p.id AND n.status='active' AND a.session_id=s.id AND a.allowed=1))"""

FIELD_TYPES = {"file": "object", "offset": "integer", "expected_revision": "integer", "line_numbers": "boolean"}


def tool(name, description, fields=(), required=()):
    schemes = [{"type": "oauth2", "scopes": [SCOPE]}]
    return dict(name=name, description=description, securitySchemes=schemes,
                _meta={"securitySchemes": schemes}, inputSchema={
        "type": "object", "additionalProperties": False, "required": list(required),
        "properties": {k: {"type": FIELD_TYPES.get(k, "string")} for k in fields}})

TOOLS = {entry["name"]: entry for entry in [
    tool("list_projects", "List accessible projects, including empty projects you created."),
    tool("list_notes", "List accessible note titles, optionally searching titles and bodies by text.",
         ("query", "project_id")),
    tool("attach_image", "Insert a still PNG, JPEG or WebP into a permitted note. Supply a ChatGPT file or base64 image bytes (up to 8 MiB decoded). Offset counts Unicode characters; omit to append.",
         ("note_id", "expected_revision", "data_base64", "file", "offset"), ("note_id", "expected_revision")),
    tool("read_image", "View an image linked to a permitted note.", ("note_id", "attachment_id"), ("note_id", "attachment_id")),
    tool("read_note", "Read one permitted note and its current revision.", ("note_id",), ("note_id",)),
    tool("set_line_numbers", "Show or hide line numbers on one permitted note.",
         ("note_id", "line_numbers", "expected_revision"), ("note_id", "line_numbers", "expected_revision")),
    tool("update_note", "Replace title and body using the revision returned by read_note.",
         ("note_id", "title", "body", "expected_revision"), ("note_id", "title", "body", "expected_revision")),
    tool("create_project", "Create an empty project.", ("title",), ("title",)),
    tool("create_note", "Create a note in an accessible project. Supply an accessible parent_id for a subnote.",
         ("project_id", "parent_id", "title", "body"), ("project_id", "title")),
]}


TOOLS["attach_image"]["_meta"]["openai/fileParams"] = ["file"]
TOOLS["attach_image"]["inputSchema"]["properties"]["file"] = {
    "type": "object", "properties": {k: {"type": "string"} for k in ("download_url", "file_id", "mime_type", "file_name")},
    "required": ["download_url", "file_id"]
}


def call(name, args, session, connect, stamp, clean_title, create_note, save_note):
    schema = TOOLS[name]["inputSchema"]
    fields = schema["properties"]
    if not isinstance(args, dict) or args.keys() - fields.keys() or set(schema["required"]) - args.keys():
        raise ValueError("Invalid tool arguments")
    for key, value in args.items():
        if type(value) is not {"integer": int, "string": str, "boolean": bool, "object": dict}[fields[key]["type"]]:
            raise ValueError(f"Invalid {key}")
    if name in ("attach_image", "read_image"):
        return pictures.ai_tool(name, args, session, connect, stamp, NOTES)
    if name == "create_note":
        row = create_note(args["project_id"], args.get("parent_id"), args["title"], None, session,
                          args.get("body", ""), ai=True)
        return {k: row[k] for k in ("id", "project_id", "title", "revision")}
    if name == "set_line_numbers":
        with connect() as db:
            before = db.execute("SELECT n.title,n.body,n.revision " + NOTES + " AND n.id=?",
                                (session, stamp(), args["note_id"])).fetchone()
        if not before:
            raise LookupError("Note not found")
        if before["revision"] != args["expected_revision"]:
            raise ValueError("Revision conflict. Read the note again before editing.")
        row = save_note(args["note_id"], before["title"], before["body"], None, args["expected_revision"],
                        line_numbers=args["line_numbers"], ai_session=session)
        return {"id": row["id"], "revision": row["revision"], "line_numbers": bool(row["line_numbers"])}
    if name == "update_note":
        row = save_note(args["note_id"], args["title"], args["body"], None, args["expected_revision"], ai_session=session)
        return {k: row[k] for k in ("id", "title", "revision")}
    with connect() as db:
        now = stamp()
        params = (session, now)
        if name == "create_project":
            project_id = secrets.token_hex(16)
            title = clean_title(args["title"])
            added = db.execute("INSERT INTO projects(id,title,created_at,updated_at,ai_creator) "
                               f"SELECT ?,?,?,?,s.id FROM web_sessions s WHERE {ACTIVE}",
                               (project_id,title,now,now,*params))
            if not added.rowcount:
                raise LookupError("Session unavailable")
            return {"id": project_id, "title": title}
        if name == "list_projects":
            return [dict(r) for r in db.execute("SELECT p.id,p.title " + PROJECTS + " ORDER BY p.title,p.id", params)]
        if name == "read_note":
            row = db.execute("SELECT n.id,n.project_id,n.title,n.body,n.revision,n.line_numbers " + NOTES + " AND n.id=?",
                             (*params, args["note_id"])).fetchone()
            if not row:
                raise LookupError("Note not found")
            return {**dict(row), "line_numbers": bool(row["line_numbers"])}
        query = args.get("query", "")
        project = args.get("project_id", "")
        rows = db.execute("SELECT n.id,n.project_id,n.title,n.revision " + NOTES +
                          " AND (?='' OR n.project_id=?) AND (instr(lower(n.title),lower(?)) OR instr(lower(n.body),lower(?)))"
                          " ORDER BY n.title,n.id", (*params, project, project, query, query))
        return [dict(r) for r in rows]


def dispatch(message, invoke):
    ident = message.get("id")
    reply = {"jsonrpc": "2.0", "id": ident}
    method = message.get("method")
    params = message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        return dict(reply, error={"code": -32600, "message": "Invalid request"})
    if "id" not in message:
        return None
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "brainstorming", "title": TITLE, "version": "1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list(TOOLS.values())}
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return dict(reply, error={"code": -32602, "message": "Unknown tool"})
        try:
            data = invoke(name, params.get("arguments", {}))
            result = {"content": [data if name == "read_image" else {"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        except (LookupError, ValueError) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        return dict(reply, error={"code": -32601, "message": "Method not found"})
    return dict(reply, result=result)
