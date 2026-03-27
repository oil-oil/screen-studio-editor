#!/usr/bin/env python3
"""
Local HTTP server for subtitle preview + editing.
Serves HTML page (left=video, right=subtitle list), backed by Flask.
Save writes transcript.json and signals exit.
"""

import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_file,
)

# ----------------------------------------------------------------------------- #
# Config
# ----------------------------------------------------------------------------- #
PORT = 8765
TRANSCRIPT_PATH = None
VIDEO_PATH = None
RESULT_DONE = threading.Event()

# ----------------------------------------------------------------------------- #
# Flask app
# ----------------------------------------------------------------------------- #
app = Flask(__name__, static_folder=None)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>字幕编辑器</title>
<style>
  :root {
    --bg: #f5f5f3;
    --surface: #ffffff;
    --border: #e4e4e0;
    --text: #1a1a18;
    --muted: #8a8a82;
    --accent: #2563eb;
    --accent2: #dc2626;
    --hover: #f0f0ec;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* ---- panel header ---- */
  .panel-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 12px 8px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .panel-title { font-size: 12px; font-weight: 600; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; flex: 1; }
  .btn {
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    transition: background 0.12s;
    white-space: nowrap;
  }
  .btn:hover { background: var(--hover); }
  .btn.danger { color: var(--accent2); border-color: transparent; }
  .btn.danger:hover { background: #fef2f2; border-color: var(--accent2); }
  .btn.save {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
    padding: 6px 16px;
    font-size: 13px;
    width: 100%;
    border-radius: 8px;
  }
  .btn.save:hover { background: #1d4ed8; }

  /* ---- main ---- */
  .main { display: flex; flex: 1; overflow: hidden; position: relative; }

  /* ---- video pane ---- */
  .video-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    margin-right: 380px;
  }
  .video-wrap {
    flex: 1;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
  }
  .video-wrap video { width: 100%; height: 100%; object-fit: contain; }
  .current-subtitle {
    position: absolute;
    bottom: 6%;
    left: 50%;
    transform: translateX(-50%);
    max-width: 90%;
    text-align: center;
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: clamp(13px, 2.2vw, 22px);
    line-height: 1.5;
    color: #ffffff;
    background: rgba(32, 32, 32, 0.62);
    padding: 4px 14px 6px;
    border-radius: 4px;
    pointer-events: none;
    white-space: pre-wrap;
    word-break: break-word;
    text-shadow: 0 1px 3px rgba(0,0,0,0.8);
    display: none;
  }
  .current-subtitle.visible { display: block; }

  /* ---- list pane: always-visible right panel ---- */
  .list-pane {
    position: absolute;
    top: 0; right: 0; bottom: 0;
    width: 380px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--surface);
    border-left: 1px solid var(--border);
    z-index: 10;
  }

  /* ---- find bar: hidden by default, shown with Ctrl+F ---- */
  .find-bar {
    display: none;
    padding: 8px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .find-bar.show { display: flex; }
  .list-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .list-header span { font-size: 11px; color: var(--muted); }

  /* ---- panel footer ---- */
  .panel-footer {
    padding: 10px 12px;
    border-top: 1px solid var(--border);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .panel-status { font-size: 11px; color: var(--muted); text-align: center; min-height: 14px; }
  .subtitle-list { flex: 1; overflow-y: auto; padding: 6px; }
  .subtitle-list::-webkit-scrollbar { width: 6px; }
  .subtitle-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ---- subtitle item ---- */
  .sub-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 8px;
    margin-bottom: 3px;
    cursor: pointer;
    transition: background 0.1s;
    position: relative;
    user-select: none;
    -webkit-user-select: none;
  }
  .sub-item:hover { background: var(--hover); }
  .sub-item.deleted { opacity: 0.35; text-decoration: line-through; }
  .sub-item mark {
    background: #fbbf24;
    color: #1a1a18;
    border-radius: 3px;
    padding: 0 1px;
  }
  .sub-check { flex-shrink: 0; margin-top: 4px; width: 16px; height: 16px; cursor: pointer; }
  .sub-times {
    flex-shrink: 0;
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    min-width: 110px;
    margin-top: 3px;
    line-height: 1.6;
  }
  .sub-text-wrap { flex: 1; min-width: 0; }
  .sub-text {
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
    border-radius: 4px;
    padding: 1px 3px;
    margin: -1px -3px;
    outline: none;
    user-select: text;
    -webkit-user-select: text;
  }
  .sub-text:focus {
    background: rgba(37,99,235,0.06);
    box-shadow: 0 0 0 2px rgba(37,99,235,0.2);
  }
  .sub-text[contenteditable="true"] { cursor: text; }
  .sub-actions {
    flex-shrink: 0;
    display: flex;
    gap: 3px;
    margin-top: 1px;
    opacity: 0;
    transition: opacity 0.15s;
  }
  .sub-item:hover .sub-actions { opacity: 1; }
  .icon-btn {
    width: 26px; height: 26px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    cursor: pointer;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
  }
  .icon-btn:hover { background: var(--hover); color: var(--text); }
  .icon-btn.del:hover { background: #fef2f2; color: var(--accent2); border-color: var(--accent2); }

  /* ---- find bar ---- */
  .find-bar {
    display: none;
    padding: 8px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .find-bar.show { display: flex; }
  .find-bar input {
    background: #f9f9f7;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 4px 10px;
    font-size: 13px;
    min-width: 160px;
  }
  .find-bar input:focus { outline: none; border-color: var(--accent); }
  .find-bar label { font-size: 12px; color: var(--muted); }

  /* ---- status bar ---- */
  .status {
    padding: 6px 16px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--muted);
    flex-shrink: 0;
    display: flex;
    gap: 16px;
  }

  /* ---- mobile layout: video top, subtitles bottom ---- */
  @media (max-width: 768px) {
    body { height: 100dvh; overflow: hidden; }
    .main { flex-direction: column; overflow: hidden; }
    .video-pane { margin-right: 0; flex: 0 0 auto; height: 40dvh; }
    .video-wrap video { width: 100%; height: 100%; object-fit: contain; }
    .list-pane {
      position: relative;
      top: auto; right: auto; bottom: auto;
      width: 100%;
      flex: 1;
      border-left: none;
      border-top: 1px solid var(--border);
    }
    .sub-times { min-width: 80px; font-size: 10px; }
    .current-subtitle { font-size: clamp(11px, 3.5vw, 16px); }
  }
</style>
</head>
<body>

<div class="main">
  <div class="video-pane" id="videoPaneEl">
    <div class="video-wrap">
      <video id="vid" controls src="/video"></video>
      <div class="current-subtitle" id="curSub"></div>
    </div>
  </div>

  <div class="list-pane" id="listPane">
    <!-- Panel header: title + bulk actions -->
    <div class="panel-header">
      <span class="panel-title">字幕</span>
      <button class="btn" id="btnSelectAll">全选</button>
      <button class="btn danger" id="btnDeleteSelected">删除</button>
    </div>

    <!-- find bar: hidden by default, Ctrl+F to show -->
    <div class="find-bar" id="findBar">
      <label>查找</label><input id="findInput" placeholder="大小写不敏感…">
      <label>替换</label><input id="replaceInput" placeholder="新文本…">
      <button class="btn" id="btnReplaceOne">替换下一个</button>
      <button class="btn" id="btnReplaceAll">全部替换</button>
    </div>

    <!-- subtitle count row -->
    <div class="list-header">
      <input type="checkbox" id="selectAllCheck" title="全选">
      <span id="listInfo"></span>
    </div>

    <div class="subtitle-list" id="list"></div>

    <!-- Panel footer: status + save -->
    <div class="panel-footer">
      <div class="panel-status" id="statusText"></div>
      <button class="btn save" id="btnSave">保存并关闭</button>
    </div>
  </div>
</div>

<script>
const LS_KEY = 'subtitle_editor_v1';

// ── state ────────────────────────────────────────────────────────────────────
let segments = [];
let deletedIds = new Set();
let editMode = null;
let findIndex = -1;
let currentNeedle = '';

// ── DOM ─────────────────────────────────────────────────────────────────────
const vid        = document.getElementById('vid');
const curSub     = document.getElementById('curSub');
const listEl     = document.getElementById('list');
const findInput  = document.getElementById('findInput');
const repInput   = document.getElementById('replaceInput');
const selAll     = document.getElementById('selectAllCheck');
const listInfo   = document.getElementById('listInfo');
const statusTxt  = document.getElementById('statusText');
const listPane   = document.getElementById('listPane');
const videoPaneEl = document.getElementById('videoPaneEl');

// ── find bar toggle (Ctrl+F) ──────────────────────────────────────────────────
const findBarEl = document.getElementById('findBar');

function openFindBar() {
  findBarEl.classList.add('show');
  findInput.focus();
  findInput.select();
}
function closeFindBar() {
  findBarEl.classList.remove('show');
  currentNeedle = '';
  render();
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    e.preventDefault();
    findBarEl.classList.contains('show') ? closeFindBar() : openFindBar();
  }
  if (e.key === 'Escape' && findBarEl.classList.contains('show')) {
    closeFindBar();
  }
});

document.getElementById('btnClosePanel')?.addEventListener('click', closeFindBar);

// ── helpers ─────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function insertMarks(text, needle) {
  if (!needle) return escapeHtml(text);
  const re = new RegExp(escapeRe(needle), 'gi');
  return escapeHtml(text).replace(re, m => `<mark>${m}</mark>`);
}

function fmtSeg(seg) {
  const fmt = s => {
    const m = Math.floor(s / 60);
    const sec = (s % 60).toFixed(2).padStart(5, '0');
    return `${m}:${sec}`;
  };
  return `${fmt(seg.start)} → ${fmt(seg.end)}`;
}

function getVisibleSegments() {
  return segments.filter(s => !deletedIds.has(s._id));
}

// ── boot ────────────────────────────────────────────────────────────────────
async function init() {
  const cached = localStorage.getItem(LS_KEY);
  if (cached) {
    try {
      const data = JSON.parse(cached);
      segments = data.segments || [];
      deletedIds = new Set(data.deletedIds || []);
      statusTxt.textContent = '已从 localStorage 恢复编辑进度';
    } catch {
      await loadFromServer();
    }
  } else {
    await loadFromServer();
  }
  render();
  updateInfo();
}

async function loadFromServer() {
  const r = await fetch('/api/transcript');
  const data = await r.json();
  segments = data.segments || [];
  deletedIds = new Set();
}

init();

// ── video sync ─────────────────────────────────────────────────────────────
vid.addEventListener('timeupdate', () => {
  const t = vid.currentTime;
  let active = null;
  for (const s of segments) {
    if (!deletedIds.has(s._id) && t >= s.start && t <= s.end) {
      active = s; break;
    }
  }
  if (active) {
    curSub.textContent = active.text.trim();
    curSub.classList.add('visible');
  } else {
    curSub.textContent = '';
    curSub.classList.remove('visible');
  }
  document.querySelectorAll('.sub-item').forEach(el => {
    const isActive = active && el.dataset.id == active._id;
    el.classList.toggle('current', !!isActive);
    if (isActive) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
});

// ── render ──────────────────────────────────────────────────────────────────
function render() {
  listEl.innerHTML = '';
  const vis = getVisibleSegments();
  selAll.checked = vis.length > 0 && vis.every(s => !deletedIds.has(s._id));

  segments.forEach(seg => {
    const id = seg._id || (seg._id = Math.random().toString(36).slice(2));
    const isDel = deletedIds.has(id);
    const isEdit = editMode === id;

    const item = document.createElement('div');
    item.className = 'sub-item' + (isDel ? ' deleted' : '') + (isEdit ? ' current' : '');
    item.dataset.id = id;

    const times = document.createElement('div');
    times.className = 'sub-times';
    times.textContent = fmtSeg(seg);

    // Checkbox — stop all propagation so clicks don't bubble to row seek
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.className = 'sub-check';
    check.checked = isDel;
    check.addEventListener('click', e => { e.stopPropagation(); toggleDelete(id); });
    check.addEventListener('mousedown', e => e.stopPropagation());
    check.addEventListener('touchstart', e => e.stopPropagation());

    const textWrap = document.createElement('div');
    textWrap.className = 'sub-text-wrap';

    const textEl = document.createElement('div');
    textEl.className = 'sub-text';
    textEl.contentEditable = 'false';
    // When needle active show highlights; otherwise plain text
    if (currentNeedle) {
      textEl.innerHTML = insertMarks(seg.text, currentNeedle);
    } else {
      textEl.textContent = seg.text;
    }

    // Click row → seek video (only when not editing this item)
    textEl.addEventListener('mousedown', e => {
      if (textEl.contentEditable === 'true') {
        e.stopPropagation(); // don't seek while editing
      }
    });
    textEl.addEventListener('dblclick', e => {
      e.stopPropagation();
      startEdit(id, textEl);
    });
    textEl.addEventListener('blur', () => {
      if (textEl.contentEditable === 'true') {
        finishEdit(id, textEl.innerText.trim());
        textEl.contentEditable = 'false';
        // Restore highlights if needle still active
        if (currentNeedle) textEl.innerHTML = insertMarks(seg.text, currentNeedle);
        else textEl.textContent = seg.text;
      }
    });
    textEl.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        textEl.contentEditable = 'false';
        textEl.textContent = seg.text;
        editMode = null;
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        textEl.blur();
      }
    });

    textWrap.appendChild(textEl);

    // Action buttons
    const actions = document.createElement('div');
    actions.className = 'sub-actions';
    const editBtn = document.createElement('button');
    editBtn.className = 'icon-btn';
    editBtn.textContent = '✎';
    editBtn.title = '编辑';
    editBtn.addEventListener('click', e => { e.stopPropagation(); startEdit(id, textEl); });
    const delBtn = document.createElement('button');
    delBtn.className = 'icon-btn del';
    delBtn.textContent = '✕';
    delBtn.title = '删除';
    delBtn.addEventListener('click', e => { e.stopPropagation(); toggleDelete(id); });
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);

    // Click row → seek video (only when text not in edit mode)
    item.addEventListener('click', e => {
      const el = listEl.querySelector(`[data-id="${id}"] .sub-text`);
      if (el && el.contentEditable === 'true') return;
      vid.currentTime = seg.start;
      // Immediately show this subtitle without waiting for timeupdate
      curSub.textContent = seg.text.trim();
      curSub.classList.add('visible');
    });

    item.appendChild(check);
    item.appendChild(times);
    item.appendChild(textWrap);
    item.appendChild(actions);
    listEl.appendChild(item);
  });
}

function startEdit(id, textEl) {
  editMode = id;
  textEl.contentEditable = 'true';
  textEl.innerHTML = '';
  const seg = segments.find(s => s._id === id);
  if (seg) textEl.textContent = seg.text;
  textEl.focus();
  // Move cursor to end
  const range = document.createRange();
  range.selectNodeContents(textEl);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function finishEdit(id, value) {
  const seg = segments.find(s => s._id === id);
  if (seg) seg.text = value;
  editMode = null;
  saveLS();
}

function toggleDelete(id) {
  deletedIds.has(id) ? deletedIds.delete(id) : deletedIds.add(id);
  saveLS();
  render();
  updateInfo();
}

function updateInfo() {
  const total = segments.length;
  const vis   = getVisibleSegments().length;
  listInfo.textContent = `共 ${total} 条 | 显示 ${vis} | 已删除 ${total - vis}`;
}

// ── localStorage ────────────────────────────────────────────────────────────
function saveLS() {
  localStorage.setItem(LS_KEY, JSON.stringify({
    segments,
    deletedIds: [...deletedIds]
  }));
}

// ── find / replace ──────────────────────────────────────────────────────────
// Update highlights on every keystroke in find input
findInput.addEventListener('input', () => {
  currentNeedle = findInput.value;
  render();
});

function doFind(replaceVal, replaceOne) {
  const needle = findInput.value;
  if (!needle) return;
  const re = new RegExp(escapeRe(needle), 'i');
  const visible = getVisibleSegments();
  let startIdx = findIndex < 0 ? 0 : findIndex + (replaceOne ? 1 : 0);

  for (let i = 0; i < visible.length; i++) {
    const idx = (startIdx + i) % visible.length;
    if (re.test(visible[idx].text)) {
      findIndex = idx;
      const el = listEl.querySelector(`[data-id="${visible[idx]._id}"]`);
      if (el) {
        el.scrollIntoView({ block: 'nearest' });
        el.style.outline = '2px solid var(--accent)';
        setTimeout(() => { if (el) el.style.outline = ''; }, 1500);
      }
      if (replaceOne) {
        const seg = segments.find(s => s._id === visible[idx]._id);
        if (seg) {
          seg.text = seg.text.replace(new RegExp(escapeRe(needle), 'gi'), replaceVal);
          saveLS();
          render();
        }
      } else if (replaceVal) {
        let count = 0;
        segments.forEach(s => {
          if (deletedIds.has(s._id)) return;
          const next = s.text.replace(new RegExp(escapeRe(needle), 'gi'), replaceVal);
          if (next !== s.text) { s.text = next; count++; }
        });
        statusTxt.textContent = `已替换 ${count} 处`;
        saveLS();
        render();
        return;
      }
      return;
    }
  }
  statusTxt.textContent = '未找到: ' + needle;
}

document.getElementById('btnReplaceOne').addEventListener('click', () => doFind(repInput.value, true));
document.getElementById('btnReplaceAll').addEventListener('click', () => doFind(repInput.value, false));
findInput.addEventListener('keydown', e => { if (e.key === 'Enter') doFind(repInput.value, true); });

// ── bulk select / delete ────────────────────────────────────────────────────
document.getElementById('btnSelectAll').addEventListener('click', () => {
  const allDel = segments.every(s => deletedIds.has(s._id));
  if (allDel) { deletedIds.clear(); statusTxt.textContent = '已取消全部删除'; }
  else { segments.forEach(s => deletedIds.add(s._id)); statusTxt.textContent = '已选中全部'; }
  saveLS();
  render();
  updateInfo();
});

document.getElementById('btnDeleteSelected').addEventListener('click', () => {
  const sel = segments.filter(s => deletedIds.has(s._id));
  if (!sel.length) { statusTxt.textContent = '请先勾选要删除的条目'; return; }
  sel.forEach(s => deletedIds.add(s._id));
  saveLS();
  render();
  updateInfo();
  statusTxt.textContent = `已删除 ${sel.length} 条`;
});

selAll.addEventListener('change', () => {
  if (selAll.checked) segments.forEach(s => deletedIds.add(s._id));
  else deletedIds.clear();
  saveLS();
  render();
  updateInfo();
});

// ── save & close ─────────────────────────────────────────────────────────────
document.getElementById('btnSave').addEventListener('click', async () => {
  // Flush any in-progress contenteditable edit before saving
  const active = document.activeElement;
  if (active && active.classList.contains('sub-text')) active.blur();

  const toSave = segments
    .filter(s => !deletedIds.has(s._id))
    .map(({ _id, ...rest }) => rest);

  const res = await fetch('/api/transcript', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ segments: toSave })
  });

  if (res.ok) {
    localStorage.removeItem(LS_KEY);
    statusTxt.textContent = `✅ 已保存 ${toSave.length} 条字幕，可以关闭此标签页`;
    window.close(); // may be blocked by browser; user can close manually
  } else {
    statusTxt.textContent = '保存失败，请重试';
  }
});
</script>
</body>
</html>"""


# ----------------------------------------------------------------------------- #
# Routes
# ----------------------------------------------------------------------------- #
@app.route("/")
def index():
    return Response(HTML_TEMPLATE, content_type="text/html; charset=utf-8")


@app.route("/video")
def video():
    if not os.path.exists(VIDEO_PATH):
        return "Video not found", 404
    return send_file(VIDEO_PATH, mimetype="video/mp4")


@app.route("/api/transcript", methods=["GET"])
def get_transcript():
    with open(TRANSCRIPT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    # Support both {"segments": [...]} and plain [...]
    segs = data.get("segments", data) if isinstance(data, dict) else data
    return jsonify({"segments": segs})


@app.route("/api/transcript", methods=["POST"])
def post_transcript():
    body = request.get_json()
    output = {"segments": body["segments"]}
    with open(TRANSCRIPT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    RESULT_DONE.set()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------- #
# Main
# ----------------------------------------------------------------------------- #
def main():
    global VIDEO_PATH, TRANSCRIPT_PATH

    if len(sys.argv) < 3:
        print("Usage: preview_editor.py <video.mp4> <transcript.json>")
        sys.exit(1)

    VIDEO_PATH      = os.path.abspath(sys.argv[1])
    TRANSCRIPT_PATH = os.path.abspath(sys.argv[2])

    for p in [VIDEO_PATH, TRANSCRIPT_PATH]:
        if not Path(p).exists():
            print(f"❌ File not found: {p}")
            sys.exit(1)

    url = f"http://localhost:{PORT}"
    print(f"[preview] Video:     {VIDEO_PATH}")
    print(f"[preview] Transcript: {TRANSCRIPT_PATH}")
    print(f"[preview] Opening  {url} …")
    webbrowser.open(url)

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    print("[preview] Exiting.")


if __name__ == "__main__":
    main()
