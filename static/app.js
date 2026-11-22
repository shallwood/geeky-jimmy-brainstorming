let EditorView, ViewPlugin, Decoration, WidgetType, basicSetup, markdown, undo, redo;
try {
  const modules = await Promise.all([
    import("https://esm.sh/codemirror@6.0.1"),
    import("https://esm.sh/@codemirror/lang-markdown@6.3.1"),
    import("https://esm.sh/@codemirror/commands@6.6.0"),
    import("https://esm.sh/@codemirror/view@6")
  ]);
  ({EditorView,basicSetup}=modules[0]); ({markdown}=modules[1]); ({undo,redo}=modules[2]);
  ({Decoration,ViewPlugin,WidgetType}=modules[3]);
} catch { /* Use the native textarea until the maintained editor bundle is available. */ }

const $ = (selector) => document.querySelector(selector);
const savedTheme = localStorage.getItem("gj-theme");
if (!localStorage.getItem("gj-theme-palette-version")) {
  localStorage.setItem("gj-theme", savedTheme === "light" ? "light" : "dark");
  localStorage.setItem("gj-theme-palette-version", "terminal-green-v1");
}
const state = { projects: [], selectedProject: null, selectedNote: null, note: null, editor: null, saveTimer: null, theme: localStorage.getItem("gj-theme") || "dark", dialogKind: "note", dialogParent: null, busy: false, canManagePermissions: false };
const AUTH_KEY = "gj-session";

const picturePattern = /!\[Picture\]\(picture:([a-f0-9]{32})\)/g;
function pictureButton(id) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "picture-marker";
  button.textContent = "▧";
  button.setAttribute("aria-label", "Open picture");
  button.title = "Open picture";
  button.onclick = () => openPicture(id);
  return button;
}

const PictureWidget = WidgetType && class extends WidgetType {
  constructor(id) { super(); this.id = id; }
  eq(other) { return this.id === other.id; }
  toDOM() { return pictureButton(this.id); }
};

function fallbackPictures(text) {
  $("#fallbackPictures").replaceChildren(...Array.from(text.matchAll(picturePattern),
    match => pictureButton(match[1])));
}

async function openPicture(id) {
  const dialog = $("#pictureDialog");
  const img = dialog.querySelector("img");
  const status = dialog.querySelector("p");
  const request = new AbortController();
  status.textContent = "Loading picture…";
  img.hidden = true;
  dialog.addEventListener("close", () => {
    request.abort();
    URL.revokeObjectURL(img.src);
    img.removeAttribute("src");
  }, { once: true });
  dialog.showModal();
  try {
    const response = await fetch('/api/notes/' + state.note.id + '/pictures/' + id, {
      headers: { Authorization: "Bearer " + token() }, signal: request.signal
    });
    if (!response.ok) throw new Error("Picture unavailable. Check note access and the Drive connection.");
    const blob = await response.blob();
    if (request.signal.aborted) return;
    img.src = URL.createObjectURL(blob);
    await img.decode();
    if (!request.signal.aborted) { img.hidden = false; status.textContent = ""; }
  } catch (error) {
    if (!request.signal.aborted) status.textContent = error.message;
  }
}

async function scaledPicture(file) {
  const image = new Image();
  const url = URL.createObjectURL(file);
  try {
    image.src = url;
    await image.decode();
    const scale = Math.min(1, 2048 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/webp", 0.82));
    if (!blob || blob.size > 8 * 1024 * 1024) throw new Error("Please choose a smaller picture.");
    return blob;
  } finally { URL.revokeObjectURL(url); }
}

async function insertPicture(file, pos) {
  const noteId = state.note?.id;
  if (!file || !noteId || $("#pictureUpload").open) return;
  const field = $("#editorFallback");
  const view = state.editor;
  const from = pos ?? view?.state.selection.main.from ?? field.selectionStart;
  const to = pos ?? view?.state.selection.main.to ?? field.selectionEnd;
  $("#pictureUpload").showModal();
  try {
    const blob = await scaledPicture(file);
    const result = await api('/api/notes/' + noteId + '/pictures', {
      method: "POST", headers: { "Content-Type": blob.type }, body: blob
    });
    if (noteId !== state.note?.id) return;
    const insert = "![Picture](picture:" + result.id + ")";
    if (view) view.dispatch({changes: {from, to, insert}, selection: {anchor: from + insert.length}});
    else {
      field.setRangeText(insert, from, to, "end");
      field.dispatchEvent(new Event("input"));
    }
  } catch (error) { toast(error.message); }
  finally {
    $("#pictureUpload").close();
    if (state.note?.id === noteId) (view || field).focus();
  }
}
$("#pictureUpload").oncancel = event => event.preventDefault();
$("#pictureFile").onchange = event => {
  const file = event.target.files[0];
  event.target.value = "";
  insertPicture(file);
};
const editArea = $(".editor-wrap");
editArea.addEventListener("paste", event => {
  const file = Array.from(event.clipboardData.files).find(file => file.type.startsWith("image/"));
  if (file) { event.preventDefault(); event.stopPropagation(); insertPicture(file); }
}, true);
editArea.addEventListener("dragover", event => {
  if (Array.from(event.dataTransfer.items).some(item => item.type.startsWith("image/"))) event.preventDefault();
});
editArea.addEventListener("drop", event => {
  const file = Array.from(event.dataTransfer.files).find(file => file.type.startsWith("image/"));
  if (!file) return;
  event.preventDefault();
  event.stopPropagation();
  insertPicture(file, state.editor?.posAtCoords({x: event.clientX, y: event.clientY}));
}, true);

function markdownEditingDecorations(view) {
  const editorState = view.state;
  const decorations = [];
  const addFormattedRange = (line, start, end, markerLength, closingLength, strike = false) => {
    const contentFrom = start + markerLength;
    const contentTo = end - closingLength;
    if (contentTo <= contentFrom) return;
    const from = line.from + start;
    const to = line.from + end;
    const cursorInText = view.hasFocus && editorState.selection.ranges.some(range =>
      range.empty ? range.head >= from && range.head <= to : range.from < to && range.to > from
    );
    if (!cursorInText) {
      decorations.push(Decoration.replace({}).range(line.from + start, line.from + contentFrom));
      decorations.push(Decoration.replace({}).range(line.from + contentTo, line.from + end));
    }
    if (strike) decorations.push(Decoration.mark({class: "cm-markdown-strike"}).range(line.from + contentFrom, line.from + contentTo));
  };

  for (let number = 1; number <= editorState.doc.lines; number++) {
    const line = editorState.doc.line(number);
    for (const match of line.text.matchAll(picturePattern)) {
      const from = line.from + match.index;
      decorations.push(Decoration.replace({widget: new PictureWidget(match[1])}).range(from, from + match[0].length));
    }
    const paired = /(\*\*|~~|==|__|`|\*)(.+?)\1/g;
    let match;
    while ((match = paired.exec(line.text))) {
      const from = line.from + match.index + match[1].length;
      const to = line.from + match.index + match[0].length - match[1].length;
      addFormattedRange(line, match.index, match.index + match[0].length, match[1].length, match[1].length, match[1] === "~~");
      if (match[1] === "__") decorations.push(Decoration.mark({class: "cm-markdown-underline"}).range(from, to));
    }
    const underline = /<u>(.+?)<\/u>/gi;
    while ((match = underline.exec(line.text))) {
      const from = line.from + match.index + 3;
      const to = line.from + match.index + match[0].length - 4;
      addFormattedRange(line, match.index, match.index + match[0].length, 3, 4);
      decorations.push(Decoration.mark({class: "cm-markdown-underline"}).range(from, to));
    }
  }
  return Decoration.set(decorations, true);
}

const markdownPreview = ViewPlugin?.fromClass(class {
  constructor(view) {
    this.decorations = markdownEditingDecorations(view);
  }
  update(update) {
    if (update.docChanged || update.selectionSet || update.focusChanged) {
      this.decorations = markdownEditingDecorations(update.view);
    }
  }
}, { decorations: plugin => plugin.decorations, provide: plugin => EditorView.atomicRanges.of(view => view.plugin(plugin).decorations.update({
  filter: (_from, _to, decoration) => decoration.spec.widget instanceof PictureWidget
})) });

function setStatus(message, kind = "ready") {
  const node = $("#syncStatus"); node.className = `sync-status ${kind}`; node.lastElementChild.textContent = message;
  $("#saveMessage").textContent = kind === "offline" ? "Saved on this device · waiting for server" : kind === "error" ? "Could not sync · your local draft is safe" : "Changes save as you type";
}
function token() { return localStorage.getItem(AUTH_KEY) || ""; }
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if(response.status===401&&path!=="/api/login"&&path!=="/api/session"){finishReading();localStorage.removeItem(AUTH_KEY);localStorage.removeItem("gj-session-id");state.canManagePermissions=false;$("#authScreen").hidden=false;$("#app").hidden=true;$("#tokenForm").hidden=false;$("#tokenHelp").hidden=false;$("#authIntro").textContent="This device connection has ended. Generate a fresh one-use token in the CLI to reconnect.";}
    const error = new Error(payload.error || `Request failed (${response.status})`); error.status=response.status; throw error;
  }
  return payload;
}
function showApp(authenticated) {
  finishReading();

  if (!authenticated) state.canManagePermissions = false;
  $("#authScreen").hidden = authenticated; $("#app").hidden = !authenticated;
  $("#newProjectButton").hidden = !state.canManagePermissions;
  $("#sidebarProjectButton").hidden = !state.canManagePermissions;
  $("#welcomeProjectButton").hidden = !state.canManagePermissions;
  if (authenticated) {
    applyTheme(state.theme);
    const bookmark = readBookmark();
    const reopen = bookmark?.id || sessionStorage.getItem(refreshNoteKey);
    sessionStorage.removeItem(refreshNoteKey);
    loadProjects().then(() => { if (reopen && !state.selectedNote) selectNote(reopen, bookmark || {id: reopen}); });
  }
}
function escapeHTML(value) { return String(value).replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch])); }
function applyTheme(theme) {
  state.theme = theme; localStorage.setItem("gj-theme", theme);
  const actual = theme === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : theme;
  document.documentElement.dataset.theme = actual;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", actual === "dark" ? "#000000" : "#ffffff");
  document.querySelectorAll("[data-theme]").forEach(button => button.classList.toggle("selected", button.dataset.theme === theme));
}
function flatten(project) { return project.notes || []; }
function depthOf(note, map) { let depth = 1, p = note; while (p.parent_id) { p = map.get(p.parent_id); if (!p) break; depth++; } return depth; }
function appendTreeItem(group, button, item, kind) {
  let timer;
  let origin;
  let held = false;
  const cancelHold = () => { clearTimeout(timer); origin = null; };
  const showMenu = () => {
    cancelHold();
    held = true;
    if (!$("#organizer").open) openOrganizer(item, kind);
  };
  button.addEventListener("contextmenu", event => {
    event.preventDefault();
    showMenu();
  });
  button.addEventListener("pointerdown", event => {
    cancelHold();
    held = false;
    if (event.pointerType === "mouse" || !event.isPrimary || event.target.closest(".tree-add")) return;
    origin = {x:event.clientX, y:event.clientY};
    timer = setTimeout(showMenu, 500);
  });
  button.addEventListener("pointermove", event => {
    if (origin && Math.hypot(event.clientX-origin.x, event.clientY-origin.y) > 10) cancelHold();
  });
  for (const name of ["pointerup","pointercancel","pointerleave"]) button.addEventListener(name, cancelHold);
  button.addEventListener("click", event => {
    if (held) { held = false; event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  const row = document.createElement("div");
  row.className = "tree-action-row";
  const menu = document.createElement("button");
  menu.className = "icon-button small";
  menu.textContent = "⋯";
  menu.setAttribute("aria-label", "Manage " + item.title);
  menu.onclick = () => openOrganizer(item, kind);
  row.append(button, menu);
  group.append(row);
}

async function organize(item, kind, action, extra = {}) {
  if (state.busy || (state.note && $("#noteSaved").dataset.unsaved === "true")) {
    throw new Error("Wait for your note to sync before changing the tree.");
  }
  await api("/api/organize", {method:"POST", body:JSON.stringify({id:item.id,kind,action,...extra})});
  state.projects = await api("/api/projects");
  renderTree();
  $("#searchResults").hidden = true;
  if (state.note) {
    const [project, listed] = projectForNote(state.note.id);
    if (!project) {
      clearCurrentNote(state.note.id);
      if ($("#settingsPane").hidden) $("#welcome").hidden = false;
    } else if (!$("#editorPane").hidden) {
      if (kind === "note" && item.id === state.note.id && action === "rename") {
        await selectNote(item.id);
      } else {
        state.note.project_id = project.id;
        state.note.parent_id = listed.parent_id;
        state.selectedProject = project.id;
        renderBreadcrumbs();
      }
    }
  }
}

function openOrganizer(item, kind) {
  const dialog = $("#organizer");
  $("#organizerTitle").textContent = item.title;
  $("#organizerName").value = item.title;
  $("#organizerError").textContent = "";
  $("#moveFields").hidden = kind !== "note";
  const destination = $("#moveDestination");
  destination.replaceChildren();
  if (kind === "note") {
    for (const project of state.projects) {
      const group = document.createElement("optgroup");
      group.label = project.title;
      group.append(new Option(project.title + " (top level)", project.id));
      const map = new Map(project.notes.map(note => [note.id,note]));
      for (const note of project.notes) {
        const path = [];
        let ancestor = note;
        while (ancestor && ancestor.id !== item.id) {
          path.unshift(ancestor.title);
          ancestor = map.get(ancestor.parent_id);
        }
        if (!ancestor && path.length < 3) group.append(new Option(path.join(" › "), project.id + "/" + note.id));
      }
      destination.append(group);
    }
    destination.value = item.project_id + (item.parent_id ? "/" + item.parent_id : "");
  }
  async function apply(action, extra) {
    try {
      await organize(item,kind,action,extra);
      dialog.close();
    } catch (error) { $("#organizerError").textContent = error.message; }
  }
  $("#renameItem").onclick = () => apply("rename",{title:$("#organizerName").value});
  $("#archiveItem").onclick = () => {
    if (confirm("Archive “" + item.title + "” and its notes? You can restore them in Settings.")) apply("archive");
  };
  $("#moveItem").onclick = () => {
    const [project_id,parent_id] = destination.value.split("/");
    apply("move",{project_id,parent_id});
  };
  $("#closeOrganizer").onclick = () => dialog.close();
  dialog.showModal();
}

async function loadArchive() {
  const host = $("#archiveList");
  try {
    const items = await api("/api/archive");
    host.replaceChildren();
    if (!items.length) host.textContent = "No archived items.";
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "archive-row";
      const label = document.createElement("span");
      label.textContent = item.title + " (" + item.kind + ")";
      row.append(label);
      for (const action of ["restore","delete"]) {
        const button = document.createElement("button");
        button.className = "secondary";
        button.textContent = action === "restore" ? "Restore" : "Delete permanently";
        button.disabled = action === "restore" && !item.restorable;
        if (button.disabled) label.textContent += " · Restore its parent first";
        button.onclick = async () => {
          if (action === "delete" && !confirm("Permanently delete “" + item.title + "” and all its descendants? This cannot be undone.")) return;
          try {
            await organize(item,item.kind,action);
            await loadArchive();
            if (state.canManagePermissions) await loadAccessMatrix();
          } catch (error) { toast(error.message); }
        };
        row.append(button);
      }
      host.append(row);
    }
  } catch (error) { host.textContent = error.message; }
}
function renderTree() {
  const host = $("#projectTree"); host.replaceChildren();
  for (const project of state.projects) {
    const group = document.createElement("section"); group.className = "project-group";
    const phead = document.createElement("button"); phead.className = `tree-project ${state.selectedProject === project.id ? "active" : ""}`;
    phead.innerHTML = `<span class="chevron">${project.collapsed ? "›" : "⌄"}</span><span class="folder-icon">▰</span><span class="tree-label">${escapeHTML(project.title)}</span><span class="tree-add" title="Add a note">＋</span>`;
    phead.addEventListener("click", e => { if (e.target.closest(".tree-add")) { state.selectedProject = project.id; openTitleDialog("note",null); } else { project.collapsed = !project.collapsed; state.selectedProject = project.id; renderTree(); if (state.note && state.note.project_id !== project.id) closeEditor(); } });
    appendTreeItem(group, phead, project, "project");
    if (!project.collapsed) {
      const notes = flatten(project), map = new Map(notes.map(note => [note.id, note]));
      const children = new Map(); notes.forEach(note => { const parent = note.parent_id && map.has(note.parent_id) ? note.parent_id : "root"; if (!children.has(parent)) children.set(parent, []); children.get(parent).push(note); });
      const build = (parent, depth = 1) => {
        for (const note of children.get(parent) || []) {
          const row = document.createElement("button"); row.className = `tree-note depth-${depth} ${state.selectedNote === note.id ? "active" : ""}`; row.style.setProperty("--depth", depth);
          row.innerHTML = `<span class="note-glyph">${depth > 1 ? "↳" : "▤"}</span><span class="tree-label">${escapeHTML(note.title || "Untitled note")}</span>`;
          row.addEventListener("click", () => selectNote(note.id)); appendTreeItem(group, row, note, "note"); if (depth < 3) build(note.id, depth + 1);
        }
      };
      build("root");
    }
    host.append(group);
  }
  try { localStorage.setItem("gj-project-tree",JSON.stringify(state.projects.map(p=>({...p,notes:(p.notes||[]).map(({body,...note})=>note)})))); } catch {}
}
async function loadProjects() {
  try { state.projects = await api("/api/projects"); localStorage.setItem("gj-project-tree",JSON.stringify(state.projects.map(p=>({...p,notes:(p.notes||[]).map(({body,...note})=>note)})))); renderTree(); setStatus("Synced"); }
  catch (error) { try { state.projects=JSON.parse(localStorage.getItem("gj-project-tree")||"[]"); renderTree(); } catch {} setStatus(navigator.onLine ? "Sync issue" : "Offline", navigator.onLine ? "error" : "offline"); if(!state.projects.length)toast(error.message); }
}
const scrollArea = $(".editor-wrap");
const pullStatus = $("#pullStatus");
const refreshNoteKey = "gj-pull-refresh-note";
let refreshing = false;
function installPull(area, refresh) {
  let pullStart = null;
  let pullReady = false;
  function resetPull() {
    pullStart = null;
    pullReady = false;
    if (!refreshing) pullStatus.hidden = true;
  }
  area.addEventListener("touchstart", event => {
    resetPull();
    if (area.scrollTop > 0 || event.touches.length !== 1 || refreshing || (area === scrollArea && reading)) return;
    const selection = state.editor?.state.selection.main;
    const field = $("#editorFallback");
    if (area === scrollArea && (selection ? !selection.empty : field.selectionStart !== field.selectionEnd)) return;
    pullStart = event.touches[0];
  }, {passive:true});
  area.addEventListener("touchmove", event => {
    if (!pullStart) return;
    const finger = event.touches[0];
    const distance = finger.clientY - pullStart.clientY;
    if (event.touches.length !== 1 || distance < -5 ||
        Math.abs(finger.clientX - pullStart.clientX) > Math.max(12, distance)) return resetPull();
    if (distance < 10 && pullStatus.hidden) return;
    if (event.cancelable) event.preventDefault();
    pullReady = distance >= 75;
    pullStatus.hidden = false;
    pullStatus.style.setProperty("--pull-angle", Math.min(360, distance * 4) + "deg");
    pullStatus.querySelector("b").textContent = pullReady ? "Release to refresh" : "Pull to refresh";
  }, {passive:false});
  area.addEventListener("touchcancel", resetPull);
  area.addEventListener("touchend", () => {
    const ready = pullReady;
    resetPull();
    if (ready) refresh();
  });
}
function showRefreshing() {
  refreshing = true;
  pullStatus.querySelector("b").textContent = "Refreshing…";
  pullStatus.classList.add("refreshing");
  pullStatus.hidden = false;
}
async function refreshProjects() {
  if (refreshing) return;
  showRefreshing();
  try {
    const projects = await api("/api/projects");
    const collapsed = new Set(state.projects.filter(item => item.collapsed).map(item => item.id));
    for (const project of projects) project.collapsed = collapsed.has(project.id);
    state.projects = projects;
    renderTree();
    toast("Projects and notes refreshed.");
  } catch (error) {
    toast(error.message);
  } finally {
    refreshing = false;
    pullStatus.classList.remove("refreshing");
    pullStatus.hidden = true;
  }
}
$("#refreshProjects").onclick = refreshProjects;
installPull($("#projectTree"), refreshProjects);
installPull(scrollArea, () => {
  if (!state.note || refreshing) return;
  if (state.busy || $("#noteSaved").dataset.unsaved === "true") {
    toast("Refresh after your changes have synced.");
    return;
  }
  const noteId = state.note.id;
  showRefreshing();
  sessionStorage.setItem(refreshNoteKey, noteId);
  requestAnimationFrame(() => requestAnimationFrame(() => location.reload()));
});
function bookmarkKey() {
  const session = localStorage.getItem("gj-session-id");
  return session ? "gj-editor-position:" + session : null;
}
function readBookmark() {
  try { return JSON.parse(localStorage.getItem(bookmarkKey()) || "null"); }
  catch { return null; }
}
function rememberPosition() {
  const key = bookmarkKey();
  if (!key || !state.note || state.note.id !== state.selectedNote || $("#editorPane").hidden || $("#app").hidden) return;
  const range = state.editor?.state.selection.main;
  const field = $("#editorFallback");
  const backward = field.selectionDirection === "backward";
  const anchor = range?.anchor ?? (backward ? field.selectionEnd : field.selectionStart);
  const head = range?.head ?? (backward ? field.selectionStart : field.selectionEnd);
  try {
    localStorage.setItem(key, JSON.stringify({id: state.note.id, anchor, head, top: scrollArea.scrollTop}));
  } catch {}
}
let positionFrame = 0;
function queuePositionSave() {
  if (!positionFrame) positionFrame = requestAnimationFrame(() => {
    positionFrame = 0;
    rememberPosition();
  });
}
scrollArea.addEventListener("scroll", queuePositionSave, {passive:true});
$("#editorFallback").addEventListener("select", queuePositionSave);
document.addEventListener("selectionchange", () => {
  if (document.activeElement === $("#editorFallback")) queuePositionSave();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) rememberPosition();
});
window.addEventListener("pagehide", rememberPosition);
function projectForNote(noteId) { for (const project of state.projects) { const note = (project.notes || []).find(row => row.id === noteId); if (note) return [project, note]; } return []; }
async function selectNote(noteId, bookmark = null) {
  finishReading();

  const [project, listed] = projectForNote(noteId); if (!project || !listed) return;
  state.selectedProject = project.id; state.selectedNote = noteId;
  let note = listed, onlineLoad=true;
  try { note = await api(`/api/notes/${noteId}`); } catch (error) {
    if (error.status===404) { await deleteDraft(noteId); clearCurrentNote(noteId); toast("This session no longer has access to that note."); return; }
    if (bookmark) return;
    note = listed; onlineLoad=false;
  }
  const draft = await readDraft(note);
  const hasCachedBody=Object.prototype.hasOwnProperty.call(draft,"body");
  const savedLineNumbers = Boolean(note.line_numbers ?? 1);
  const localLineNumbers = typeof draft.line_numbers === "boolean" ? draft.line_numbers : savedLineNumbers;
  const localDirty = onlineLoad && draft.dirty && (draft.title !== note.title || draft.body !== note.body || localLineNumbers !== savedLineNumbers);
  const useLocal = hasCachedBody && (!onlineLoad || localDirty);
  state.note = { ...note, title:useLocal?draft.title:note.title, body:useLocal?draft.body:note.body,
    line_numbers:useLocal?localLineNumbers:savedLineNumbers,
    baseRevision:useLocal?(draft.baseRevision??note.revision):note.revision, dirty:Boolean(useLocal&&draft.dirty) };
  if (onlineLoad && !localDirty && note.updated_at) writeDraft({...note,baseRevision:note.revision,dirty:false});
  if (bookmark) project.collapsed = false;
  renderTree(); showEditor(bookmark); closeSidebar();
  if (localDirty) queueSave();
}
function showEditor(bookmark = null) {
  showSaveStatus(state.note.dirty ? "On this device" : "Saved");
  finishReading();

  $("#welcome").hidden = true; $("#settingsPane").hidden = true; $("#editorPane").hidden = false;
  updateLineNumberControl();
  $("#noteTitle").textContent = state.note.title || ""; renderBreadcrumbs(); setWordCount(state.note.body || "");
  const draft = state.note.body || "";
  if (EditorView && !state.editor) {
    $("#editor").hidden = false; $("#editorFallback").hidden = true;
    state.editor = new EditorView({ doc: draft, extensions: [basicSetup, EditorView.lineWrapping, markdown(), markdownPreview, EditorView.updateListener.of(update => { if (update.selectionSet || update.docChanged) queuePositionSave(); if (update.docChanged) onBodyChanged(update.state.doc.toString()); })], parent: $("#editor") });
  } else if (EditorView) { state.editor.dispatch({ changes: { from: 0, to: state.editor.state.doc.length, insert: draft } }); }
  else {
    $("#editor").hidden = true;
    const fallback = $("#editorFallback");
    fallback.hidden = false;
    fallback.value = draft;
    fallbackPictures(draft);
    const fit = () => {
      fallback.style.height = "auto";
      fallback.style.height = fallback.scrollHeight + "px";
    };
    fit();
    window.onresize = fit;
    fallback.oninput = () => {
      fit();
      fallbackPictures(fallback.value);
      onBodyChanged(fallback.value);
    };
  }
  const clamp = value => Number.isInteger(value) ? Math.max(0, Math.min(value, draft.length)) : 0;
  const anchor = clamp(bookmark?.anchor);
  const head = clamp(bookmark?.head);
  if (state.editor) state.editor.dispatch({selection: {anchor, head}});
  else $("#editorFallback").setSelectionRange(Math.min(anchor, head), Math.max(anchor, head),
    head < anchor ? "backward" : "forward");
  scrollArea.scrollTop = bookmark?.top || 0;
  if (bookmark && state.editor) {
    const id = state.note.id;
    state.editor.requestMeasure({read: () => id, write: () => {
      if (state.note?.id === id) { scrollArea.scrollTop = bookmark.top || 0; queuePositionSave(); }
    }});
  }
  queuePositionSave();
}
const speechButton = $("#readAloud");
const synthesis = window.speechSynthesis;
const voicePicker = $("#speechVoice");
const voicePreference = "gj-read-aloud-voice";
const qualityVoice = /\b(natural|neural|premium|enhanced)\b/i;
function availableVoices() {
  return synthesis?.getVoices() || [];
}
function preferredVoice() {
  const voices = availableVoices();
  const language = (document.documentElement.lang || "en").split("-")[0];
  const local = voices.filter(voice => voice.localService);
  const matching = local.filter(voice => voice.lang.split("-")[0] === language);
  return voices.find(voice => voice.voiceURI === localStorage.getItem(voicePreference))
    || matching.find(voice => qualityVoice.test(voice.name))
    || matching[0] || local[0];
}
function refreshVoicePicker() {
  const voices = availableVoices();
  const saved = localStorage.getItem(voicePreference);
  voicePicker.replaceChildren(new Option(voices.length ? "Automatic (on device)" : "No voices available", ""));
  for (const english of [true, false]) {
    const languageGroup = english ? "English" : "Other languages";
    const subset = voices.filter(voice => /^en(?:[-_]|$)/i.test(voice.lang) === english);
    for (const local of [true, false]) {
      const group = document.createElement("optgroup");
      group.label = languageGroup + " · " + (local ? "On device" : "Online");
      const choices = subset.filter(voice => voice.localService === local)
        .sort((a, b) => Number(qualityVoice.test(b.name)) - Number(qualityVoice.test(a.name))
          || a.name.localeCompare(b.name));
      for (const voice of choices) group.append(new Option(voice.name + " (" + voice.lang + ")", voice.voiceURI));
      if (choices.length) voicePicker.append(group);
    }
  }
  $("#onlineVoiceInfo").hidden = !voices.some(voice => !voice.localService);
  voicePicker.value = voices.some(voice => voice.voiceURI === saved) ? saved : "";
  voicePicker.disabled = !voices.length;
  $("#previewVoice").disabled = !preferredVoice();
}
voicePicker.onchange = () => {
  localStorage.setItem(voicePreference, voicePicker.value);
  refreshVoicePicker();
};
$("#previewVoice").onclick = () => {
  const voice = preferredVoice();
  if (!voice) return;
  synthesis.cancel();
  const sample = new SpeechSynthesisUtterance("Here’s a thought. What would you like to work on today?");
  sample.voice = voice;
  sample.lang = voice.lang;
  synthesis.speak(sample);
};
synthesis?.addEventListener("voiceschanged", refreshVoicePicker);
refreshVoicePicker();
let reading = null;

function finishReading(restoreCursor = false) {
  if (!reading) return;
  const session = reading;
  reading = null;
  $("#speechControls").hidden = true;
  synthesis.cancel();
  speechButton.hidden = false;
  if (!restoreCursor || state.note?.id !== session.noteId) return;

  const { position, from, to } = session;
  if (state.editor) {
    if (from === to) state.editor.dispatch({ selection: { anchor: position }, scrollIntoView: true });
    state.editor.focus();
  } else {
    const field = $("#editorFallback");
    field.focus();
    if (from === to) field.setSelectionRange(position, position);
    else field.setSelectionRange(from, to, session.direction);
  }
}

speechButton.onmousedown = event => event.preventDefault();
speechButton.onclick = () => {
  if (reading) return finishReading(true);
  if (!synthesis || !window.SpeechSynthesisUtterance) return toast("This browser does not support read aloud.");
  const voice = preferredVoice();
  if (!voice) return toast("Choose an available voice in Settings → Read aloud.");
  const field = $("#editorFallback");
  const range = state.editor?.state.selection.main;
  const from = range?.from ?? field.selectionStart;
  const to = range?.to ?? field.selectionEnd;
  const body = state.editor?.state.doc.toString() ?? field.value;
  const end = from === to ? body.length : to;
  if (!body.slice(from, end).trim()) return toast("Place the cursor before text to read.");
  const session = { noteId: state.note.id, position: from, from, to, direction: field.selectionDirection };
  synthesis.cancel();
  reading = session;
  speechButton.hidden = true;
  const passages = [];
  for (let start = from; start < end;) {
    const sample = body.slice(start, Math.min(start + 200, end));
    const sentence = sample.match(/^.*?[.!?](?=\s|$)|^[^\n]*\n/s);
    const lastSpace = sample.search(/\s+\S*$/);
    const count = sentence?.[0].length || (start + sample.length < end && lastSpace > 0 ? lastSpace + 1 : sample.length);
    if (sample.slice(0, count).trim()) passages.push({start, end: start + count});
    start += count;
  }

  let index = 0;
  let paused = false;
  const pauseButton = $("#speechPause");
  pauseButton.textContent = "Pause";
  $("#speechControls").hidden = false;
  function cancelPassage() {
    session.utterance = null;
    synthesis.cancel();
  }
  function playPassage(start) {
    const text = body.slice(start, passages[index].end);
    const utterance = new SpeechSynthesisUtterance(text);
    session.utterance = utterance;
    utterance.voice = voice;
    utterance.lang = voice.lang;
    session.position = start;
    utterance.onboundary = event => {
      if (reading === session && session.utterance === utterance) session.position = start + Math.min(event.charIndex, text.length);
    };
    utterance.onend = () => {
      if (reading !== session || session.utterance !== utterance) return;
      if (++index < passages.length) moveToPassage(index);
      else {
        session.position = end;
        finishReading(true);
      }
    };
    utterance.onerror = () => {
      if (reading !== session || session.utterance !== utterance) return;
      finishReading(true);
      toast("The device voice could not finish reading.");
    };
    synthesis.speak(utterance);
  }
  function moveToPassage(nextIndex) {
    cancelPassage();
    index = nextIndex;
    session.position = passages[index].start;
    $("#speechBack").disabled = index === 0;
    $("#speechNext").disabled = index === passages.length - 1;
    if (!paused) playPassage(session.position);
  }
  pauseButton.onclick = () => {
    paused = !paused;
    pauseButton.textContent = paused ? "Play" : "Pause";
    if (paused) cancelPassage();
    else playPassage(session.position);
  };
  $("#speechBack").onclick = () => moveToPassage(Math.max(0, index - 1));
  $("#speechNext").onclick = () => moveToPassage(Math.min(passages.length - 1, index + 1));
  $("#speechStop").onclick = () => finishReading(true);
  moveToPassage(0);
};
window.addEventListener("pagehide", () => finishReading());

function closeEditor() {
  localStorage.removeItem(bookmarkKey());
  finishReading();
 $("#editorPane").hidden = true; $("#welcome").hidden = false; state.note = null; state.selectedNote = null; renderTree(); }
function setWordCount(text) { const count = (text.trim().match(/\S+/g) || []).length; $("#wordCount").textContent = `${count} ${count === 1 ? "word" : "words"}`; }
function renderBreadcrumbs() {
  if (!state.note) return;
  const [project] = projectForNote(state.note.id); const map = new Map((project?.notes || []).map(n => [n.id, n]));
  const lineage = []; let current = state.note; while (current) { lineage.unshift(current.title || "Untitled note"); current = current.parent_id ? map.get(current.parent_id) : null; }
  $("#breadcrumbs").innerHTML = `<button data-project="${project?.id || ""}">My Projects</button><span>›</span><button data-project="${project?.id || ""}">${escapeHTML(project?.title || "Project")}</button><span>›</span><span>${lineage.map(escapeHTML).join(" › ")}</span>`;
  $("#breadcrumbs").querySelectorAll("button").forEach(button => button.addEventListener("click", () => { state.selectedProject = button.dataset.project; closeEditor(); }));
}
function clearCurrentNote(noteId) {
  if (readBookmark()?.id === noteId) localStorage.removeItem(bookmarkKey());
  if (state.note?.id !== noteId) return;
  state.note=null;state.selectedNote=null;
  if(state.editor&&state.editor.state.doc.length)state.editor.dispatch({changes:{from:0,to:state.editor.state.doc.length,insert:""}});
  $("#noteTitle").textContent="";setWordCount("");$("#editorPane").hidden=true;
  $("#welcome").hidden=!$("#settingsPane").hidden;renderTree();
}
function onBodyChanged(body) {
  finishReading();
 if(!state.note)return; state.note.body = body; setWordCount(body); noteLocalSave(); queueSave(); }
function updateLineNumberControl() {
  const enabled = Boolean(state.note?.line_numbers ?? true);
  $("#editorPane").classList.toggle("line-numbers-hidden", !enabled);
  const button = $("#lineNumbersButton");
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", enabled ? "Hide line numbers" : "Show line numbers");
}
function draftDB() { return new Promise((resolve,reject) => { const request = indexedDB.open("gj-brainstorming-local", 1); request.onupgradeneeded = () => request.result.createObjectStore("drafts", {keyPath:"id"}); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
async function deleteDraft(noteId) { try { const db=await draftDB(); await new Promise((resolve,reject)=>{const tx=db.transaction("drafts","readwrite");tx.objectStore("drafts").delete(noteId);tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);}); } catch {} }
async function writeDraft(note) { try { const db = await draftDB(); await new Promise((resolve,reject) => { const tx = db.transaction("drafts","readwrite"); tx.objectStore("drafts").put({id:note.id,title:note.title,body:note.body,line_numbers:note.line_numbers===undefined?true:Boolean(note.line_numbers),baseRevision:note.baseRevision??note.revision,baseUpdatedAt:note.updated_at,dirty:Boolean(note.dirty),updatedAt:Date.now()}); tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error); }); } catch { /* Keep editing even if browser storage is unavailable. */ } }
async function readDraft(note) { try { const db = await draftDB(); return await new Promise((resolve,reject) => { const tx = db.transaction("drafts","readonly"); const req=tx.objectStore("drafts").get(note.id);req.onsuccess=()=>resolve(req.result||note);req.onerror=()=>reject(req.error); }); } catch { return note; } }
function showSaveStatus(label) {
  const status = $("#noteSaved");
  status.querySelector(".save-text").textContent = label;
  status.dataset.unsaved = String(label !== "Saved");
  status.title = label;
}
function noteLocalSave() { if (!state.note) return; writeDraft({...state.note,dirty:true}); showSaveStatus("Saving…"); }
function queueSave() { if (!state.note) return; clearTimeout(state.saveTimer); noteLocalSave(); state.saveTimer = setTimeout(saveNow, 700); }
async function saveNow() {
  if (!state.note || state.busy) return;
  const snapshot = {id:state.note.id,title:state.note.title,body:state.note.body,line_numbers:state.note.line_numbers!==false,expected_revision:state.note.baseRevision??state.note.revision};
  let succeeded=false;
  try { state.busy=true; const saved=await api(`/api/notes/${snapshot.id}`,{method:"PUT",body:JSON.stringify(snapshot)}); state.note.updated_at=saved.updated_at; state.note.revision=saved.revision;state.note.baseRevision=saved.revision; const [,listed]=projectForNote(snapshot.id); if (listed) Object.assign(listed,saved); await writeDraft({...state.note,dirty:false}); if (state.note?.id === snapshot.id) showSaveStatus("Saved"); setStatus("Synced");succeeded=true; }
  catch (error) {
    if(error.status===404) { await deleteDraft(snapshot.id); clearCurrentNote(snapshot.id); toast("This session no longer has access to that note."); }
    setStatus(navigator.onLine?"Sync issue":"Offline",navigator.onLine?"error":"offline");
    if (state.note?.id === snapshot.id) showSaveStatus(error.status===409?"Draft conflict":"On this device");
    if(error.status===409)toast("The server copy changed. Your local draft is still saved on this device.");
  }
  finally { state.busy=false; renderTree(); if(succeeded&&state.note?.id===snapshot.id&&(state.note.title!==snapshot.title||state.note.body!==snapshot.body||state.note.line_numbers!==snapshot.line_numbers))queueSave(); }
}
function openTitleDialog(kind, explicitParent = undefined) {
  if (kind === "note" && !state.selectedProject && !state.projects.length) { toast("Create a project first."); return; }
  state.dialogKind=kind; const title = kind === "project" ? "Name your project" : "Give this note a title";
  $("#dialogEyebrow").textContent=kind === "project"?"NEW PROJECT":"NEW NOTE"; $("#titleDialogHeading").textContent=title; $("#newTitleInput").placeholder=kind === "project"?"A space for related ideas":"A thought worth keeping";
  const project=state.projects.find(p=>p.id===state.selectedProject); const selected=state.note?.project_id===state.selectedProject ? state.note : (state.selectedNote ? projectForNote(state.selectedNote)[1] : null);
  state.dialogParent = explicitParent !== undefined ? explicitParent : (kind === "note" && selected ? selected : null);
  let parent=state.dialogParent;
  $("#dialogContext").textContent=kind==="project"?"Projects keep related notes together.":parent?`Will be added inside “${parent.title}”.${depthFor(parent) >= 3 ? " This note is already at the deepest level." : ""}`:project?`Will be added to “${project.title}”.`:"Choose a project to hold this note.";
  $("#dialogError").textContent=""; $("#titleDialog").hidden=false; $("#newTitleInput").value=""; setTimeout(()=>$("#newTitleInput").focus(),20);
}
function depthFor(note) { const project=projectForNote(note.id)[0]; const map=new Map((project?.notes||[]).map(n=>[n.id,n])); return depthOf(note,map); }
$("#titleForm").addEventListener("submit", async event => {
  event.preventDefault(); const title=$("#newTitleInput").value.trim(); if (!title) return; const kind=state.dialogKind;
  try {
    if (kind === "project") { const project=await api("/api/projects",{method:"POST",body:JSON.stringify({title})}); state.projects.push({...project,notes:[]}); state.selectedProject=project.id;state.note=null;state.selectedNote=null;renderTree(); $("#titleDialog").hidden=true; openTitleDialog("note",null); }
    else { const project=state.projects.find(p=>p.id===state.selectedProject); if (!project) throw new Error("Select a project first"); const parent=state.dialogParent; const note=await api("/api/notes",{method:"POST",body:JSON.stringify({title,project_id:project.id,parent_id:parent?.id||null})}); project.notes.push(note); state.note={...note,baseRevision:note.revision};state.selectedNote=note.id;await writeDraft({...state.note,dirty:false}); $("#titleDialog").hidden=true;renderTree();showEditor(); $("#editor").querySelector(".cm-content")?.focus(); }
  } catch(error) { $("#dialogError").textContent=error.message; }
});
$("#cancelTitle").onclick=()=>$("#titleDialog").hidden=true;
$("#newProjectButton").onclick=$("#sidebarProjectButton").onclick=$("#welcomeProjectButton").onclick=()=>openTitleDialog("project");
const importFile = $("#importFile");
$("#importButton").onclick = () => {
  if (!state.selectedProject) {
    toast("Select a project to import into first.");
    return;
  }
  importFile.dataset.project = state.selectedProject;
  importFile.click();
};
importFile.onchange = async () => {
  const file = importFile.files[0];
  importFile.value = "";
  if (!file) return;
  const project = state.projects.find(item => item.id === importFile.dataset.project);
  if (!project) return;
  const button = $("#importButton");
  button.disabled = true;
  button.textContent = "Importing…";
  try {
    if (!/\.(md|markdown|pdf)$/i.test(file.name)) throw new Error("Choose a Markdown or PDF file.");
    if (file.size > 20 * 1024 * 1024) throw new Error("Choose a file under 20 MiB.");
    const title = file.name.replace(/\.[^.]+$/, "").trim().slice(0, 160) || "Imported note";
    const params = new URLSearchParams({ project_id: project.id, title });
    const type = /\.pdf$/i.test(file.name) ? "application/pdf" : "text/markdown";
    const note = await api("/api/notes/import?" + params, {
      method: "POST", headers: { "Content-Type": type }, body: file
    });
    if (state.note) await saveNow();
    project.notes.push(note);
    project.collapsed = false;
    await selectNote(note.id);
    toast("Imported into " + project.title);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Import file…";
  }
};
$("#addNoteButton").onclick=()=>openTitleDialog("note");
$("#lineNumbersButton").onclick=()=>{if(!state.note)return;state.note.line_numbers=!Boolean(state.note.line_numbers);updateLineNumberControl();queueSave();};
function toggleSidebar(open) { $("#sidebar").classList.toggle("open",open); $("#scrim").classList.toggle("visible",open); }
function closeSidebar() { toggleSidebar(false); }
$("#menuButton").onclick=()=>toggleSidebar(true); $("#scrim").onclick=closeSidebar;
function format(action) {
  if (action === "picture") { $("#pictureFile").click(); return; }
  if (!state.editor) { const field=$("#editorFallback"); if(field.hidden)return; const from=field.selectionStart,to=field.selectionEnd,selected=field.value.slice(from,to); const wrappers={bold:["**","**"],italic:["*","*"],underline:["__","__"],strike:["~~","~~"],highlight:["==","=="],code:["`","`"]}; if(action==="undo"){document.execCommand("undo");return;}if(action==="redo"){document.execCommand("redo");return;}let lead="",tail="";if(wrappers[action]){[lead,tail]=wrappers[action];}else if(action==="heading")lead="## ";else if(action==="bullet")lead="- ";else if(action==="number")lead="1. ";else if(action==="quote")lead="> ";else if(action==="link"){lead="[";tail="](url)";}else return;const content=selected||"text";field.setRangeText(lead+content+tail,from,to,"select");field.focus();field.setSelectionRange(from+lead.length,from+lead.length+content.length);field.dispatchEvent(new Event("input"));return; }
  const view=state.editor, {from,to}=view.state.selection.main, selected=view.state.sliceDoc(from,to);
  const wrappers={bold:["**","**"],italic:["*","*"],underline:["__","__"],strike:["~~","~~"],highlight:["==","=="],code:["`","`"],quote:["> ",""]};
  if (action==="undo") { undo(view); view.focus(); return; }
  if (action==="redo") { redo(view); view.focus(); return; }
  if (action==="heading"||action==="bullet"||action==="number"||action==="quote") { const prefix={heading:"## ",bullet:"- ",number:"1. ",quote:"> "}[action]; const line=view.state.doc.lineAt(from); view.dispatch({changes:{from:line.from,to:line.from,insert:prefix},selection:{anchor:from+prefix.length,head:to+prefix.length}}); view.focus(); return; }
  if (action==="link") { const text=selected||"link text"; const insert=`[${text}](url)`; view.dispatch({changes:{from,to,insert},selection:{anchor:from+text.length+3,head:from+text.length+6}}); view.focus();return; }
  const pair=wrappers[action]; if (!pair) return; const insert=pair[0]+(selected||"text")+pair[1]; view.dispatch({changes:{from,to,insert},selection:{anchor:from+pair[0].length,head:from+pair[0].length+(selected||"text").length}}); view.focus();
}
const toolbarTooltip = $("#toolbarTooltip");
function hideToolbarTooltip() {
  toolbarTooltip.hidden = true;
}
function showToolbarTooltip(button) {
  toolbarTooltip.textContent = button.getAttribute("aria-label");
  toolbarTooltip.hidden = false;
  const rect = button.getBoundingClientRect();
  const width = toolbarTooltip.offsetWidth;
  const left = Math.min(window.innerWidth - width - 4, rect.left + (rect.width - width) / 2);
  toolbarTooltip.style.left = Math.max(4, left) + "px";
  toolbarTooltip.style.top = (rect.bottom + toolbarTooltip.offsetHeight + 4 <= window.innerHeight
    ? rect.bottom + 4 : rect.top - toolbarTooltip.offsetHeight - 4) + "px";
}
for (const button of document.querySelectorAll(".format-toolbar button")) {
  button.addEventListener("pointerenter", event => {
    if (event.pointerType !== "touch") showToolbarTooltip(button);
  });
  button.addEventListener("focus", () => {
    if (button.matches(":focus-visible")) showToolbarTooltip(button);
  });
  for (const event of ["pointerleave", "blur", "click"]) button.addEventListener(event, hideToolbarTooltip);
}
document.addEventListener("scroll", hideToolbarTooltip, true);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") hideToolbarTooltip();
});
document.querySelectorAll(".format-toolbar button").forEach(button=>button.addEventListener("click",()=>format(button.dataset.action)));
$("#themeButton").onclick=()=>{ const next={system:"dark",dark:"light",light:"system"}[state.theme];applyTheme(next);toast(`Theme: ${next}`); };
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change",()=>{if(state.theme==="system")applyTheme("system");});
document.querySelectorAll("[data-theme]").forEach(button=>button.onclick=()=>applyTheme(button.dataset.theme));
$("#settingsButton").onclick=showSettings;
const localTimeFormat = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "short", day: "numeric", hour: "numeric",
  minute: "2-digit", second: "2-digit", timeZoneName: "short",
});
function localTimestamp(value) {
  // Older backups recorded the completion time to the nearest UTC minute.
  const iso = value.replace(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC$/, "$1T$2:00Z");
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? value : localTimeFormat.format(date);
}
let backupPoll;
async function loadBackups() {
  clearTimeout(backupPoll);
  try {
    const info = await api("/api/backups");
    $("#backupCard").hidden = !info.allowed;
    if (!info.allowed) return;
    $("#backupStatus").textContent = !info.configured ? "Mount the Google Drive backup folder and configure its encryption key on Ubuntu." :
      ["Drive backup configured.", info.running ? "Backup running…" : "",
       info.last ? "Last written to Drive mount: " + localTimestamp(info.last) : "", info.error].filter(Boolean).join(" ");
    $("#backupNow").disabled = !info.configured || info.running;
    $("#backupInterval").disabled = !info.configured;
    $("#backupInterval").value = String(info.hours);
    if (info.running && !$("#settingsPane").hidden) backupPoll = setTimeout(loadBackups, 2000);
  } catch(error) { toast(error.message); }
}
async function driveAction(action, extra = {}) {
  try {
    await api("/api/backups",{method:"POST",body:JSON.stringify({action,...extra})});
    await loadBackups();
  } catch(error) { toast(error.message); await loadBackups(); }
}
$("#backupNow").onclick = () => driveAction("run");
$("#backupInterval").onchange = event => driveAction("schedule",{hours:Number(event.target.value)});
async function showSettings() {
  loadBackups();
  refreshVoicePicker();
  finishReading();
 if(state.note) await saveNow();$("#editorPane").hidden=true;$("#welcome").hidden=true;$("#settingsPane").hidden=false;closeSidebar();$("#deviceSessionsCard").hidden=!state.canManagePermissions;$("#accessMatrixCard").hidden=!state.canManagePermissions;$("#archiveCard").hidden=false;loadArchive();if(state.canManagePermissions){loadSessions();loadAccessMatrix();} }
async function loadSessions() {
  try {
    const sessions = await api("/api/sessions");
    const list = $("#sessionList");
    list.innerHTML = sessions.length ? sessions.map(session => {
      const status = session.revoked_at ? "Revoked" : Date.parse(session.expires_at) <= Date.now() ? "Expired" : "Active";
      const lastUsed = session.last_used_at ? localTimestamp(session.last_used_at) : "Never";
      const expires = localTimestamp(session.expires_at);
      const disconnect = status === "Active" ? `<button data-disconnect="${session.id}" class="revoke-button">Disconnect</button>` : "";
      return `<div class="token-row"><span><b>${escapeHTML(session.client_name)}</b><small>${status}${session.can_manage_permissions?" · Can manage permissions":""} · Last used ${lastUsed} · Expires ${expires}</small></span>${status==="Active" ? `<label><input type="checkbox" data-backup="${session.id}" ${session.can_backup?"checked":""}> Manage full backups</label>` : ""}${disconnect}</div>`;
    }).join("") : "<p class='muted'>No recorded sessions.</p>";
    list.querySelectorAll("[data-backup]").forEach(box => box.onchange = async () => {
      box.disabled = true;
      try {
        await api("/api/backup-permission",{method:"POST",body:JSON.stringify({id:box.dataset.backup,allowed:box.checked})});
        await loadBackups();
      } catch(error) { box.checked = !box.checked; toast(error.message); }
      finally { box.disabled = false; }
    });
    list.querySelectorAll("[data-disconnect]").forEach(button => button.onclick = async () => {
      await api(`/api/sessions/${button.dataset.disconnect}`, {method:"DELETE"});
      await loadSessions();
      await loadAccessMatrix();
      if (button.dataset.disconnect === localStorage.getItem("gj-session-id")) {
        localStorage.removeItem(AUTH_KEY);
        localStorage.removeItem("gj-session-id");
        showApp(false);
      }
      toast("Device disconnected. A new CLI token is needed to reconnect.");
    });
  } catch(error) { toast(error.message); }
}
async function loadAccessMatrix() {
  try {
    const matrix = await api("/api/access-matrix");
    const host = $("#accessMatrix");
    if (!matrix.sessions.length) { host.innerHTML="<p class='muted'>No active sessions to grant access to.</p>"; return; }
    const header = `<thead><tr><th>Note</th>${matrix.sessions.map(session=>`<th>${escapeHTML(session.client_name)}</th>`).join("")}</tr></thead>`;
    const rows = matrix.projects.map(project => {
      const section = `<tr class="access-project"><th colspan="${matrix.sessions.length+1}">${escapeHTML(project.title)}</th></tr>`;
      const notes = project.notes.map(note => `<tr><th>${escapeHTML(note.title||"Untitled note")}</th>${matrix.sessions.map(session=>`<td><input type="checkbox" aria-label="Allow ${escapeHTML(session.client_name)} to access ${escapeHTML(note.title||"Untitled note")}" data-note="${note.id}" data-session="${session.id}" ${note.access[session.id]?"checked":""}></td>`).join("")}</tr>`).join("");
      return section+notes;
    }).join("");
    host.innerHTML = `<div class="access-matrix-scroll"><table class="access-matrix-table">${header}<tbody>${rows}</tbody></table></div>`;
    host.querySelectorAll("input[data-note][data-session]").forEach(input=>input.addEventListener("change",async()=>{
      input.disabled=true;
      try {
        await api("/api/access-matrix",{method:"PUT",body:JSON.stringify({note_id:input.dataset.note,session_id:input.dataset.session,allowed:input.checked})});
        if(input.dataset.session===localStorage.getItem("gj-session-id")) {
          if(state.note?.id===input.dataset.note&&!input.checked) {
            await deleteDraft(input.dataset.note);
            clearCurrentNote(input.dataset.note);$("#settingsPane").hidden=false;
            toast("This session can no longer access that note.");
          }
          await loadProjects();
        }
      } catch(error) { input.checked=!input.checked;toast(error.message); }
      finally { input.disabled=false; }
    }));
  } catch(error) { $("#accessMatrix").innerHTML=`<p class="error">${escapeHTML(error.message)}</p>`; }
}
$("#signOutButton").onclick=async()=>{try{await api("/api/logout",{method:"POST",body:"{}"});}catch{}localStorage.removeItem(AUTH_KEY);localStorage.removeItem("gj-session-id");showApp(false);$("#authScreen").hidden=false;$("#app").hidden=true;$("#tokenForm").hidden=false;$("#tokenHelp").hidden=false;};
$("#searchInput").addEventListener("input",async event=>{const q=event.target.value.trim(),box=$("#searchResults");if(!q){box.hidden=true;return;}try{const rows=await api(`/api/search?q=${encodeURIComponent(q)}`);box.innerHTML=rows.length?rows.map(n=>`<button data-note="${n.id}"><b>${escapeHTML(n.title)}</b><small>${escapeHTML(n.project_title)}</small></button>`).join(""):"<p>No matching notes yet.</p>";box.hidden=false;box.querySelectorAll("[data-note]").forEach(b=>b.onclick=()=>selectNote(b.dataset.note));}catch{box.hidden=true;}});
function toast(message) { const node=$("#toast");node.textContent=message;node.classList.add("visible");setTimeout(()=>node.classList.remove("visible"),2800); }
window.addEventListener("online",()=>{if(state.note)saveNow();else if(token())loadProjects();});
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="hidden"&&state.note)saveNow();});

$("#setupForm").addEventListener("submit",async event=>{event.preventDefault();const form=new FormData(event.currentTarget);try{await api("/api/setup",{method:"POST",body:JSON.stringify({username:form.get("username")})});$("#setupForm").hidden=true;$("#tokenForm").hidden=false;$("#tokenHelp").hidden=false;$("#authIntro").textContent="Account created. Generate a one-use connection token on the Docker host, then paste it here.";}catch(error){$("#authError").textContent=error.message;}});
$("#tokenForm").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;try{const result=await api("/api/login",{method:"POST",body:JSON.stringify({token:new FormData(form).get("token")})});localStorage.setItem(AUTH_KEY,result.session);localStorage.setItem("gj-session-id",result.session_id||"");state.canManagePermissions=Boolean(result.can_manage_permissions);form.reset();showApp(true);}catch(error){try{if(token()){const session=await api("/api/session");if(session.authenticated){state.canManagePermissions=Boolean(session.can_manage_permissions);form.reset();showApp(true);return;}}}catch{}$("#authError").textContent=error.message;}});
async function start() {
  applyTheme(state.theme);
  try { const status=await fetch("/api/setup-status").then(r=>r.json()); if(status.needs_setup){$("#authIntro").textContent="Create your local account. Then generate a one-use token from the Docker host CLI for this device.";$("#setupForm").hidden=false; } else {$("#tokenForm").hidden=false;$("#tokenHelp").hidden=false;} } catch {$("#tokenForm").hidden=false;$("#tokenHelp").hidden=false;}
  if(token()) { try { const session=await api("/api/session"); if(session.authenticated){state.canManagePermissions=Boolean(session.can_manage_permissions);showApp(true);return;} } catch {} localStorage.removeItem(AUTH_KEY);localStorage.removeItem("gj-session-id"); }
  $("#authScreen").hidden=false;
  if("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(()=>{});
}
start();
