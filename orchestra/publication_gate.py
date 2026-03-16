from __future__ import annotations

r"""
publication_gate.py - SOVEREIGN Phase 9

Reads orchestra\session_graph.json (read-only), evaluates completed sessions
against publication thresholds, writes qualified manifests to:
  publication_queue\{session_id}_manifest.json
and appends all outcomes to:
  published\index\gate_log.jsonl

CLI: python publication_gate.py --root E:\SOVEREIGN [--dry-run] [--skip-novelty]
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import requests as _requests
except ImportError:
    _requests = None

MODULE_NAME = "PUBLICATION_GATE"
SOVEREIGN_VERSION = "2.9.0"
SCHEMA_VERSION = "1.0"
MAX_LOG_BYTES = 10 * 1024 * 1024

OLLAMA_BASE_DEFAULT = "http://127.0.0.1:11434"
OLLAMA_EMBED_MODEL = "nomic-embed-text"

_COMPLETED_STATUSES = {"complete", "completed", "done"}

THRESH_CONVERGENCE = 0.90
THRESH_CONFIDENCE = 0.80
THRESH_DOMAIN = 0.70
THRESH_MAX_SIM_PUBLISHED = 0.80
THRESH_NOVELTY = 0.20

# New format: [SYNTH session_id=<ID>]
# Legacy format: [SYNTH S<SESSION_ID> <timestamp>]
_SYNTH_OPEN_RE = re.compile(
    r"^\[(SYNTH|RRR-SYNTH)(?:\s+session_id=([^\]\s]+)|\s+S([^\s\]]+)(?:\s+[^\]]+)?)\]\s*$",
    re.IGNORECASE,
)
_SYNTH_CLOSE_RE = re.compile(r"^\[/(SYNTH|RRR-SYNTH)\]\s*$", re.IGNORECASE)

_CONFLICTS_HDR_RE = re.compile(r"^\s*(#+\s*)?CONFLICTS\s*:?\s*$", re.IGNORECASE)
_CONFLICTS_NONE_VALS = {
    "none",
    "no conflicts",
    "no conflict",
    "n/a",
    "na",
    "none.",
    "no conflicts.",
    "no conflict.",
    "not provided",
    "not provided.",
    "- none",
    "* none",
    "- no conflicts",
    "* no conflicts",
}
_SECTION_HEADER_PREFIXES = (
    "claims",
    "evidence",
    "resolution",
    "gaps",
    "summary",
    "abstract",
    "introduction",
    "method",
    "result",
    "discussion",
    "conclusion",
    "references",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rotate_log_if_needed(log_file: Path) -> None:
    try:
        if not log_file.exists() or log_file.stat().st_size <= MAX_LOG_BYTES:
            return
        backup = Path(str(log_file) + ".1")
        if backup.exists():
            backup.unlink()
        os.replace(str(log_file), str(backup))
    except OSError:
        pass


def _log(level: str, msg: str, lp: Optional[Path]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] [{MODULE_NAME}] [{level}] {msg}"
    print(line, flush=True)
    if lp is not None:
        try:
            lp.parent.mkdir(parents=True, exist_ok=True)
            _rotate_log_if_needed(lp)
            with open(lp, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _info(msg: str, lp: Optional[Path]) -> None:
    _log("INFO", msg, lp)


def _warn(msg: str, lp: Optional[Path]) -> None:
    _log("WARN", msg, lp)


def _error(msg: str, lp: Optional[Path]) -> None:
    _log("ERROR", msg, lp)


def _paths(root: Path) -> dict[str, Path]:
    return {
        "session_graph": root / "orchestra" / "session_graph.json",
        "synthesis_log": root / "praxis" / "logs" / "synthesis.txt",
        "domain_file": root / "corpus" / "domain.txt",
        "pub_queue": root / "publication_queue",
        "pub_index_dir": root / "published" / "index",
        "gate_log": root / "published" / "index" / "gate_log.jsonl",
        "pub_index": root / "published" / "index" / "index.jsonl",
        "log_file": root / "logs" / "publication_gate_log.txt",
    }


def _read_text(path: Path, lp: Optional[Path]) -> Optional[str]:
    if not path.exists():
        _warn(f"File not found: {path}", lp)
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _error(f"Cannot read {path}: {exc}", lp)
        return None


def _read_session_graph(path: Path, lp: Optional[Path]) -> Optional[list[dict[str, Any]]]:
    raw = _read_text(path, lp)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:200].replace("\n", "\\n")
        _error(f"Malformed JSON in {path}: {exc} | raw[:200]={snippet}", lp)
        return None

    if not isinstance(data, dict):
        _error(f"{path} must be a JSON object", lp)
        return None

    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        _error(
            f"session_graph.schema_version must be '{SCHEMA_VERSION}', got {schema_version!r}",
            lp,
        )
        return None

    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        _error(f"{path} missing/invalid sessions list", lp)
        return None

    out: list[dict[str, Any]] = []
    for i, item in enumerate(sessions):
        if isinstance(item, dict):
            out.append(item)
        else:
            _warn(f"Non-object session at index {i} - skipping", lp)
    return out


def _read_jsonl(path: Path, lp: Optional[Path]) -> list[dict[str, Any]]:
    raw = _read_text(path, lp)
    if raw is None:
        return []

    out: list[dict[str, Any]] = []
    for ln, line in enumerate(raw.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                out.append(obj)
            else:
                _warn(f"Non-object JSONL line {ln} in {path} - skipping", lp)
        except json.JSONDecodeError as exc:
            _warn(f"Malformed JSON line {ln} in {path}: {exc}", lp)
    return out


def _append_jsonl(path: Path, obj: dict[str, Any], dry_run: bool, lp: Optional[Path]) -> None:
    if dry_run:
        _info(f"[DRY-RUN] Would append to {path}: {json.dumps(obj, ensure_ascii=False)}", lp)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError as exc:
        _error(f"Failed to append to {path}: {exc}", lp)


def _atomic_write_json(path: Path, obj: dict[str, Any], dry_run: bool, lp: Optional[Path]) -> bool:
    if dry_run:
        _info(f"[DRY-RUN] Would write {path}", lp)
        return True

    tmp_path: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        tmp_path = Path(tmp)
        data = json.dumps(obj, ensure_ascii=False, indent=2)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            if not data.endswith("\n"):
                fh.write("\n")
        os.replace(str(tmp_path), str(path))
        return True
    except OSError as exc:
        _error(f"Failed to write {path}: {exc}", lp)
        try:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False


def _session_is_completed(node: dict[str, Any]) -> bool:
    return str(node.get("status", "")).strip().lower() in _COMPLETED_STATUSES


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:
            return None
        return v
    except (TypeError, ValueError):
        return None


def _parse_synth_open_tag(line: str) -> Optional[tuple[str, str]]:
    m = _SYNTH_OPEN_RE.match(line.strip())
    if not m:
        return None
    tag_name = m.group(1).upper()
    sid = (m.group(2) or m.group(3) or "").strip()
    return tag_name, sid


def _extract_synthesis_block(synthesis_text: str, session_id: str) -> tuple[str, str]:
    lines = synthesis_text.splitlines()
    sid = session_id.strip()
    hits: list[tuple[int, str, str]] = []

    for i, line in enumerate(lines):
        parsed = _parse_synth_open_tag(line)
        if not parsed:
            continue
        tag_name, tag_sid = parsed
        if tag_sid == sid:
            hits.append((i, line.rstrip(), tag_name))

    if not hits:
        return "", ""

    start_idx, tag_line, tag_name = hits[-1]
    end_idx = len(lines)

    for j in range(start_idx + 1, len(lines)):
        s = lines[j].strip()
        close_match = _SYNTH_CLOSE_RE.match(s)
        if close_match and close_match.group(1).upper() == tag_name:
            end_idx = j
            break
        if _parse_synth_open_tag(s):
            end_idx = j
            break

    block = "\n".join(lines[start_idx + 1 : end_idx]).strip()
    return block, tag_line


def _conflicts_resolved(synthesis_block: str) -> bool:
    if not synthesis_block:
        return False

    lines = synthesis_block.splitlines()
    conflicts_idx = None
    for i, line in enumerate(lines):
        if _CONFLICTS_HDR_RE.match(line):
            conflicts_idx = i
            break

    if conflicts_idx is None:
        return False

    body_lines: list[str] = []
    for j in range(conflicts_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if _CONFLICTS_HDR_RE.match(line):
            break
        upper = stripped.upper().lstrip("#").strip().rstrip(":").strip()
        if stripped.startswith("#") and any(upper.startswith(p.upper()) for p in _SECTION_HEADER_PREFIXES):
            break
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not body:
        return True

    norm = re.sub(r"\s+", " ", body).strip().lower()
    return norm in _CONFLICTS_NONE_VALS


class _EmbeddingCache:
    def __init__(self, ollama_base: str, lp: Optional[Path]) -> None:
        self._base = ollama_base.rstrip("/")
        self._lp = lp
        self._cache: dict[str, list[float]] = {}

    def embed(self, text: str) -> Optional[list[float]]:
        text = (text or "").strip()
        if not text:
            return None

        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key in self._cache:
            return self._cache[key]

        if _requests is None:
            _error("requests not installed - cannot call Ollama embeddings", self._lp)
            return None

        try:
            resp = _requests.post(
                f"{self._base}/api/embeddings",
                json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _warn(f"Ollama embeddings request failed: {exc}", self._lp)
            return None

        emb = data.get("embedding")
        if not isinstance(emb, list) or not emb:
            _warn("Ollama embeddings response missing 'embedding'", self._lp)
            return None

        try:
            vec = [float(x) for x in emb]
        except (TypeError, ValueError):
            _warn("Ollama embeddings returned non-numeric values", self._lp)
            return None

        self._cache[key] = vec
        return vec


def _cosine(a: list[float], b: list[float]) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None

    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y

    if na <= 0.0 or nb <= 0.0:
        return None

    return dot / ((na ** 0.5) * (nb ** 0.5))


def _domain_score(topic: str, domain_text: str, emb: _EmbeddingCache, lp: Optional[Path]) -> Optional[float]:
    if not domain_text:
        _error("domain.txt is empty - domain_score unavailable", lp)
        return None

    dom_vec = emb.embed(domain_text)
    top_vec = emb.embed(topic)
    if dom_vec is None or top_vec is None:
        return None

    return _cosine(dom_vec, top_vec)


def _novelty_score(
    topic: str,
    synthesis_summary: str,
    published_index: list[dict[str, Any]],
    emb: _EmbeddingCache,
    lp: Optional[Path],
) -> tuple[float, float, bool]:
    if not published_index:
        return 1.0, 0.0, False

    cur_text = (topic or "").strip() + "\n" + (synthesis_summary or "").strip()
    cur_vec = emb.embed(cur_text)
    if cur_vec is None:
        _warn("Novelty embedding unavailable - forcing novelty_score=0.0 (conservative fail)", lp)
        return 0.0, 1.0, True

    max_cos = 0.0
    any_compared = False

    for entry in published_index:
        etopic = str(entry.get("topic", "")).strip()
        etitle = str(entry.get("title", "")).strip()
        if not etopic and not etitle:
            continue

        e_text = etopic + ("\n" + etitle if etitle else "")
        e_vec = emb.embed(e_text)
        if e_vec is None:
            _warn(f"Embedding failed for published topic '{etopic[:60]}' - skipping", lp)
            continue

        sim = _cosine(cur_vec, e_vec)
        if sim is None:
            continue

        any_compared = True
        if sim > max_cos:
            max_cos = sim

    if not any_compared:
        _warn("No comparable published embeddings - forcing novelty_score=0.0 (conservative fail)", lp)
        return 0.0, 1.0, True

    return 1.0 - max_cos, max_cos, False


@dataclass
class _GateResult:
    session_id: str
    topic: str
    gate_passed: bool
    reasons: list[str]
    convergence_score: float
    confidence_score: float
    conflicts_resolved: bool
    domain_score: float
    novelty_score: float
    max_similarity_to_published: float
    synthesis_tag: str
    seeded_by: list[str]
    gate_timestamp: str


def _evaluate_session(
    node: dict[str, Any],
    synthesis_text: Optional[str],
    domain_text: str,
    published_index: list[dict[str, Any]],
    emb: _EmbeddingCache,
    skip_novelty: bool,
    lp: Optional[Path],
) -> _GateResult:
    ts = _utc_iso()
    session_id = str(node.get("session_id", "")).strip()
    topic = str(node.get("topic", "")).strip()
    metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
    synthesis_summary = str(node.get("final_synthesis", node.get("synthesis_summary", ""))).strip()
    reasons: list[str] = []

    if not session_id:
        reasons.append("missing_session_id")
    if not topic:
        reasons.append("missing_topic")

    conv = _safe_float(metrics.get("convergence"))
    if conv is None:
        reasons.append("missing_convergence")
        conv_val = 0.0
    else:
        conv_val = conv
        if conv_val < THRESH_CONVERGENCE:
            reasons.append(f"convergence_below_threshold:{conv_val:.4f}<{THRESH_CONVERGENCE}")

    conf = _safe_float(metrics.get("confidence"))
    if conf is None:
        reasons.append("missing_confidence")
        conf_val = 0.0
    else:
        conf_val = conf
        if conf_val < THRESH_CONFIDENCE:
            reasons.append(f"confidence_below_threshold:{conf_val:.4f}<{THRESH_CONFIDENCE}")

    block, tag_line = ("", "")
    if synthesis_text and session_id:
        block, tag_line = _extract_synthesis_block(synthesis_text, session_id)

    if not tag_line:
        reasons.append("missing_synthesis_tag")

    cr = _conflicts_resolved(block)
    if not cr:
        reasons.append("conflicts_not_resolved_or_missing")

    d_score = _domain_score(topic, domain_text, emb, lp) if topic else None
    if d_score is None:
        reasons.append("missing_domain_score")
        d_val = 0.0
    else:
        d_val = d_score
        if d_val < THRESH_DOMAIN:
            reasons.append(f"domain_below_threshold:{d_val:.4f}<{THRESH_DOMAIN}")

    if skip_novelty:
        _warn("--skip-novelty active: forcing novelty_score=0.0 (conservative fail)", lp)
        novelty_val = 0.0
        max_cos_val = 1.0
        reasons.append("novelty_skipped")
    else:
        novelty_val, max_cos_val, degraded = _novelty_score(
            topic,
            synthesis_summary,
            published_index,
            emb,
            lp,
        )
        if degraded:
            reasons.append("novelty_unavailable_treated_as_zero")

    if max_cos_val >= THRESH_MAX_SIM_PUBLISHED:
        reasons.append(f"too_similar_to_published:{max_cos_val:.4f}>={THRESH_MAX_SIM_PUBLISHED}")
    if novelty_val < THRESH_NOVELTY:
        reasons.append(f"novelty_below_threshold:{novelty_val:.4f}<{THRESH_NOVELTY}")

    seeded_by = node.get("seeded_by", [])
    if not isinstance(seeded_by, list):
        seeded_by = []
    seeded_by = [str(x).strip() for x in seeded_by if str(x).strip()]

    return _GateResult(
        session_id=session_id,
        topic=topic,
        gate_passed=(len(reasons) == 0),
        reasons=reasons,
        convergence_score=conv_val,
        confidence_score=conf_val,
        conflicts_resolved=bool(cr),
        domain_score=d_val,
        novelty_score=novelty_val,
        max_similarity_to_published=max_cos_val,
        synthesis_tag=tag_line,
        seeded_by=seeded_by,
        gate_timestamp=ts,
    )


def _manifest_dict(gr: _GateResult) -> dict[str, Any]:
    return {
        "session_id": gr.session_id,
        "topic": gr.topic,
        "convergence_score": float(gr.convergence_score),
        "confidence_score": float(gr.confidence_score),
        "conflicts_resolved": bool(gr.conflicts_resolved),
        "domain_score": float(gr.domain_score),
        "novelty_score": float(gr.novelty_score),
        "gate_passed": bool(gr.gate_passed),
        "gate_timestamp": gr.gate_timestamp,
        "synthesis_tag": gr.synthesis_tag,
        "seeded_by": gr.seeded_by,
        "status": "queued",
    }


def _gate_log_dict(
    gr: _GateResult,
    root: str,
    manifest_path: str,
    manifest_written: bool,
    manifest_skipped: bool,
) -> dict[str, Any]:
    return {
        "timestamp": gr.gate_timestamp,
        "SOVEREIGN_version": SOVEREIGN_VERSION,
        "root": root,
        "session_id": gr.session_id,
        "topic": gr.topic,
        "gate_passed": bool(gr.gate_passed),
        "reasons": gr.reasons,
        "convergence_score": float(gr.convergence_score),
        "confidence_score": float(gr.confidence_score),
        "conflicts_resolved": bool(gr.conflicts_resolved),
        "domain_score": float(gr.domain_score),
        "novelty_score": float(gr.novelty_score),
        "max_similarity_to_published": float(gr.max_similarity_to_published),
        "synthesis_tag": gr.synthesis_tag,
        "manifest_path": manifest_path,
        "manifest_written": bool(manifest_written),
        "manifest_skipped_existing": bool(manifest_skipped),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SOVEREIGN Phase 9 - Publication Gate")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(r"E:\SOVEREIGN"),
        help=r"SOVEREIGN root directory (default: E:\SOVEREIGN)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without writing manifests/logs")
    parser.add_argument("--skip-novelty", action="store_true", help="Skip novelty embedding calls and fail conservatively")
    parser.add_argument("--ollama", default=OLLAMA_BASE_DEFAULT, help="Ollama base URL")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(args.root).resolve()
    dry_run = bool(args.dry_run)
    skip_novelty = bool(args.skip_novelty)
    ollama_base = str(args.ollama).strip() or OLLAMA_BASE_DEFAULT

    p = _paths(root)
    lp = p["log_file"]

    _info(
        f"=== Publication Gate starting | root={root} | dry_run={dry_run} | skip_novelty={skip_novelty} ===",
        lp,
    )

    if not dry_run:
        p["pub_queue"].mkdir(parents=True, exist_ok=True)
        p["pub_index_dir"].mkdir(parents=True, exist_ok=True)

    # Read-only consumer: publication gate must never write session_graph.json.
    nodes = _read_session_graph(p["session_graph"], lp)
    if nodes is None:
        return 1
    if not nodes:
        _error("No sessions in session_graph.json - exiting", lp)
        return 1

    synthesis_text = _read_text(p["synthesis_log"], lp)
    if synthesis_text is None:
        _warn("synthesis.txt missing - synthesis-tag checks will fail closed", lp)

    domain_text = (_read_text(p["domain_file"], lp) or "").strip()
    if not domain_text:
        _warn("domain.txt missing or empty - domain score will fail closed", lp)

    published_index = _read_jsonl(p["pub_index"], lp)
    _info(f"Published index entries loaded: {len(published_index)}", lp)

    emb = _EmbeddingCache(ollama_base, lp)

    def _sort_key(n: dict[str, Any]) -> tuple[str, str]:
        return (str(n.get("timestamp", "")).strip(), str(n.get("session_id", "")).strip())

    evaluated = completed = passed = wrote = skipped = 0

    for node in sorted(nodes, key=_sort_key):
        evaluated += 1
        if not _session_is_completed(node):
            continue
        completed += 1

        session_id = str(node.get("session_id", "")).strip()
        topic = str(node.get("topic", "")).strip()
        _info(f"Evaluating session_id={session_id} topic='{topic}'", lp)

        gr = _evaluate_session(
            node=node,
            synthesis_text=synthesis_text,
            domain_text=domain_text,
            published_index=published_index,
            emb=emb,
            skip_novelty=skip_novelty,
            lp=lp,
        )

        outcome = "PASSED" if gr.gate_passed else "FAILED"
        _info(
            f"Gate {outcome}: session_id={session_id} | conv={gr.convergence_score} conf={gr.confidence_score} "
            f"domain={gr.domain_score} novelty={gr.novelty_score} | reasons={gr.reasons}",
            lp,
        )

        manifest_path = p["pub_queue"] / f"{session_id}_manifest.json"
        manifest_written = False
        manifest_skipped = False

        if gr.gate_passed:
            passed += 1
            if manifest_path.exists():
                _info(f"Manifest exists, skipping (append-only): {manifest_path}", lp)
                manifest_skipped = True
                skipped += 1
            else:
                ok = _atomic_write_json(manifest_path, _manifest_dict(gr), dry_run, lp)
                manifest_written = bool(ok) and not dry_run
                if manifest_written:
                    wrote += 1

        _append_jsonl(
            p["gate_log"],
            _gate_log_dict(gr, str(root), str(manifest_path), manifest_written, manifest_skipped),
            dry_run,
            lp,
        )

    _info(
        f"=== Publication Gate complete | evaluated={evaluated} completed={completed} "
        f"passed={passed} manifests_written={wrote} manifests_skipped={skipped} ===",
        lp,
    )

    print(
        json.dumps(
            {
                "root": str(root),
                "SOVEREIGN_version": SOVEREIGN_VERSION,
                "schema_version": SCHEMA_VERSION,
                "timestamp": _utc_iso(),
                "dry_run": dry_run,
                "skip_novelty": skip_novelty,
                "session_graph_nodes": len(nodes),
                "completed_nodes": completed,
                "passed_gate": passed,
                "manifests_written": wrote,
                "manifests_skipped": skipped,
                "published_index_entries": len(published_index),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
