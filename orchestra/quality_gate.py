#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
quality_gate.py
SOVEREIGN canonical quality gate

Purpose
-------
Evaluates session_graph.json and decides whether a selected session
passes quality thresholds for downstream publication.

Canonical session_graph.json shape:
{
  "schema_version": "1.0",
  "generated_at": "...",
  "sessions": [...]
}

Each session must include:
  session_id, status, metrics.convergence, metrics.confidence

Only one terminal success state is valid: "completed"

CLI
---
python E:\\SOVEREIGN\\quality_gate.py
python E:\\SOVEREIGN\\quality_gate.py --session-id <ID>
python E:\\SOVEREIGN\\quality_gate.py --min-convergence 0.80 --min-confidence 0.70
python E:\\SOVEREIGN\\quality_gate.py --root E:\\SOVEREIGN --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0"
VALID_SUCCESS_STATUS = "completed"
MODULE_NAME = "QUALITY_GATE"
MAX_LOG_BYTES = 10 * 1024 * 1024

# Earliest sortable timestamp fallback for malformed/missing values.
TIMESTAMP_FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Default paths - relative to --root
# ---------------------------------------------------------------------------

def _defaults(root: Path) -> Dict[str, Path]:
    return {
        "session_graph_path": root / "orchestra" / "session_graph.json",
        "output_path": root / "scheduler" / "state" / "quality_gate_result.json",
        "log_file": root / "logs" / "quality_gate_log.txt",
    }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            if not payload.endswith("\n"):
                fh.write("\n")
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise


def _rotate_log_if_needed(log_file: Path) -> None:
    try:
        if not log_file.exists():
            return
        if log_file.stat().st_size <= MAX_LOG_BYTES:
            return
        backup = Path(str(log_file) + ".1")
        if backup.exists():
            backup.unlink()
        os.replace(str(log_file), str(backup))
    except OSError:
        pass


def log(msg: str, log_file: Path, level: str = "INFO") -> None:
    lvl = str(level or "INFO").upper()
    if lvl not in {"INFO", "WARN", "ERROR"}:
        lvl = "INFO"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] [{MODULE_NAME}] [{lvl}] {msg}"
    print(line, flush=True)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_needed(log_file)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Graph shape validation
# ---------------------------------------------------------------------------

def validate_graph_shape(data: Any) -> List[str]:
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["session_graph must be a JSON object, not a flat array or other type"]

    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(f"schema_version must be '{SCHEMA_VERSION}', got {sv!r}")

    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        errors.append("sessions must be a list")
        return errors

    for i, session in enumerate(sessions):
        prefix = f"sessions[{i}]"
        if not isinstance(session, dict):
            errors.append(f"{prefix} must be an object")
            continue

        if not session.get("session_id"):
            errors.append(f"{prefix}.session_id is required and non-empty")

        if "status" not in session:
            errors.append(f"{prefix}.status is required")

        metrics = session.get("metrics")
        if not isinstance(metrics, dict):
            errors.append(f"{prefix}.metrics is required and must be an object")
            continue

        for key in ("convergence", "confidence"):
            val = metrics.get(key)
            if not isinstance(val, (int, float)):
                errors.append(f"{prefix}.metrics.{key} must be numeric, got {type(val).__name__!r}")
            elif not (0.0 <= float(val) <= 1.0):
                errors.append(f"{prefix}.metrics.{key} = {val} is outside [0.0, 1.0]")

    return errors


# ---------------------------------------------------------------------------
# Session selection
# ---------------------------------------------------------------------------

def _timestamp_key(session: Dict[str, Any]) -> datetime:
    ts = session.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return TIMESTAMP_FLOOR
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return TIMESTAMP_FLOOR


def select_session(
    data: Dict[str, Any],
    session_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    sessions = data.get("sessions", [])
    if not sessions:
        return None

    if session_id:
        for s in sessions:
            if s.get("session_id") == session_id:
                return s
        return None

    # Default: latest by timestamp; fallback order is most recently appended.
    return max(enumerate(sessions), key=lambda pair: (_timestamp_key(pair[1]), pair[0]))[1]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _coerce_metric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def evaluate_session(
    session: Dict[str, Any],
    min_convergence: float,
    min_confidence: float,
) -> Dict[str, Any]:
    session_id = session.get("session_id", "")
    topic = session.get("topic", "")
    status = session.get("status", "")
    metrics = session.get("metrics", {})
    convergence = _coerce_metric(metrics.get("convergence", 0.0))
    confidence = _coerce_metric(metrics.get("confidence", 0.0))

    reasons: List[str] = []
    passed = True

    if status != VALID_SUCCESS_STATUS:
        passed = False
        reasons.append(f"status must be '{VALID_SUCCESS_STATUS}', got '{status}'")

    if convergence < min_convergence:
        passed = False
        reasons.append(f"convergence {convergence:.4f} < threshold {min_convergence:.4f}")

    if confidence < min_confidence:
        passed = False
        reasons.append(f"confidence {confidence:.4f} < threshold {min_confidence:.4f}")

    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_now_iso(),
        "session_id": session_id,
        "topic": topic,
        "status": status,
        "passed": passed,
        "metrics": {
            "convergence": convergence,
            "confidence": confidence,
        },
        "thresholds": {
            "min_convergence": min_convergence,
            "min_confidence": min_confidence,
        },
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SOVEREIGN canonical quality gate")
    parser.add_argument("--root", default=r"E:\SOVEREIGN", help="SOVEREIGN root (default: E:\\SOVEREIGN)")
    parser.add_argument("--session-id", default=None, help="Evaluate a specific session_id (default: latest)")
    parser.add_argument("--min-convergence", type=float, default=0.85)
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--dry-run", action="store_true", help="Print result but do not write output file")
    parser.add_argument("--session-graph-path", default=None)
    parser.add_argument("--output-path", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(args.root).resolve()
    d = _defaults(root)
    lf = d["log_file"]

    graph_path = Path(args.session_graph_path) if args.session_graph_path else d["session_graph_path"]
    output_path = Path(args.output_path) if args.output_path else d["output_path"]

    log(f"=== quality_gate starting | graph={graph_path} ===", lf, "INFO")

    def _fail(code: int, error: str, **extra: Any) -> int:
        result = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_now_iso(),
            "passed": False,
            "error": error,
            **extra,
        }
        log(f"FAIL [{code}]: {error}", lf, "ERROR")
        if not args.dry_run:
            _write_json_atomic(output_path, result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code

    # Read-only consumer: quality gate must never write session_graph.json.
    if not graph_path.exists():
        return _fail(2, f"session_graph not found: {graph_path}")

    try:
        data = load_json(graph_path)
    except Exception as e:
        return _fail(3, f"failed to parse session_graph.json: {e}")

    shape_errors = validate_graph_shape(data)
    if shape_errors:
        return _fail(4, "session_graph shape/schema validation failed", details=shape_errors)

    session = select_session(data, args.session_id)
    if not session:
        return _fail(
            5,
            "no matching session found",
            requested_session_id=args.session_id,
            total_sessions=len(data.get("sessions", [])),
        )

    result = evaluate_session(
        session=session,
        min_convergence=args.min_convergence,
        min_confidence=args.min_confidence,
    )

    verdict = "PASS" if result["passed"] else "FAIL"
    log(
        f"{verdict} | session={result['session_id']} conv={result['metrics']['convergence']} conf={result['metrics']['confidence']}",
        lf,
        "INFO" if result["passed"] else "WARN",
    )
    if result["reasons"]:
        for reason in result["reasons"]:
            log(f"reason: {reason}", lf, "WARN")

    if not args.dry_run:
        _write_json_atomic(output_path, result)
        log(f"result written to {output_path}", lf, "INFO")

    print(json.dumps(result, indent=2, ensure_ascii=False))
    log("=== quality_gate complete ===", lf, "INFO")
    return 0 if result["passed"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
