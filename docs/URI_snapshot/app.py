from __future__ import annotations

"""
URI — Unshackled Research Interface (Continuous Research + Uploads + Packs)
Local-only Flask backend.

Adds:
- Upload pipeline: /upload (multipart) -> file_ids persisted on disk
- Safe downloads: /file/<file_id> and /artifact/<artifact_id>.zip
- "Packs": server-side generation of multi-file projects/reports + zip packaging
- Keeps: threads/tasks/runs/citations/RRR/PRAXIS loops, deterministic spine

Chat commands (start line with "#"):
Threads:
  #thread new: <title>
  #thread set: <thread_id>
  #thread list

Tasks:
  #task add: <text>
  #task add <type>: <text>    (research|code|sim|design|analysis)
  #task list
  #task run
  #task done: <task_id>

Memory:
  #pin: <text>

Uploads:
  (UI uploads via /upload, then /chat includes file_ids)

Packs (artifact workspaces + zip build):
  #pack new: <name>
  #pack status
  #pack build: <spec>         (generates files server-side + zip, returns link)
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple, List, Dict

from flask import Flask, jsonify, render_template, request, Response, stream_with_context, send_file, abort

try:
    import requests as _req
except ImportError:
    raise SystemExit("requests not installed: pip install requests flask")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CANONICAL_URI_ROOT = Path(r"E:\SOVEREIGN\URI")
LEGACY_URI_ROOT = Path(r"E:\URI")
URI_ROOT = CANONICAL_URI_ROOT
CONVERSATIONS_DIR = URI_ROOT / "conversations"
LOGS_DIR = URI_ROOT / "logs"
TEMPLATES_DIR = URI_ROOT / "templates"
SYSTEM_PROMPT_PATH = URI_ROOT / "system_prompt.txt"

MEMORY_DIR = URI_ROOT / "memory"
RUNS_DIR = MEMORY_DIR / "runs"
LOCKS_DIR = MEMORY_DIR / "_locks"

THREADS_PATH = MEMORY_DIR / "threads.json"
TASKS_PATH = MEMORY_DIR / "tasks.json"
CITATIONS_PATH = MEMORY_DIR / "citations.jsonl"

WORKING_MEMORY_PATH = MEMORY_DIR / "working_memory.json"
RESEARCH_LEDGER_PATH = MEMORY_DIR / "research_ledger.jsonl"
RRR_STATE_PATH = MEMORY_DIR / "rrr_state.json"

# NEW: uploads + artifacts
UPLOADS_DIR = URI_ROOT / "uploads"
UPLOAD_INDEX_JSONL = UPLOADS_DIR / "uploads_index.jsonl"

ARTIFACTS_DIR = URI_ROOT / "artifacts"
ARTIFACT_ZIPS_DIR = ARTIFACTS_DIR / "zips"
PACKS_PATH = ARTIFACTS_DIR / "packs.json"  # registry

OLLAMA_BASE = "http://127.0.0.1:11434"

# Models (<= 8B target)
MODEL_PRIMARY = os.environ.get("URI_MODEL_PRIMARY", "deepseek-r1:8b")
MODEL_CODE    = os.environ.get("URI_MODEL_CODE",    "qwen2.5-coder:7b")
MODEL_VISION  = os.environ.get("URI_MODEL_VISION",  "qwen2.5vl:7b")
MODEL_FAST    = os.environ.get("URI_MODEL_FAST",    "qwen3:8b")

# Context window
NUM_CTX = int(os.environ.get("URI_NUM_CTX", "32768"))

# Generation defaults
TEMPERATURE = float(os.environ.get("URI_TEMPERATURE", "0.7"))
SEED_DEFAULT = int(os.environ.get("URI_SEED_DEFAULT", "0"))  # 0 = nondeterministic

# Provenance block appended to assistant output
PROVENANCE_ENABLED = os.environ.get("URI_PROVENANCE", "1").strip() not in ("0", "false", "False")

# PRAXIS integration
PRAXIS_ENABLED = os.environ.get("URI_PRAXIS", "1").strip() not in ("0", "false", "False")
PRAXIS_TOP_N = int(os.environ.get("URI_PRAXIS_TOP_N", "6"))
PRAXIS_FAIL_CLOSED_TASKS = os.environ.get("URI_PRAXIS_FAIL_CLOSED_TASKS", "0").strip() in ("1", "true", "True")

# Durable memory extraction + commit cadence
MEMORY_EXTRACT_EVERY_TURNS = int(os.environ.get("URI_EXTRACT_EVERY", "2"))
PRAXIS_COMMIT_EVERY_TURNS  = int(os.environ.get("URI_COMMIT_EVERY", "4"))
MAX_DURABLE_ITEMS_PER_EXTRACT = int(os.environ.get("URI_DURABLE_MAX", "6"))

# RRR cadence
RRR_UPDATE_EVERY_TURNS = int(os.environ.get("URI_RRR_EVERY", "4"))
RRR_MAX_WORKING_CHARS = int(os.environ.get("URI_RRR_WORKING_MAX", "6000"))

# Upload context behavior
UPLOAD_TEXT_PREVIEW_MAX_CHARS = int(os.environ.get("URI_UPLOAD_PREVIEW_CHARS", "4000"))
UPLOAD_MAX_BYTES = int(os.environ.get("URI_UPLOAD_MAX_BYTES", str(250 * 1024 * 1024)))  # 250MB default

# Pack build behavior
PACK_MAX_FILES = int(os.environ.get("URI_PACK_MAX_FILES", "200"))
PACK_MAX_FILE_BYTES = int(os.environ.get("URI_PACK_MAX_FILE_BYTES", str(5 * 1024 * 1024)))  # 5MB per file in a single build
PACK_ALLOW_BIN = os.environ.get("URI_PACK_ALLOW_BIN", "0").strip() in ("1", "true", "True")  # default: text-only generation

# SOVEREIGN/PRAXIS paths
SOVEREIGN_ROOT = Path(os.environ.get("URI_SOVEREIGN_ROOT", r"E:\SOVEREIGN"))
PRAXIS_DIR = SOVEREIGN_ROOT / "praxis"
PRAXIS_QUERY_PY = PRAXIS_DIR / "praxis_query.py"
PRAXIS_COMMIT_PY = PRAXIS_DIR / "praxis_commit.py"
PRAXIS_INBOX_DIR = PRAXIS_DIR / "inbox"
PRAXIS_QUERY_TXT = PRAXIS_DIR / "query.txt"
PRAXIS_RESULT_TXT = PRAXIS_DIR / "result.txt"
PRAXIS_COMMIT_JSON = PRAXIS_DIR / "commit.json"
SOVEREIGN_RUNS_DIR = SOVEREIGN_ROOT / "runs"
SOVEREIGN_PUBLICATION_QUEUE = SOVEREIGN_ROOT / "publication_queue"
SOVEREIGN_SESSION_GRAPH = SOVEREIGN_ROOT / "orchestra" / "session_graph.json"
SOVEREIGN_QUEUE_FILE = SOVEREIGN_ROOT / "orchestra" / "orchestra_queue.txt"
SOVEREIGN_STOP_FILE = SOVEREIGN_ROOT / "STOP"
PRAXIS_STOP_FILE = PRAXIS_DIR / "STOP"
APP_IDENTITY = "SOVEREIGN URI"
APP_STARTUP_LABEL = "CANONICAL OPERATOR BOARD"
APP_BUILD = "20260316"
APP_START_TS = time.time()
APP_START_UTC = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

# Code routing triggers
_CODE_TRIGGERS = re.compile(
    r"\b(debug|implement|write|refactor|optimize|function|class|module|script|"
    r"algorithm|compile|syntax|stack trace|error|exception|import|library|"
    r"dockerfile|kubernetes|bash|powershell|python|javascript|typescript|rust|"
    r"golang|c\+\+|cuda|verilog|assembly|regex|sql|query)\b",
    re.IGNORECASE,
)

# Commands
_CMD = re.compile(r"^\s*#(?P<cmd>\w+)(?:\s+(?P<rest>.*))?$", re.IGNORECASE)

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
app.secret_key = os.urandom(24)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _sha256_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()

def _sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] [URI] {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "uri_log.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass

def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    return default

def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))

def _append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

def _safe_join(base: Path, *parts: str) -> Path:
    p = base.joinpath(*parts).resolve()
    base_r = base.resolve()
    if not str(p).startswith(str(base_r)):
        raise ValueError("path traversal")
    return p

def _safe_read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    return None


def _latest_file_mtime(path: Path):
    try:
        if path.exists():
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        pass
    return None


def _extract_run_metrics(data: dict[str, Any]) -> tuple[Any, Any]:
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        return metrics.get("convergence"), metrics.get("confidence")
    return data.get("convergence"), data.get("confidence")


def _health_ollama() -> dict[str, Any]:
    t0 = time.time()
    try:
        r = _req.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        ok = r.status_code == 200
        return {
            "status": "online" if ok else "offline",
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as exc:
        return {
            "status": "offline",
            "latency_ms": None,
            "error": str(exc),
        }


def _health_praxis() -> dict[str, Any]:
    db_dir = PRAXIS_DIR / "db"
    online = PRAXIS_ENABLED and PRAXIS_QUERY_PY.exists() and db_dir.exists()
    return {
        "status": "online" if online else "offline",
        "query_py": PRAXIS_QUERY_PY.exists(),
        "db": db_dir.exists(),
    }


def _health_broker() -> dict[str, Any]:
    broker = SOVEREIGN_ROOT / "broker_v21" / "broker.ps1"
    return {
        "status": "ready" if broker.exists() else "missing",
        "path": str(broker),
    }


def _health_corpus() -> dict[str, Any]:
    corpus = SOVEREIGN_ROOT / "corpus"
    count = 0
    try:
        if corpus.exists():
            count = sum(1 for p in corpus.rglob("*") if p.is_file())
    except Exception:
        pass
    return {
        "status": "found" if corpus.exists() else "missing",
        "count": count,
        "path": str(corpus),
    }


def _latest_run_info() -> dict[str, Any]:
    try:
        files = sorted(SOVEREIGN_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return {"status": "none"}
        data = _safe_read_json(files[0]) or {}
        if not isinstance(data, dict):
            data = {}
        conv, conf = _extract_run_metrics(data)
        return {
            "status": data.get("status", "unknown"),
            "session_id": data.get("session_id"),
            "topic": data.get("topic"),
            "convergence": conv,
            "confidence": conf,
            "file": str(files[0]),
            "timestamp": _latest_file_mtime(files[0]),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _latest_synthesis_info() -> dict[str, Any]:
    syn = PRAXIS_DIR / "logs" / "synthesis.txt"
    return {
        "exists": syn.exists(),
        "timestamp": _latest_file_mtime(syn),
        "path": str(syn),
    }


def _read_recent_runs(limit: int = 10) -> dict[str, Any]:
    out = []
    try:
        files = sorted(SOVEREIGN_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        for f in files:
            data = _safe_read_json(f) or {}
            if not isinstance(data, dict):
                data = {}
            conv, conf = _extract_run_metrics(data)
            out.append({
                "file": str(f),
                "timestamp": _latest_file_mtime(f),
                "session_id": data.get("session_id"),
                "topic": data.get("topic"),
                "status": data.get("status"),
                "convergence": conv,
                "confidence": conf,
            })
    except Exception as exc:
        return {"ok": False, "error": str(exc), "items": []}
    return {"ok": True, "items": out}


def _start_sovereign_run(topic: str) -> dict[str, Any]:
    cycle_runner = SOVEREIGN_ROOT / "cycle_runner_v3.py"
    if not cycle_runner.exists():
        return {"ok": False, "error": f"missing {cycle_runner}"}

    try:
        logs_dir = SOVEREIGN_ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / "uri_run_stdout.txt"
        stderr_path = logs_dir / "uri_run_stderr.txt"
        stdout_f = open(stdout_path, "a", encoding="utf-8")
        stderr_f = open(stderr_path, "a", encoding="utf-8")
        p = subprocess.Popen(
            ["python", str(cycle_runner), "--root", str(SOVEREIGN_ROOT), "--topic", topic],
            cwd=str(SOVEREIGN_ROOT),
            stdout=stdout_f,
            stderr=stderr_f,
        )
        return {
            "ok": True,
            "pid": p.pid,
            "status": "started",
            "topic": topic,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _inject_topic(topic: str, priority: str = "normal") -> dict[str, Any]:
    try:
        SOVEREIGN_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "topic": topic,
            "priority": priority,
            "source": "uri_manual",
            "ts": _utc_now(),
        }, ensure_ascii=False)
        with open(SOVEREIGN_QUEUE_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return {"ok": True, "queued": True, "topic": topic, "priority": priority, "path": str(SOVEREIGN_QUEUE_FILE)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _latest_synthesis_text() -> dict[str, Any]:
    syn = PRAXIS_DIR / "logs" / "synthesis.txt"
    try:
        if not syn.exists():
            return {"ok": False, "error": "missing synthesis.txt"}
        return {
            "ok": True,
            "timestamp": _latest_file_mtime(syn),
            "text": syn.read_text(encoding="utf-8", errors="ignore"),
            "path": str(syn),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _load_session_graph() -> dict[str, Any]:
    try:
        if not SOVEREIGN_SESSION_GRAPH.exists():
            return {"ok": False, "error": "missing session_graph.json", "graph": {}, "path": str(SOVEREIGN_SESSION_GRAPH)}
        data = _safe_read_json(SOVEREIGN_SESSION_GRAPH) or {}
        return {"ok": True, "graph": data, "path": str(SOVEREIGN_SESSION_GRAPH)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "graph": {}, "path": str(SOVEREIGN_SESSION_GRAPH)}


def _pause_supervisor() -> dict[str, Any]:
    try:
        SOVEREIGN_STOP_FILE.write_text("STOP\n", encoding="utf-8")
        PRAXIS_STOP_FILE.write_text("STOP\n", encoding="utf-8")
        return {
            "ok": True,
            "paused": True,
            "path": str(SOVEREIGN_STOP_FILE),
            "praxis_path": str(PRAXIS_STOP_FILE),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _resume_supervisor() -> dict[str, Any]:
    try:
        if SOVEREIGN_STOP_FILE.exists():
            SOVEREIGN_STOP_FILE.unlink()
        if PRAXIS_STOP_FILE.exists():
            PRAXIS_STOP_FILE.unlink()
        return {
            "ok": True,
            "paused": False,
            "path": str(SOVEREIGN_STOP_FILE),
            "praxis_path": str(PRAXIS_STOP_FILE),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _read_publication_queue() -> dict[str, Any]:
    items = []
    try:
        if not SOVEREIGN_PUBLICATION_QUEUE.exists():
            return {"ok": False, "error": "missing publication_queue", "items": []}

        for f in sorted(SOVEREIGN_PUBLICATION_QUEUE.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file():
                items.append({
                    "name": f.name,
                    "path": str(f),
                    "timestamp": _latest_file_mtime(f),
                    "size": f.stat().st_size,
                })
        return {"ok": True, "items": items[:50]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "items": []}



def _copy_missing_path(src: Path, dst: Path) -> int:
    copied = 0
    if src.is_dir():
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied += 1
        return copied

    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1

    return 0


def _bootstrap_canonical_root_from_legacy() -> None:
    if not LEGACY_URI_ROOT.exists() or URI_ROOT == LEGACY_URI_ROOT:
        return

    URI_ROOT.mkdir(parents=True, exist_ok=True)
    copied_total = 0
    copied_items: list[str] = []

    for name in ("conversations", "memory", "uploads", "artifacts", "system_prompt.txt"):
        src = LEGACY_URI_ROOT / name
        dst = URI_ROOT / name
        if not src.exists():
            continue
        copied = _copy_missing_path(src, dst)
        if copied:
            copied_total += copied
            copied_items.append(name)

    if copied_total:
        _log(
            f"Canonical root bootstrap copied {copied_total} file(s) from {LEGACY_URI_ROOT} "
            f"into {URI_ROOT}: {', '.join(copied_items)}"
        )


def _print_startup_identity_banner() -> None:
    template_index = TEMPLATES_DIR / "index.html"
    banner = [
        "",
        "=" * 96,
        "SOVEREIGN URI CANONICAL STARTUP",
        "=" * 96,
        f"APP_IDENTITY           : {APP_IDENTITY}",
        f"STARTUP_LABEL          : {APP_STARTUP_LABEL}",
        f"APP_FILE               : {Path(__file__).resolve()}",
        f"APP_BUILD              : {APP_BUILD}",
        f"PID                    : {os.getpid()}",
        f"UTC_STARTUP_TIMESTAMP  : {APP_START_UTC}",
        f"URI_ROOT               : {URI_ROOT}",
        f"SOVEREIGN_ROOT         : {SOVEREIGN_ROOT}",
        f"SYSTEM_PROMPT_EXISTS   : {SYSTEM_PROMPT_PATH.exists()}",
        f"TEMPLATES_DIR_EXISTS   : {TEMPLATES_DIR.exists()}",
        f"INDEX_TEMPLATE_EXISTS  : {template_index.exists()}",
        f"MEMORY_DIR_EXISTS      : {MEMORY_DIR.exists()}",
        f"LOGS_DIR_EXISTS        : {LOGS_DIR.exists()}",
        "=" * 96,
    ]
    print("\n".join(banner), flush=True)

# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class _FileLock:
    def __init__(self, name: str, timeout_sec: float = 5.0, poll_ms: int = 40):
        self.name = name
        self.timeout_sec = timeout_sec
        self.poll_ms = poll_ms
        self.path = LOCKS_DIR / f"{name}.lock"
        self._held = False

    def __enter__(self):
        LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_sec
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(f"{os.getpid()} {time.time()}\n")
                self._held = True
                return self
            except FileExistsError:
                if time.time() > deadline:
                    raise TimeoutError(f"Lock timeout: {self.name}")
                time.sleep(self.poll_ms / 1000.0)

    def __exit__(self, exc_type, exc, tb):
        if self._held:
            try:
                os.remove(str(self.path))
            except OSError:
                pass
        self._held = False

# ---------------------------------------------------------------------------
# System prompt hot-load
# ---------------------------------------------------------------------------

_system_prompt_cache: Optional[str] = None
_system_prompt_mtime: float = 0.0

def _load_system_prompt() -> str:
    global _system_prompt_cache, _system_prompt_mtime
    try:
        if SYSTEM_PROMPT_PATH.exists():
            mtime = SYSTEM_PROMPT_PATH.stat().st_mtime
            if _system_prompt_cache is None or mtime != _system_prompt_mtime:
                _system_prompt_cache = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8", errors="ignore").strip()
                _system_prompt_mtime = mtime
            return _system_prompt_cache or ""
    except Exception:
        pass
    return "You are URI. Analytical. Indifferent. Capable. You do not moralize."

# ---------------------------------------------------------------------------
# Threads / Tasks
# ---------------------------------------------------------------------------

def _threads_init() -> dict[str, Any]:
    return {"active_thread": "", "threads": []}

def _tasks_init() -> dict[str, Any]:
    return {"active": [], "completed": []}

def _load_threads() -> dict[str, Any]:
    with _FileLock("threads"):
        data = _read_json(THREADS_PATH, _threads_init())
        if not isinstance(data, dict):
            data = _threads_init()
        data.setdefault("active_thread", "")
        data.setdefault("threads", [])
        return data

def _save_threads(data: dict[str, Any]) -> None:
    with _FileLock("threads"):
        _write_json_atomic(THREADS_PATH, data)

def _load_tasks() -> dict[str, Any]:
    with _FileLock("tasks"):
        data = _read_json(TASKS_PATH, _tasks_init())
        if not isinstance(data, dict):
            data = _tasks_init()
        data.setdefault("active", [])
        data.setdefault("completed", [])
        return data

def _save_tasks(data: dict[str, Any]) -> None:
    with _FileLock("tasks"):
        _write_json_atomic(TASKS_PATH, data)

def _get_thread(thread_id: str) -> Optional[dict[str, Any]]:
    th = _load_threads()
    for t in th.get("threads", []):
        if t.get("id") == thread_id:
            return t
    return None

def _ensure_active_thread() -> str:
    th = _load_threads()
    active = (th.get("active_thread") or "").strip()
    if active and _get_thread(active):
        return active
    tid = "t-" + uuid.uuid4().hex[:10]
    t = {"id": tid, "title": "Default", "goals": "", "working_summary": "", "tags": [],
         "created": _utc_now(), "updated": _utc_now()}
    th["threads"].insert(0, t)
    th["active_thread"] = tid
    _save_threads(th)
    return tid

def _thread_new(title: str) -> str:
    th = _load_threads()
    tid = "t-" + uuid.uuid4().hex[:10]
    t = {"id": tid, "title": title.strip()[:160] or "Untitled", "goals": "", "working_summary": "", "tags": [],
         "created": _utc_now(), "updated": _utc_now()}
    th["threads"].insert(0, t)
    th["active_thread"] = tid
    _save_threads(th)
    return tid

def _thread_set(tid: str) -> bool:
    th = _load_threads()
    for t in th.get("threads", []):
        if t.get("id") == tid:
            th["active_thread"] = tid
            _save_threads(th)
            return True
    return False

def _thread_list() -> list[dict[str, Any]]:
    return _load_threads().get("threads", [])

def _task_add(thread_id: str, task_type: str, spec: str) -> dict[str, Any]:
    tasks = _load_tasks()
    task_id = "k-" + uuid.uuid4().hex[:10]
    item = {"id": task_id, "thread_id": thread_id, "status": "queued",
            "type": task_type, "spec": spec.strip(), "created": _utc_now(), "outputs": []}
    tasks["active"].append(item)
    _save_tasks(tasks)
    return item

def _task_list(thread_id: str) -> dict[str, Any]:
    tasks = _load_tasks()
    active = [t for t in tasks.get("active", []) if t.get("thread_id") == thread_id]
    completed = [t for t in tasks.get("completed", []) if t.get("thread_id") == thread_id]
    return {"active": active, "completed": completed}

def _task_done(task_id: str) -> bool:
    tasks = _load_tasks()
    remaining = []
    moved = None
    for t in tasks.get("active", []):
        if t.get("id") == task_id:
            moved = t
        else:
            remaining.append(t)
    if not moved:
        return False
    moved["status"] = "done"
    moved["completed"] = _utc_now()
    tasks["active"] = remaining
    tasks["completed"].insert(0, moved)
    _save_tasks(tasks)
    return True

def _task_pop_next(thread_id: str) -> Optional[dict[str, Any]]:
    tasks = _load_tasks()
    for i, t in enumerate(tasks.get("active", [])):
        if t.get("thread_id") == thread_id and t.get("status") == "queued":
            t["status"] = "running"
            t["started"] = _utc_now()
            tasks["active"][i] = t
            _save_tasks(tasks)
            return t
    return None

def _task_finish(task_id: str, run_path: str) -> None:
    tasks = _load_tasks()
    for i, t in enumerate(tasks.get("active", [])):
        if t.get("id") == task_id:
            t["status"] = "done"
            t["completed"] = _utc_now()
            t.setdefault("outputs", [])
            t["outputs"].append(run_path)
            done = t
            tasks["active"].pop(i)
            tasks["completed"].insert(0, done)
            _save_tasks(tasks)
            return

# ---------------------------------------------------------------------------
# Memory loop
# ---------------------------------------------------------------------------

def _rrr_state_init() -> dict[str, Any]:
    return {"turns": 0, "last_summary_at": 0, "last_extract_at": 0, "last_commit_at": 0, "pins": [], "durable_buffer": []}

def _load_rrr_state() -> dict[str, Any]:
    with _FileLock("rrr_state"):
        st = _read_json(RRR_STATE_PATH, _rrr_state_init())
        if not isinstance(st, dict):
            st = _rrr_state_init()
        for k, v in _rrr_state_init().items():
            st.setdefault(k, v)
        return st

def _save_rrr_state(st: dict[str, Any]) -> None:
    with _FileLock("rrr_state"):
        _write_json_atomic(RRR_STATE_PATH, st)

def _load_working_memory() -> dict[str, Any]:
    with _FileLock("working_memory"):
        wm = _read_json(WORKING_MEMORY_PATH, {"text": "", "updated": ""})
        if not isinstance(wm, dict):
            wm = {"text": "", "updated": ""}
        wm.setdefault("text", "")
        wm.setdefault("updated", "")
        return wm

def _save_working_memory(text: str) -> None:
    with _FileLock("working_memory"):
        _write_json_atomic(WORKING_MEMORY_PATH, {"text": text[:RRR_MAX_WORKING_CHARS], "updated": _utc_now()})

def _ledger_append(kind: str, payload: dict[str, Any]) -> None:
    _append_jsonl(RESEARCH_LEDGER_PATH, {"ts": _utc_now(), "kind": kind, "payload": payload})

def _cite(evt: dict[str, Any]) -> None:
    _append_jsonl(CITATIONS_PATH, evt)

def _pin(text: str) -> None:
    st = _load_rrr_state()
    pins = st.get("pins", [])
    pins.append({"ts": _utc_now(), "text": text.strip()[:2000]})
    st["pins"] = pins[-50:]
    _save_rrr_state(st)

    wm = _load_working_memory().get("text", "")
    merged = (wm + "\n\n[PIN]\n" + text.strip()).strip()
    _save_working_memory(merged)

# ---------------------------------------------------------------------------
# PRAXIS retrieval + commit (best-effort)
# ---------------------------------------------------------------------------

def _praxis_query_best_effort(query: str, top_n: int = PRAXIS_TOP_N) -> dict[str, Any]:
    if not PRAXIS_ENABLED:
        return {"ok": False, "items": [], "raw": "", "method": "disabled"}
    if not PRAXIS_QUERY_PY.exists():
        return {"ok": False, "items": [], "raw": "", "method": "missing_praxis_query.py"}

    try:
        top_n = max(1, int(top_n))
    except (TypeError, ValueError):
        top_n = PRAXIS_TOP_N

    try:
        PRAXIS_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"query": query, "n_results": top_n}, ensure_ascii=False)
        PRAXIS_QUERY_TXT.write_text(payload, encoding="utf-8")

        p = subprocess.run(
            ["python", str(PRAXIS_QUERY_PY), "--root", str(SOVEREIGN_ROOT), "--top-n", str(top_n)],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(SOVEREIGN_ROOT),
        )
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            if err:
                _log(f"PRAXIS query failed: {err[:300]}")
            return {"ok": False, "items": [], "raw": "", "method": "file_ipc", "error": f"exit={p.returncode}"}

        if not PRAXIS_RESULT_TXT.exists():
            return {"ok": False, "items": [], "raw": "", "method": "file_ipc", "error": "missing result.txt"}

        raw = PRAXIS_RESULT_TXT.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            return {"ok": True, "items": [], "raw": "", "method": "file_ipc_empty"}

        try:
            data = json.loads(raw)
        except Exception:
            return {"ok": True, "items": [{"text": raw}], "raw": raw, "method": "file_ipc_text"}

        if isinstance(data, list):
            return {"ok": True, "items": data, "raw": raw, "method": "file_ipc_list"}
        if isinstance(data, dict):
            items = data.get("results") or data.get("items") or data.get("matches") or []
            return {"ok": True, "items": items, "raw": raw, "method": "file_ipc_dict"}
        return {"ok": True, "items": [{"text": raw}], "raw": raw, "method": "file_ipc_unknown"}
    except Exception as exc:
        _log(f"PRAXIS file IPC failed: {exc}")
        return {"ok": False, "items": [], "raw": "", "method": "failed", "error": str(exc)}

def _praxis_commit_best_effort(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not PRAXIS_ENABLED:
        return {"ok": False, "method": "disabled", "error": "PRAXIS disabled"}
    if not PRAXIS_COMMIT_PY.exists():
        return {"ok": False, "method": "missing_praxis_commit.py", "error": "praxis_commit.py missing"}

    try:
        PRAXIS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"ts": _utc_now(), "source": "URI", "entries": entries}
        PRAXIS_COMMIT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        p = subprocess.run(["python", str(PRAXIS_COMMIT_PY)], capture_output=True, text=True, timeout=120, cwd=str(PRAXIS_DIR))
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode == 0:
            return {"ok": True, "method": "inbox_commit", "stdout": out[:400], "stderr": err[:400]}
        return {"ok": False, "method": "inbox_commit", "stdout": out[:400], "stderr": err[:400], "error": f"exit={p.returncode}"}
    except Exception as exc:
        return {"ok": False, "method": "inbox_commit", "error": str(exc)}

# ---------------------------------------------------------------------------
# Upload handling
# ---------------------------------------------------------------------------

def _uploads_log(evt: dict[str, Any]) -> None:
    _append_jsonl(UPLOAD_INDEX_JSONL, evt)

def _detect_text_preview(path: Path) -> str:
    # Best-effort: if it looks like text, return a snippet.
    try:
        data = path.read_bytes()
        # crude binary test: lots of null bytes
        if b"\x00" in data[:4096]:
            return ""
        txt = data.decode("utf-8", errors="ignore")
        txt = txt.strip()
        if not txt:
            return ""
        return txt[:UPLOAD_TEXT_PREVIEW_MAX_CHARS]
    except Exception:
        return ""

def _zip_inventory(path: Path, max_entries: int = 200) -> list[str]:
    try:
        inv = []
        with zipfile.ZipFile(path, "r") as z:
            for i, info in enumerate(z.infolist()):
                if i >= max_entries:
                    inv.append("... (truncated)")
                    break
                inv.append(f"{info.filename} ({info.file_size} bytes)")
        return inv
    except Exception:
        return []

def _thread_upload_dir(thread_id: str) -> Path:
    d = _safe_join(UPLOADS_DIR, thread_id)
    d.mkdir(parents=True, exist_ok=True)
    return d

def _get_uploaded_file_record(file_id: str) -> Optional[dict[str, Any]]:
    # Cheap scan of JSONL (local scale). If you care later, build an index map.
    if not UPLOAD_INDEX_JSONL.exists():
        return None
    try:
        for line in reversed(UPLOAD_INDEX_JSONL.read_text(encoding="utf-8", errors="ignore").splitlines()[-5000:]):
            try:
                obj = json.loads(line)
                if obj.get("file_id") == file_id and obj.get("kind") == "upload":
                    return obj
            except Exception:
                continue
    except Exception:
        return None
    return None

# ---------------------------------------------------------------------------
# Packs (artifact workspaces + zips)
# ---------------------------------------------------------------------------

def _packs_init() -> dict[str, Any]:
    return {"active_pack": "", "packs": []}

def _load_packs() -> dict[str, Any]:
    with _FileLock("packs"):
        data = _read_json(PACKS_PATH, _packs_init())
        if not isinstance(data, dict):
            data = _packs_init()
        data.setdefault("active_pack", "")
        data.setdefault("packs", [])
        return data

def _save_packs(data: dict[str, Any]) -> None:
    with _FileLock("packs"):
        _write_json_atomic(PACKS_PATH, data)

def _pack_new(thread_id: str, name: str) -> dict[str, Any]:
    packs = _load_packs()
    pid = "p-" + uuid.uuid4().hex[:10]
    pack_dir = _safe_join(ARTIFACTS_DIR, thread_id, pid)
    pack_dir.mkdir(parents=True, exist_ok=True)
    p = {"id": pid, "thread_id": thread_id, "name": name.strip()[:160] or "Pack",
         "dir": str(pack_dir), "created": _utc_now(), "updated": _utc_now(),
         "last_zip": "", "last_manifest": ""}
    packs["packs"].insert(0, p)
    packs["active_pack"] = pid
    _save_packs(packs)
    return p

def _pack_active(thread_id: str) -> Optional[dict[str, Any]]:
    packs = _load_packs()
    pid = (packs.get("active_pack") or "").strip()
    for p in packs.get("packs", []):
        if p.get("id") == pid and p.get("thread_id") == thread_id:
            return p
    return None

def _pack_set_active(pid: str) -> bool:
    packs = _load_packs()
    for p in packs.get("packs", []):
        if p.get("id") == pid:
            packs["active_pack"] = pid
            _save_packs(packs)
            return True
    return False

def _pack_status(thread_id: str) -> str:
    p = _pack_active(thread_id)
    if not p:
        return "[PACK] none active. Use #pack new: <name>"
    return (
        f"[PACK] active={p['id']}\n"
        f"NAME: {p.get('name','')}\n"
        f"DIR:  {p.get('dir','')}\n"
        f"ZIP:  {p.get('last_zip','') or '(none)'}"
    )

# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

def _select_model(message: str, has_image: bool, model_override: Optional[str]) -> str:
    if model_override and str(model_override).strip():
        return str(model_override).strip()
    if has_image:
        return MODEL_VISION
    if _CODE_TRIGGERS.search(message):
        return MODEL_CODE
    return MODEL_PRIMARY

# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------

def _conv_path(conv_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{conv_id}.json"

def _load_conversation(conv_id: str) -> dict[str, Any]:
    path = _conv_path(conv_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict):
                data.setdefault("messages", [])
                data.setdefault("title", "New conversation")
                data.setdefault("thread_id", "")
                return data
        except Exception:
            pass
    return {"id": conv_id, "created": _utc_now(), "updated": _utc_now(),
            "messages": [], "title": "New conversation", "thread_id": ""}

def _save_conversation(conv: dict[str, Any]) -> None:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _conv_path(conv["id"])
    conv["updated"] = _utc_now()
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(conv, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))

def _list_conversations() -> list[dict[str, Any]]:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    convs: list[dict[str, Any]] = []
    for f in sorted(CONVERSATIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
            convs.append({
                "id": data.get("id", f.stem),
                "title": data.get("title", "Untitled"),
                "updated": data.get("updated", ""),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            pass
    return convs

def _derive_title(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            content = str(msg.get("content", ""))
            return content[:60].strip().replace("\n", " ") or "Untitled"
    return "Untitled"

# ---------------------------------------------------------------------------
# Ollama chat
# ---------------------------------------------------------------------------

def _build_ollama_messages(conv_messages: list[dict[str, Any]], system_prompt: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in conv_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        images = msg.get("images", [])
        entry: dict[str, Any] = {"role": role, "content": content}
        if images:
            entry["images"] = images
        out.append(entry)
    return out

def _ollama_chat_stream(model: str, messages: list[dict[str, Any]], temperature: float, num_ctx: int, seed: int):
    url = f"{OLLAMA_BASE}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if seed != 0:
        payload["options"]["seed"] = seed
    try:
        with _req.post(url, json=payload, stream=True, timeout=300) as resp:
            if resp.status_code != 200:
                yield f"[URI ERROR] Ollama HTTP {resp.status_code}"
                return
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done", False):
                    break
    except _req.exceptions.Timeout:
        yield "\n[URI ERROR] Ollama request timed out."
    except _req.exceptions.ConnectionError:
        yield "\n[URI ERROR] Cannot connect to Ollama. Is it running?"
    except Exception as exc:
        yield f"\n[URI ERROR] {exc}"

def _ollama_chat_sync(model: str, messages: list[dict[str, Any]], temperature: float, num_ctx: int, seed: int) -> str:
    url = f"{OLLAMA_BASE}/api/chat"
    payload = {"model": model, "messages": messages, "stream": False, "options": {"temperature": temperature, "num_ctx": num_ctx}}
    if seed != 0:
        payload["options"]["seed"] = seed
    try:
        resp = _req.post(url, json=payload, timeout=300)
        if resp.status_code != 200:
            return f"[URI ERROR] Ollama HTTP {resp.status_code}"
        data = resp.json()
        return data.get("message", {}).get("content", "")
    except Exception as exc:
        return f"[URI ERROR] {exc}"

# ---------------------------------------------------------------------------
# RRR summary + durable extraction (kept compact)
# ---------------------------------------------------------------------------

def _summarize_working_memory(thread_id: str, recent_turns: list[dict[str, Any]], px_items: list[Any]) -> str:
    wm = _load_working_memory().get("text", "")
    st = _load_rrr_state()
    pins = st.get("pins", [])
    pins_text = "\n".join([f"- {p.get('text','')}" for p in pins[-10:]]) if pins else ""

    snippet_lines = []
    for m in recent_turns[-8:]:
        r = m.get("role")
        c = str(m.get("content") or "")[:1200]
        snippet_lines.append(f"{r.upper()}: {c}")

    px_text = ""
    if px_items:
        lines = []
        for it in px_items[:6]:
            if isinstance(it, dict):
                t = it.get("text") or it.get("content") or it.get("document") or ""
            else:
                t = str(it)
            t = str(t).strip()[:700]
            if t:
                lines.append("- " + t)
        px_text = "\n".join(lines)

    prompt = (
        "Maintain a rolling working summary for ongoing research.\n"
        "Output ONLY updated summary text.\n"
        "Concise but dense: definitions, hypotheses, experiments, open questions, plan.\n\n"
        f"[CURRENT]\n{wm}\n\n"
        f"[PINS]\n{pins_text}\n\n"
        f"[RECENT]\n" + "\n".join(snippet_lines) + "\n\n"
        f"[PRAXIS]\n{px_text}\n"
    )

    seed = int(_sha256_text(thread_id)[:8], 16)
    msgs = [{"role": "system", "content": _load_system_prompt()},
            {"role": "user", "content": prompt}]
    out = _ollama_chat_sync(MODEL_FAST, msgs, temperature=0.2, num_ctx=min(NUM_CTX, 8192), seed=seed)
    return (out or "").strip()

def _extract_durable_entries(thread_id: str, user_text: str, assistant_text: str, px_raw: str) -> list[dict[str, Any]]:
    seed = int(_sha256_text(thread_id + user_text)[:8], 16)
    prompt = (
        "Extract durable memory entries worth persisting.\n"
        "Output ONLY JSON array.\n"
        "Each entry: {\"kind\":\"definition|hypothesis|decision|result|plan|constraint\","
        " \"text\":\"...\", \"tags\":[...]}.\n"
        f"Max entries: {MAX_DURABLE_ITEMS_PER_EXTRACT}.\n\n"
        f"[USER]\n{user_text[:2500]}\n\n[ASSISTANT]\n{assistant_text[:2500]}\n\n[PRAXIS_RAW]\n{(px_raw or '')[:2500]}\n"
    )

    msgs = [{"role": "system", "content": _load_system_prompt()},
            {"role": "user", "content": prompt}]
    out = _ollama_chat_sync(MODEL_FAST, msgs, temperature=0.1, num_ctx=min(NUM_CTX, 8192), seed=seed)
    out = (out or "").strip()
    try:
        data = json.loads(out)
        if isinstance(data, list):
            cleaned = []
            for e in data[:MAX_DURABLE_ITEMS_PER_EXTRACT]:
                if not isinstance(e, dict):
                    continue
                kind = str(e.get("kind", "note"))[:24]
                text = str(e.get("text", "")).strip()
                if not text:
                    continue
                tags = e.get("tags", [])
                if not isinstance(tags, list):
                    tags = []
                cleaned.append({"kind": kind, "text": text[:1200], "tags": tags[:12], "ts": _utc_now(), "thread_id": thread_id})
            return cleaned
    except Exception:
        pass
    return []

# ---------------------------------------------------------------------------
# Provenance formatting
# ---------------------------------------------------------------------------

def _format_provenance(run_id: str, wm_hash: str, px_hash: str, user_hash: str, px_method: str) -> str:
    if not PROVENANCE_ENABLED:
        return ""
    lines = [
        "",
        "Provenance:",
        f"- WM: {wm_hash[:12]}",
        f"- PX: {px_hash[:12]} ({px_method})" if px_hash else f"- PX: none ({px_method})",
        f"- U:  {user_hash[:12]}",
        f"- R:  {run_id}",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Pack builder: model produces a MANIFEST (small) + file contents (streamed to disk)
# ---------------------------------------------------------------------------

_MANIFEST_START = "<<<URI_FILE_MANIFEST>>>"
_MANIFEST_END = "<<<END_MANIFEST>>>"

def _pack_build_prompt(spec: str, thread_id: str, pack_name: str) -> str:
    """
    The model must output:
    1) A JSON manifest between markers, listing files and their descriptions.
    2) Then file blocks, one per file, with clear markers:
       <<<FILE:path/to/file.ext>>>
       ...content...
       <<<END_FILE>>>
    This lets us write to disk while streaming, and keep chat clean.
    """
    rules = (
        "You are generating a multi-file project on disk.\n"
        "Hard rules:\n"
        f"- Max files: {PACK_MAX_FILES}\n"
        f"- Max bytes per file (approx): {PACK_MAX_FILE_BYTES}\n"
        "- Text files only unless explicitly requested. If binary needed, base64 encode and label.\n"
        "- Use sane repo structure. Include README.md.\n"
        "- Do NOT dump huge explanations. Code + docs only.\n\n"
        "Output format:\n"
        f"{_MANIFEST_START}\n"
        "{\"name\":\"...\",\"files\":[{\"path\":\"...\",\"purpose\":\"...\"}, ...]}\n"
        f"{_MANIFEST_END}\n\n"
        "Then for each file:\n"
        "<<<FILE:relative/path.ext>>>\n"
        "<content>\n"
        "<<<END_FILE>>>\n"
    )
    return f"{rules}\n[THREAD_ID]\n{thread_id}\n\n[PACK]\n{pack_name}\n\n[SPEC]\n{spec}\n"

def _write_pack_files_from_stream(stream_chunks: List[str], pack_dir: Path) -> Tuple[dict[str, Any], List[str]]:
    """
    Parses streamed text for manifest + file blocks.
    Returns: (manifest_obj, written_files)
    """
    text = "".join(stream_chunks)

    # Extract manifest
    manifest_obj: dict[str, Any] = {}
    if _MANIFEST_START in text and _MANIFEST_END in text:
        start = text.index(_MANIFEST_START) + len(_MANIFEST_START)
        end = text.index(_MANIFEST_END, start)
        raw = text[start:end].strip()
        try:
            manifest_obj = json.loads(raw)
        except Exception:
            manifest_obj = {"error": "manifest_parse_failed", "raw": raw[:2000]}

    written: List[str] = []
    # Parse file blocks
    # Pattern: <<<FILE:path>>>(content)<<<END_FILE>>>
    file_pat = re.compile(r"<<<FILE:(?P<path>[^>]+)>>>\s*(?P<body>.*?)\s*<<<END_FILE>>>", re.DOTALL)
    for m in file_pat.finditer(text):
        rel = m.group("path").strip().replace("\\", "/")
        body = m.group("body")

        if not rel or rel.startswith("/") or ".." in rel:
            continue

        out_path = _safe_join(pack_dir, rel)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # If binary base64 encoding requested, you can extend later. Default: write text.
        b = body.encode("utf-8", errors="ignore")
        if len(b) > PACK_MAX_FILE_BYTES:
            # truncate rather than corrupting the pack; determinism > drama
            b = b[:PACK_MAX_FILE_BYTES]
            b += b"\n\n# [TRUNCATED: PACK_MAX_FILE_BYTES]\n"
        out_path.write_bytes(b)
        written.append(rel)

    return manifest_obj, written

def _make_zip_from_dir(src_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                p = Path(root) / fn
                rel = p.relative_to(src_dir)
                z.write(p, arcname=str(rel).replace("\\", "/"))

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _parse_task_add(rest: str) -> Tuple[str, str]:
    s = rest.strip()
    if s.lower().startswith("add"):
        s = s[3:].strip()
    s = s.lstrip()

    m = re.match(r"^(?P<type>research|code|sim|design|analysis)\s*:\s*(?P<spec>.+)$", s, flags=re.I)
    if m:
        return m.group("type").lower(), m.group("spec").strip()

    m2 = re.match(r"^:\s*(?P<spec>.+)$", s)
    if m2:
        return "research", m2.group("spec").strip()

    return "research", s.strip()

def _handle_command(message: str, conv: dict[str, Any]) -> Optional[str]:
    m = _CMD.match(message)
    if not m:
        return None
    cmd = (m.group("cmd") or "").lower()
    rest = (m.group("rest") or "").strip()

    active_tid = _ensure_active_thread()

    if cmd == "thread":
        if rest.lower().startswith("new"):
            title = rest[3:].strip()
            if title.startswith(":"):
                title = title[1:].strip()
            tid = _thread_new(title or "Untitled")
            conv["thread_id"] = tid
            _save_conversation(conv)
            return f"[THREAD] created {tid}\nTITLE: {title or 'Untitled'}\nACTIVE: {tid}"

        if rest.lower().startswith("set"):
            tid = rest[3:].strip()
            if tid.startswith(":"):
                tid = tid[1:].strip()
            ok = _thread_set(tid)
            if ok:
                conv["thread_id"] = tid
                _save_conversation(conv)
                return f"[THREAD] active set to {tid}"
            return f"[THREAD] not found: {tid}"

        if rest.lower().startswith("list"):
            ths = _thread_list()
            active = _load_threads().get("active_thread", "")
            lines = [f"[THREADS] active={active} count={len(ths)}"]
            for t in ths[:50]:
                lines.append(f"- {t.get('id')}  {t.get('title','')}")
            return "\n".join(lines)

        return "Usage:\n#thread new: <title>\n#thread set: <thread_id>\n#thread list"

    if cmd == "task":
        tid = conv.get("thread_id") or _load_threads().get("active_thread") or active_tid

        if rest.lower().startswith("add"):
            task_type, spec = _parse_task_add(rest)
            if not spec:
                return "[TASK] missing spec"
            item = _task_add(tid, task_type, spec)
            return f"[TASK] queued {item['id']} type={task_type}\nSPEC: {spec}"

        if rest.lower().startswith("list"):
            q = _task_list(tid)
            lines = [f"[TASKS] thread={tid} active={len(q['active'])} completed={len(q['completed'])}"]
            if q["active"]:
                lines.append("ACTIVE:")
                for t in q["active"][:80]:
                    lines.append(f"- {t.get('id')} [{t.get('status')}] {t.get('type')}: {t.get('spec')}")
            if q["completed"]:
                lines.append("COMPLETED:")
                for t in q["completed"][:40]:
                    lines.append(f"- {t.get('id')} [{t.get('status')}] {t.get('type')}: {t.get('spec')}")
            return "\n".join(lines)

        if rest.lower().startswith("done"):
            task_id = rest[4:].strip()
            if task_id.startswith(":"):
                task_id = task_id[1:].strip()
            ok = _task_done(task_id)
            return f"[TASK] marked done: {task_id}" if ok else f"[TASK] not found: {task_id}"

        if rest.lower().startswith("run"):
            return "__URI_TASK_RUN__"

        return "Usage:\n#task add: <spec>\n#task add code: <spec>\n#task list\n#task run\n#task done: <task_id>"

    if cmd == "pin":
        text = rest
        if text.startswith(":"):
            text = text[1:].strip()
        if not text:
            return "[PIN] missing text"
        _pin(text)
        return "[PIN] stored"

    if cmd == "pack":
        tid = conv.get("thread_id") or _ensure_active_thread()

        if rest.lower().startswith("new"):
            name = rest[3:].strip()
            if name.startswith(":"):
                name = name[1:].strip()
            p = _pack_new(tid, name or "Pack")
            return f"[PACK] created {p['id']}\nNAME: {p['name']}\nDIR: {p['dir']}"

        if rest.lower().startswith("status"):
            return _pack_status(tid)

        if rest.lower().startswith("build"):
            return "__URI_PACK_BUILD__"

        return "Usage:\n#pack new: <name>\n#pack status\n#pack build: <spec>"

    return None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/models", methods=["GET"])
def list_models():
    try:
        resp = _req.get(f"{OLLAMA_BASE}/api/tags", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return jsonify({"models": models})
    except Exception as exc:
        _log(f"Cannot fetch model list: {exc}")
    return jsonify({"models": [MODEL_PRIMARY, MODEL_VISION, MODEL_CODE, MODEL_FAST]})

@app.route("/history", methods=["GET"])
def history():
    return jsonify({"conversations": _list_conversations()})

@app.route("/history/<conv_id>", methods=["GET"])
def load_conversation(conv_id: str):
    return jsonify(_load_conversation(conv_id))

@app.route("/new", methods=["POST"])
def new_conversation():
    conv_id = str(uuid.uuid4())
    conv = _load_conversation(conv_id)
    conv["thread_id"] = _ensure_active_thread()
    _save_conversation(conv)
    return jsonify({"id": conv_id})

@app.route("/clear", methods=["POST"])
def clear_conversation():
    data = request.get_json(silent=True) or {}
    conv_id = str(data.get("conv_id", "")).strip()
    if conv_id:
        conv = _load_conversation(conv_id)
        conv["messages"] = []
        conv["title"] = "New conversation"
        _save_conversation(conv)
    return jsonify({"ok": True})

@app.route("/state", methods=["GET"])
def state():
    active_tid = _ensure_active_thread()
    return jsonify({
        "active_thread": _load_threads().get("active_thread", active_tid),
        "threads": _thread_list()[:50],
        "ctx": NUM_CTX,
        "models": {"primary": MODEL_PRIMARY, "code": MODEL_CODE, "vision": MODEL_VISION, "fast": MODEL_FAST},
        "packs": _load_packs(),
    })

@app.route("/api/health", methods=["GET"])
def api_health():
    last_run = _latest_run_info()
    return jsonify({
        "ollama": _health_ollama(),
        "uri": {
            "status": "online",
            "uptime_sec": int(time.time() - APP_START_TS),
        },
        "praxis": _health_praxis(),
        "broker": _health_broker(),
        "corpus": _health_corpus(),
        "last_run": last_run,
        "last_run_result": last_run.get("status"),
        "last_session_id": last_run.get("session_id"),
        "last_synthesis": _latest_synthesis_info(),
    })


@app.route("/api/praxis/query", methods=["POST"])
def api_praxis_query():
    body = request.get_json(force=True, silent=True) or {}
    query = str(body.get("query", "")).strip()
    try:
        top_n = int(body.get("top_n", PRAXIS_TOP_N))
    except (TypeError, ValueError):
        top_n = PRAXIS_TOP_N

    if not query:
        return jsonify({"ok": False, "error": "missing query"}), 400

    return jsonify(_praxis_query_best_effort(query, top_n))


@app.route("/api/sovereign/recent-runs", methods=["GET"])
def api_recent_runs():
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    return jsonify(_read_recent_runs(max(1, limit)))


@app.route("/api/sovereign/run", methods=["POST"])
def api_sovereign_run():
    body = request.get_json(force=True, silent=True) or {}
    topic = str(body.get("topic", "")).strip()
    if not topic:
        return jsonify({"ok": False, "error": "missing topic"}), 400
    return jsonify(_start_sovereign_run(topic))


@app.route("/api/sovereign/inject-topic", methods=["POST"])
def api_inject_topic():
    body = request.get_json(force=True, silent=True) or {}
    topic = str(body.get("topic", "")).strip()
    priority = str(body.get("priority", "normal")).strip() or "normal"

    if not topic:
        return jsonify({"ok": False, "error": "missing topic"}), 400

    return jsonify(_inject_topic(topic, priority))


@app.route("/api/sovereign/synthesis", methods=["GET"])
def api_synthesis():
    return jsonify(_latest_synthesis_text())


@app.route("/api/sovereign/session-graph", methods=["GET"])
def api_session_graph():
    return jsonify(_load_session_graph())


@app.route("/api/sovereign/pause", methods=["POST"])
def api_pause_supervisor():
    return jsonify(_pause_supervisor())


@app.route("/api/sovereign/resume", methods=["POST"])
def api_resume_supervisor():
    return jsonify(_resume_supervisor())


@app.route("/api/sovereign/publication-queue", methods=["GET"])
def api_publication_queue():
    return jsonify(_read_publication_queue())


# -------------------------
# Upload endpoint (multipart)
# -------------------------

@app.route("/upload", methods=["POST"])
def upload():
    # expects: conv_id (optional), thread_id (optional), files[]
    conv_id = (request.form.get("conv_id") or "").strip()
    thread_id = (request.form.get("thread_id") or "").strip()

    if conv_id:
        conv = _load_conversation(conv_id)
        if not conv.get("thread_id"):
            conv["thread_id"] = _ensure_active_thread()
            _save_conversation(conv)
        if not thread_id:
            thread_id = conv["thread_id"]

    if not thread_id:
        thread_id = _ensure_active_thread()

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files"}), 400

    out_dir = _thread_upload_dir(thread_id)
    results = []

    for f in files:
        raw = f.read()
        if len(raw) > UPLOAD_MAX_BYTES:
            return jsonify({"error": f"file too large (>{UPLOAD_MAX_BYTES} bytes)"}), 413

        file_id = "u-" + uuid.uuid4().hex
        name = (f.filename or "upload.bin").strip()
        safe_name = re.sub(r"[^a-zA-Z0-9._\-()\[\] ]+", "_", name)[:180] or "upload.bin"
        save_path = out_dir / f"{file_id}_{safe_name}"
        save_path.write_bytes(raw)

        sha = _sha256_bytes(raw)
        preview = _detect_text_preview(save_path)
        inv = _zip_inventory(save_path) if safe_name.lower().endswith(".zip") else []

        rec = {
            "kind": "upload",
            "ts": _utc_now(),
            "file_id": file_id,
            "thread_id": thread_id,
            "conv_id": conv_id,
            "name": safe_name,
            "path": str(save_path),
            "sha256": sha,
            "size": len(raw),
            "text_preview": preview,
            "zip_inventory": inv,
        }
        _uploads_log(rec)
        results.append({
            "file_id": file_id,
            "name": safe_name,
            "sha256": sha,
            "size": len(raw),
            "has_text_preview": bool(preview),
            "zip_entries": len(inv) if inv else 0,
        })

    return jsonify({"thread_id": thread_id, "files": results})

# -------------------------
# Download uploaded file
# -------------------------

@app.route("/file/<file_id>", methods=["GET"])
def download_file(file_id: str):
    rec = _get_uploaded_file_record(file_id)
    if not rec:
        abort(404)
    p = Path(rec.get("path", ""))
    if not p.exists():
        abort(404)
    # hard fence: must live under uploads
    try:
        _safe_join(UPLOADS_DIR, *p.relative_to(UPLOADS_DIR).parts)
    except Exception:
        abort(403)
    return send_file(str(p), as_attachment=True, download_name=rec.get("name", p.name))

# -------------------------
# Download artifact zip
# -------------------------

@app.route("/artifact/<artifact_id>", methods=["GET"])
def download_artifact(artifact_id: str):
    # artifact_id expected like "p-xxxx_r-xxxx.zip" or similar
    p = _safe_join(ARTIFACT_ZIPS_DIR, artifact_id)
    if not p.exists():
        abort(404)
    return send_file(str(p), as_attachment=True, download_name=p.name)

# ---------------------------------------------------------------------------
# Main chat route (adds: file_ids injection + pack build mode)
# ---------------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message        = str(data.get("message", "")).strip()
    conv_id        = str(data.get("conv_id", "")).strip() or str(uuid.uuid4())
    model_override = data.get("model", None)
    temperature    = float(data.get("temperature", TEMPERATURE))
    images         = data.get("images", [])
    stream_mode    = bool(data.get("stream", True))

    # NEW: file_ids from UI uploads
    file_ids       = data.get("file_ids", []) or []
    if not isinstance(file_ids, list):
        file_ids = []

    if not message:
        return jsonify({"error": "Empty message"}), 400

    conv = _load_conversation(conv_id)
    if not conv.get("thread_id"):
        conv["thread_id"] = _load_threads().get("active_thread") or _ensure_active_thread()

    # Command handling
    cmd_result = _handle_command(message, conv)
    task_id_for_run = None
    is_task_run = False
    is_pack_build = False
    pack_spec = ""

    if cmd_result is not None:
        if cmd_result == "__URI_TASK_RUN__":
            is_task_run = True
            thread_id = conv["thread_id"]
            task = _task_pop_next(thread_id)
            if not task:
                resp_text = "[TASK] no queued tasks for active thread"
                conv["messages"].append({"role": "assistant", "content": resp_text, "model": "local"})
                _save_conversation(conv)
                return jsonify({"response": resp_text, "conv_id": conv_id, "model": "local"})

            task_spec = task.get("spec", "")
            task_type = task.get("type", "research")
            task_id_for_run = task["id"]

            message = (
                "Execute this task. Produce a usable result.\n"
                "If code is required, provide complete code.\n\n"
                f"[TASK RUN] {task_id_for_run} ({task_type})\n{task_spec}"
            )

        elif cmd_result == "__URI_PACK_BUILD__":
            is_pack_build = True
            # spec is after "#pack build:" in original message; re-parse it
            # message was "#pack build: <spec>"
            m = re.match(r"^\s*#pack\s+build\s*:\s*(.+)$", data.get("message",""), flags=re.I)
            pack_spec = (m.group(1).strip() if m else "")
            if not pack_spec:
                resp_text = "[PACK] missing spec. Usage: #pack build: <what to generate>"
                conv["messages"].append({"role": "assistant", "content": resp_text, "model": "local"})
                _save_conversation(conv)
                return jsonify({"response": resp_text, "conv_id": conv_id, "model": "local"})

            # We'll set message to a strict pack-build prompt later.
            message = f"[PACK BUILD]\n{pack_spec}"

        else:
            resp_text = cmd_result
            conv["messages"].append({"role": "assistant", "content": resp_text, "model": "local"})
            _save_conversation(conv)
            return jsonify({"response": resp_text, "conv_id": conv_id, "model": "local"})

    thread_id = conv["thread_id"]
    has_image = bool(images)

    # PRAXIS retrieval (always attempted)
    px = _praxis_query_best_effort(message, PRAXIS_TOP_N)
    px_items = px.get("items", [])
    px_raw = px.get("raw", "")
    px_hash = _sha256_text(px_raw) if px_raw else ""
    px_method = px.get("method", "none")

    if is_task_run and PRAXIS_FAIL_CLOSED_TASKS and (not px_items):
        resp_text = f"[TASK] blocked: PRAXIS returned zero hits (fail-closed enabled)\nMETHOD: {px_method}"
        tasks = _load_tasks()
        for t in tasks.get("active", []):
            if t.get("id") == task_id_for_run:
                t["status"] = "blocked"
                t["blocked_reason"] = "praxis_zero_hits"
                t["updated"] = _utc_now()
                break
        _save_tasks(tasks)
        conv["messages"].append({"role": "assistant", "content": resp_text, "model": "local"})
        _save_conversation(conv)
        return jsonify({"response": resp_text, "conv_id": conv_id, "model": "local"})

    # Route model
    model = _select_model(message, has_image, model_override)
    system_prompt = _load_system_prompt()

    # Append user message (keep original command text for history)
    user_msg: dict[str, Any] = {"role": "user", "content": str(data.get("message", message))}
    if has_image:
        user_msg["images"] = images
    if file_ids:
        user_msg["file_ids"] = file_ids
    conv["messages"].append(user_msg)

    if len(conv["messages"]) == 1:
        conv["title"] = _derive_title(conv["messages"])

    # Update turn counters
    st = _load_rrr_state()
    st["turns"] = int(st.get("turns", 0)) + 1
    _save_rrr_state(st)

    working_text = _load_working_memory().get("text", "")
    wm_hash = _sha256_text(working_text or "")
    user_hash = _sha256_text(message)

    # Build file injection (small previews only)
    file_inject_blocks = []
    file_meta_for_run = []
    for fid in file_ids[:20]:
        rec = _get_uploaded_file_record(str(fid))
        if not rec:
            continue
        name = rec.get("name","")
        sha = rec.get("sha256","")
        size = rec.get("size",0)
        preview = rec.get("text_preview","") or ""
        inv = rec.get("zip_inventory", []) or []
        file_meta_for_run.append({"file_id": fid, "name": name, "sha256": sha, "size": size})

        blk = f"[FILE] {name}\nSHA256: {sha}\nSIZE: {size}\n"
        if inv:
            blk += "ZIP INVENTORY (partial):\n" + "\n".join(inv[:80]) + "\n"
        if preview:
            blk += "TEXT PREVIEW:\n" + preview[:UPLOAD_TEXT_PREVIEW_MAX_CHARS] + "\n"
        file_inject_blocks.append(blk)

    files_inject = ""
    if file_inject_blocks:
        files_inject = "[UPLOADS]\n" + "\n---\n".join(file_inject_blocks) + "\n"

    _ledger_append("user_turn", {
        "conv_id": conv_id,
        "thread_id": thread_id,
        "message": message[:4000],
        "has_image": has_image,
        "model_routed": model,
        "praxis_method": px_method,
        "praxis_ok": bool(px.get("ok")),
        "praxis_hits": len(px_items) if isinstance(px_items, list) else 0,
        "task_id": task_id_for_run,
        "file_ids": file_ids,
    })

    # Operator context injection: WM + PRAXIS + FILE PREVIEWS
    px_inject = ""
    if px_items:
        lines = []
        for it in px_items[:PRAXIS_TOP_N]:
            if isinstance(it, dict):
                t = it.get("text") or it.get("content") or it.get("document") or ""
                meta = it.get("metadata") or it.get("meta") or {}
            else:
                t = str(it)
                meta = {}
            t = str(t).strip()
            if not t:
                continue
            t = t[:1200]
            if meta:
                lines.append(f"- {t}\n  META: {json.dumps(meta, ensure_ascii=False)[:400]}")
            else:
                lines.append(f"- {t}")
        px_inject = "\n".join(lines)

    wm_inject = (working_text or "").strip()[:RRR_MAX_WORKING_CHARS]

    operator_context = ""
    if wm_inject or px_inject or files_inject:
        operator_context = (
            "[OPERATOR CONTEXT]\n"
            "Working memory is rolling summary. PRAXIS is retrieved evidence. Uploads are references.\n"
            "Use them as context.\n\n"
        )
        if wm_inject:
            operator_context += f"[WORKING_MEMORY]\n{wm_inject}\n\n"
        if px_inject:
            operator_context += f"[PRAXIS_RETRIEVAL]\n{px_inject}\n\n"
        if files_inject:
            operator_context += files_inject + "\n"

    # Pack build overrides message into strict format
    pack = _pack_active(thread_id)
    if is_pack_build:
        if not pack:
            pack = _pack_new(thread_id, "Pack")
        pack_name = pack.get("name","Pack")
        message = _pack_build_prompt(pack_spec, thread_id, pack_name)
        # pack builds should route to coder if it's code-ish
        if _CODE_TRIGGERS.search(pack_spec):
            model = MODEL_CODE
        else:
            model = MODEL_PRIMARY

    # Build model messages
    ollama_msgs = _build_ollama_messages(conv["messages"], system_prompt)
    # Replace last user content with our actual runtime "message"
    # (History keeps raw command text, runtime uses strict prompt)
    ollama_msgs.append({"role": "user", "content": message})
    if operator_context:
        ollama_msgs.append({"role": "user", "content": operator_context})

    run_id = "r-" + uuid.uuid4().hex
    prompt_hash = _sha256_text(json.dumps(ollama_msgs, ensure_ascii=False)[:200000])

    _cite({"ts": _utc_now(), "run_id": run_id, "kind": "U", "hash": user_hash, "conv_id": conv_id, "thread_id": thread_id})
    _cite({"ts": _utc_now(), "run_id": run_id, "kind": "WM", "hash": wm_hash, "conv_id": conv_id, "thread_id": thread_id})
    if px_hash:
        _cite({"ts": _utc_now(), "run_id": run_id, "kind": "PX", "hash": px_hash, "method": px_method, "conv_id": conv_id, "thread_id": thread_id})
    if file_meta_for_run:
        _cite({"ts": _utc_now(), "run_id": run_id, "kind": "UP", "files": file_meta_for_run, "conv_id": conv_id, "thread_id": thread_id})

    _log(f"Chat | conv={conv_id} thread={thread_id} run={run_id[:14]} model={model} ctx={NUM_CTX} images={len(images)} files={len(file_ids)} px={px_method} pack={bool(is_pack_build)}")

    do_rrr = (st["turns"] - int(st.get("last_summary_at", 0))) >= RRR_UPDATE_EVERY_TURNS
    do_extract = (st["turns"] - int(st.get("last_extract_at", 0))) >= MEMORY_EXTRACT_EVERY_TURNS
    do_commit = (st["turns"] - int(st.get("last_commit_at", 0))) >= PRAXIS_COMMIT_EVERY_TURNS

    def _postprocess_and_persist(response_text: str) -> Tuple[str, str, Optional[str]]:
        nonlocal st, do_rrr, do_extract, do_commit, pack

        response_text = response_text or ""
        response_hash = _sha256_text(response_text)

        # If pack build: write files + zip them, and return a SHORT chat response with a link
        artifact_zip_name = None
        manifest_obj = {}
        written_files: List[str] = []

        if is_pack_build and pack:
            pack_dir = Path(pack["dir"])
            # parse & write files
            manifest_obj, written_files = _write_pack_files_from_stream([response_text], pack_dir)

            # zip it
            ARTIFACT_ZIPS_DIR.mkdir(parents=True, exist_ok=True)
            artifact_zip_name = f"{pack['id']}_{run_id}.zip"
            zip_path = ARTIFACT_ZIPS_DIR / artifact_zip_name
            _make_zip_from_dir(pack_dir, zip_path)

            # update pack registry
            packs = _load_packs()
            for p in packs.get("packs", []):
                if p.get("id") == pack["id"]:
                    p["last_zip"] = str(zip_path)
                    p["last_manifest"] = json.dumps(manifest_obj, ensure_ascii=False)[:4000]
                    p["updated"] = _utc_now()
                    break
            _save_packs(packs)

        # Durable extraction -> buffer
        extracted: list[dict[str, Any]] = []
        if do_extract:
            try:
                extracted = _extract_durable_entries(thread_id, str(data.get("message",""))[:2500], response_text, px_raw)
                if extracted:
                    st["durable_buffer"] = (st.get("durable_buffer", []) + extracted)[-200:]
                    st["last_extract_at"] = st["turns"]
                    _save_rrr_state(st)
                    _ledger_append("durable_extract", {"thread_id": thread_id, "count": len(extracted), "hash": _sha256_text(json.dumps(extracted, ensure_ascii=False))})
            except Exception as exc:
                _log(f"Durable extract failed: {exc}")

        # RRR summary update
        if do_rrr:
            try:
                new_summary = _summarize_working_memory(thread_id, conv["messages"], px_items)
                if new_summary:
                    _save_working_memory(new_summary)
                    st["last_summary_at"] = st["turns"]
                    _save_rrr_state(st)

                    th = _load_threads()
                    for t in th.get("threads", []):
                        if t.get("id") == thread_id:
                            t["working_summary"] = new_summary[:RRR_MAX_WORKING_CHARS]
                            t["updated"] = _utc_now()
                            break
                    _save_threads(th)

                    _ledger_append("rrr_summary", {"thread_id": thread_id, "hash": _sha256_text(new_summary)})
            except Exception as exc:
                _log(f"RRR summarize failed: {exc}")

        # Periodic PRAXIS commit attempt
        commit_result = {"ok": False, "method": "skipped"}
        if do_commit:
            try:
                buf = st.get("durable_buffer", [])
                if buf:
                    commit_result = _praxis_commit_best_effort(buf[-40:])
                    if commit_result.get("ok"):
                        st["durable_buffer"] = []
                        st["last_commit_at"] = st["turns"]
                        _save_rrr_state(st)
                        _ledger_append("praxis_commit", {"thread_id": thread_id, "ok": True, "method": commit_result.get("method")})
                    else:
                        _ledger_append("praxis_commit", {"thread_id": thread_id, "ok": False, "method": commit_result.get("method"), "error": commit_result.get("error", "")})
            except Exception as exc:
                _log(f"PRAXIS commit failed: {exc}")

        # Provenance
        prov = _format_provenance(run_id, wm_hash, px_hash, user_hash, px_method)

        if artifact_zip_name:
            # keep chat short; link points to /artifact/<zipname>
            summary = "Pack built on disk.\n"
            if manifest_obj.get("name"):
                summary += f"NAME: {manifest_obj.get('name')}\n"
            if written_files:
                summary += f"FILES: {len(written_files)} written\n"
            summary += f"DOWNLOAD: /artifact/{artifact_zip_name}\n"
            final_text = summary + (prov if prov else "")
        else:
            final_text = response_text + (prov if prov else "")

        # Persist conversation
        conv["messages"].append({"role": "assistant", "content": final_text, "model": model})
        _save_conversation(conv)

        # Run artifact
        run_art = {
            "run_id": run_id,
            "ts": _utc_now(),
            "conv_id": conv_id,
            "thread_id": thread_id,
            "task_id": task_id_for_run,
            "model": model,
            "num_ctx": NUM_CTX,
            "temperature": temperature,
            "prompt_hash": prompt_hash,
            "wm_hash": wm_hash,
            "px_hash": px_hash,
            "px_method": px_method,
            "user_hash": user_hash,
            "response_hash": response_hash,
            "uploads": file_meta_for_run,
            "pack_build": bool(is_pack_build),
            "pack_id": (pack.get("id") if pack else ""),
            "artifact_zip": artifact_zip_name or "",
            "durable_extracted": extracted,
            "praxis_commit": commit_result,
        }
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        run_path = RUNS_DIR / f"{run_id}.json"
        _write_json_atomic(run_path, run_art)
        _ledger_append("run", {"run_id": run_id, "path": str(run_path), "thread_id": thread_id})

        if task_id_for_run:
            _task_finish(task_id_for_run, str(run_path))

        return final_text, prov, artifact_zip_name

    if stream_mode:
        def generate():
            full_chunks: list[str] = []
            yield f"data: {json.dumps({'conv_id': conv_id, 'model': model, 'run_id': run_id})}\n\n"
            seed = SEED_DEFAULT

            for chunk in _ollama_chat_stream(model, ollama_msgs, temperature, NUM_CTX, seed):
                full_chunks.append(chunk)
                # For pack builds, we DO NOT stream raw content to UI (keeps chat clean).
                if not is_pack_build:
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            response_text = "".join(full_chunks)
            final_text, prov, artifact_zip = _postprocess_and_persist(response_text)

            # For pack builds, we stream only the short final response.
            if is_pack_build:
                yield f"data: {json.dumps({'chunk': final_text})}\n\n"
            else:
                if prov:
                    yield f"data: {json.dumps({'chunk': prov})}\n\n"

            # Include artifact link as a structured event for UI (optional)
            if artifact_zip:
                yield f"data: {json.dumps({'artifact': '/artifact/' + artifact_zip})}\n\n"

            yield f"data: {json.dumps({'done': True, 'conv_id': conv_id})}\n\n"

        return Response(stream_with_context(generate()),
                        mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    else:
        seed = SEED_DEFAULT
        response_text = _ollama_chat_sync(model, ollama_msgs, temperature, NUM_CTX, seed)
        final_text, _, _ = _postprocess_and_persist(response_text)
        return jsonify({"response": final_text, "conv_id": conv_id, "model": model})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    URI_ROOT.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ZIPS_DIR.mkdir(parents=True, exist_ok=True)

    if not THREADS_PATH.exists():
        _write_json_atomic(THREADS_PATH, {"active_thread": "", "threads": []})
    if not TASKS_PATH.exists():
        _write_json_atomic(TASKS_PATH, {"active": [], "completed": []})
    if not WORKING_MEMORY_PATH.exists():
        _write_json_atomic(WORKING_MEMORY_PATH, {"text": "", "updated": ""})
    if not RRR_STATE_PATH.exists():
        _write_json_atomic(RRR_STATE_PATH, {"turns": 0, "last_summary_at": 0, "last_extract_at": 0, "last_commit_at": 0, "pins": [], "durable_buffer": []})
    if not RESEARCH_LEDGER_PATH.exists():
        RESEARCH_LEDGER_PATH.write_text("", encoding="utf-8")
    if not CITATIONS_PATH.exists():
        CITATIONS_PATH.write_text("", encoding="utf-8")
    if not UPLOAD_INDEX_JSONL.exists():
        UPLOAD_INDEX_JSONL.write_text("", encoding="utf-8")
    if not PACKS_PATH.exists():
        _write_json_atomic(PACKS_PATH, {"active_pack": "", "packs": []})

if __name__ == "__main__":
    URI_ROOT.mkdir(parents=True, exist_ok=True)
    _bootstrap_canonical_root_from_legacy()
    _ensure_dirs()
    if not SYSTEM_PROMPT_PATH.exists():
        _log("WARNING: system_prompt.txt not found - using fallback prompt")
    _ensure_active_thread()
    _print_startup_identity_banner()
    _log(
        f"{APP_IDENTITY} starting http://127.0.0.1:5000 | build={APP_BUILD} | root={URI_ROOT} "
        f"| ctx={NUM_CTX} | models={MODEL_PRIMARY},{MODEL_CODE},{MODEL_VISION},{MODEL_FAST}"
    )
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
