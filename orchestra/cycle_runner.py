# DEPRECATED: superseded by cycle_runner_v3.py. Do not run. Kept for reference only.
# cycle_runner.py - SOVEREIGN Phase 8 (6/6)
#
# Orchestrates the full Phase 8 research cycle:
#   1.  Scan corpus\ for unprocessed documents (corpus_scan)
#   2.  Extract 3-5 debate topics per document (topic_extractor)
#   3.  Check each topic against domain.txt (domain_checker, threshold 0.70)
#   4.  Write approved topics to orchestra_queue.txt
#   5.  ORCHESTRA runs sessions (broker handles debate; we do not call broker directly)
#   6.  Read completed session synthesis from praxis\logs\synthesis.txt
#       Read convergence score from session_graph.json
#   7.  Quality gate: convergence >= 0.85, confidence >= 0.75, conflicts empty
#   8.  On pass: call praxis_commit.py with extracted memory entries
#   9.  Write approved synthesis as corpus document (corpus_writer)
#  10.  Repeat â€” generated\ documents become input for the next cycle
#
# Flags:
#   --auto-approve    skip human approval gate, write directly to corpus\generated\
#   --dry-run         log all decisions, write nothing to disk
#   --max-topics N    cap topics added to queue per cycle (default: 20)
#   --root            override E:\SOVEREIGN\ root (for testing)
#
# Autonomous stop conditions (checked after each completed session):
#   - STOP file detected at E:\SOVEREIGN\corpus\STOP
#   - Corpus saturated: no new documents and queue is empty
#   - Quality gate failure rate > 50% over last 10 sessions
#
# On autonomous stop: logs reason, exits cleanly.
# Nothing in Phase 8 aborts the cycle except these three conditions.
# All individual step failures are logged and the cycle continues.
#
# All decisions logged to: E:\SOVEREIGN\logs\corpus_build_log.txt (append-only)
#
# UTF-8 without BOM. Windows paths. No cloud APIs. No external dependencies
# beyond the Phase 8 sibling modules.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

from typing import Deque, Dict, List, Optional, Tuple

import corpus_scan
import corpus_writer
import domain_checker
import quality_gate
import topic_extractor


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PHASE8_VERSION        = "phase8-1.0"
DEFAULT_ROOT          = r"E:\SOVEREIGN"
LOG_DIR               = "logs"
LOG_FILENAME          = "corpus_build_log.txt"
ORCHESTRA_SUBDIR      = "orchestra"
PRAXIS_SUBDIR         = "praxis"
CORPUS_SUBDIR         = "corpus"

ORCHESTRA_QUEUE_FILE  = "orchestra_queue.txt"
SESSION_GRAPH_FILE    = "session_graph.json"
SYNTHESIS_LOG_FILE    = os.path.join("logs", "synthesis.txt")

DEFAULT_MAX_TOPICS    = 20
GATE_WINDOW           = 10        # sessions to consider for failure-rate check
GATE_FAILURE_RATE_MAX = 0.50      # stop if > 50% of last N sessions failed gate

# How long to wait between polls when waiting for ORCHESTRA to complete a session
ORCHESTRA_POLL_SEC    = 5
# Maximum time to wait for ORCHESTRA to complete a session (seconds)
ORCHESTRA_TIMEOUT_SEC = 3600      # 1 hour


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _log_path(root: str) -> str:
    return os.path.join(root, LOG_DIR, LOG_FILENAME)

def _stop_file(root: str) -> str:
    return os.path.join(root, CORPUS_SUBDIR, "STOP")

def _orchestra_queue(root: str) -> str:
    return os.path.join(root, ORCHESTRA_SUBDIR, ORCHESTRA_QUEUE_FILE)

def _session_graph(root: str) -> str:
    return os.path.join(root, ORCHESTRA_SUBDIR, SESSION_GRAPH_FILE)

def _synthesis_file(root: str) -> str:
    return os.path.join(root, PRAXIS_SUBDIR, SYNTHESIS_LOG_FILE)

def _praxis_commit_script(root: str) -> str:
    return os.path.join(root, PRAXIS_SUBDIR, "praxis_commit.py")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().replace(microsecond=0).isoformat()

def _log(root: str, msg: str) -> None:
    line = f"[{_ts()}] [CYCLE_RUNNER] {msg}\n"
    lp   = _log_path(root)
    try:
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"[CYCLE_RUNNER] WARNING: log write failed: {exc}", file=sys.stderr)
    print(line.rstrip())


# ---------------------------------------------------------------------------
# STOP file check
# ---------------------------------------------------------------------------

def _stop_requested(root: str) -> bool:
    return os.path.isfile(_stop_file(root))


# ---------------------------------------------------------------------------
# Gate failure rate tracker
# ---------------------------------------------------------------------------

class _GateTracker:
    """
    Sliding window tracker for quality gate outcomes.
    Maintains the last GATE_WINDOW results (True=passed, False=failed).
    """
    def __init__(self, window: int = GATE_WINDOW) -> None:
        self._window:  int              = window
        self._results: Deque[bool]      = deque(maxlen=window)

    def record(self, passed: bool) -> None:
        self._results.append(passed)

    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        return sum(1 for r in self._results if not r) / len(self._results)

    def should_stop(self) -> bool:
        # Only trigger after the window is full (need enough data)
        return (
            len(self._results) >= self._window
            and self.failure_rate() > GATE_FAILURE_RATE_MAX
        )


# ---------------------------------------------------------------------------
# orchestra_queue.txt helpers
# ---------------------------------------------------------------------------

def _write_topics_to_queue(
    topics:   List[str],
    root:     str,
    dry_run:  bool,
) -> int:
    """
    Append approved topics to orchestra_queue.txt.
    Returns number of topics written (0 on dry_run or error).
    """
    queue_path = _orchestra_queue(root)

    if dry_run:
        for t in topics:
            _log(root, f"[DRY-RUN] Would queue topic: {t[:120]}")
        return 0

    try:
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        with open(queue_path, "a", encoding="utf-8", newline="\n") as fh:
            for t in topics:
                fh.write(t.strip() + "\n")
        return len(topics)
    except OSError as exc:
        _log(root, f"ERROR: could not write to orchestra_queue.txt: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Session graph reader â€” get latest completed session
# ---------------------------------------------------------------------------

class _GraphState:
    """Tri-state result for _read_graph â€” distinguishes missing from corrupt."""
    MISSING = "missing"   # file does not exist (startup condition â€” just wait)
    CORRUPT = "corrupt"   # file exists but unreadable/invalid (fail-safe â€” treat as concurrent)
    OK      = "ok"        # file loaded successfully


def _read_graph(root: str) -> tuple:
    """
    Load session_graph.json.
    Returns (_GraphState, data) where data is List[Dict] or None.
    Callers use the state to distinguish startup (MISSING) from corruption (CORRUPT)
    without a separate isfile probe that introduces a TOCTOU race.
    """
    gp = _session_graph(root)
    if not os.path.isfile(gp):
        return _GraphState.MISSING, None
    try:
        with open(gp, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return _GraphState.OK, data
        return _GraphState.CORRUPT, None
    except Exception:
        return _GraphState.CORRUPT, None


# Sessions we consider "done and usable" â€” added to the completed list.
_COMPLETED_STATUSES: frozenset = frozenset({"complete", "completed", "done"})

# Other terminal statuses â€” not active, not usable as completed.
# Blank/unknown status counts as active (conservative): ORCHESTRA writing
# nodes before setting status must never silently re-enable FIFO fallback.
_TERMINAL_STATUSES: frozenset = frozenset({
    "failed", "error",
    "cancelled", "canceled",
    "skipped",
})


def _get_all_session_ids(root: str) -> set:
    """
    Return the set of ALL non-empty session_id values in session_graph.json
    regardless of status. IDs are stripped; blank entries are excluded so that
    broker nodes written before a session_id is set never poison known_sessions
    and accidentally suppress the "baseline once" snapshot in the wait loop.
    """
    state, data = _read_graph(root)
    if state != _GraphState.OK:
        return set()
    out = set()
    for n in data:
        if not isinstance(n, dict):
            continue
        sid = str(n.get("session_id", "")).strip()
        if sid:
            out.add(sid)
    return out

def _get_latest_synthesis_for_session(session_id: str, root: str, allow_fifo_fallback: bool = True) -> Optional[str]:
    """
    Read the synthesis log and extract the most recent synthesis block for session_id.
    broker.ps1 writes blocks tagged: [SYNTH S{n} {ts}]
    Returns synthesis text or None if not found.
    """
    synth_path = _synthesis_file(root)
    if not os.path.isfile(synth_path):
        _log(root, f"WARNING: synthesis file not found: {synth_path}")
        return None

    try:
        with open(synth_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        _log(root, f"ERROR: cannot read synthesis file: {exc}")
        return None

    # Find all synthesis blocks for this session
    # broker.ps1 format: [SYNTH S{session_id} {ts}]\n{text}\n
    import re
    sid = re.escape(str(session_id).strip())
    pattern = rf"\[SYNTH S{sid} [^\]]+\]\r?\n(.*?)(?=\[SYNTH |\[RRR-SYNTH |\Z)"
    matches = re.findall(pattern, content, re.DOTALL)

    if not matches:
        if not allow_fifo_fallback:
            # Concurrency was detected during session polling â€” refusing to guess
            # which synthesis block belongs to this session. A gate failure is
            # recoverable; a misassociated synthesis committed to PRAXIS is not.
            _log(root, f"ERROR: no synthesis block found for session_id={session_id} "
                       f"and FIFO fallback is disabled (concurrent sessions detected). "
                       f"Returning None.")
            return None
        # Serial FIFO fallback: safe only when ORCHESTRA is single-session.
        # broker.ps1 may use sequence tags (S1, S2) rather than real session IDs.
        _log(root, f"WARNING: no synthesis block found for session_id={session_id}. "
                   f"Falling back to latest synthesis block (FIFO serial assumption).")
        any_pattern = r"\[SYNTH [^\]]+\]\r?\n(.*?)(?=\[SYNTH |\[RRR-SYNTH |\Z)"
        any_matches = re.findall(any_pattern, content, re.DOTALL)
        if any_matches:
            return any_matches[-1].strip()
        _log(root, f"ERROR: no synthesis blocks at all in {synth_path}")
        return None

    # Last match is the most recent synthesis for this session
    return matches[-1].strip()


# ---------------------------------------------------------------------------
# PRAXIS commit helper
# ---------------------------------------------------------------------------

def _run_praxis_commit(
    session_id: str,
    topic:      str,
    synthesis:  str,
    root:       str,
    dry_run:    bool,
) -> bool:
    """
    Call praxis_commit.py via subprocess to write approved synthesis to PRAXIS memory.
    Called only AFTER the quality gate passes.
    Returns True on success, False on any failure.
    Non-fatal: failure is logged but does not undo the gate decision.
    """
    if dry_run:
        _log(root, f"[DRY-RUN] Would run PRAXIS commit for session={session_id}")
        return True

    commit_script = _praxis_commit_script(root)
    if not os.path.isfile(commit_script):
        _log(root, f"WARNING: praxis_commit.py not found at {commit_script}. Skipping commit.")
        return False

    praxis_dir  = os.path.join(root, PRAXIS_SUBDIR)
    commit_json = os.path.join(praxis_dir, "commit.json")
    done_txt    = os.path.join(praxis_dir, "commit_done.txt")
    stderr_txt  = os.path.join(praxis_dir, "commit_stderr.txt")

    payload = {
        "session_id": session_id,
        "topic":      topic,
        "entries": [
            {"type": "synthesis", "content": synthesis}
        ],
    }

    for f in (done_txt, stderr_txt):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    try:
        with open(commit_json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError as exc:
        _log(root, f"ERROR: cannot write commit.json: {exc}")
        return False

    try:
        result = subprocess.run(
            [sys.executable, commit_script],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        _log(root, f"ERROR: praxis_commit.py timed out for session={session_id}")
        return False
    except OSError as exc:
        _log(root, f"ERROR: cannot run praxis_commit.py: {exc}")
        return False

    success = False
    if os.path.isfile(done_txt):
        try:
            with open(done_txt, "r", encoding="utf-8") as fh:
                done_text = fh.read().strip()
            if done_text == "OK":
                success = True
                _log(root, f"PRAXIS commit OK for session={session_id}")
            else:
                _log(root, f"PRAXIS commit returned: {done_text[:200]}")
        except OSError:
            pass

    if result.returncode != 0 and not success:
        _log(root, f"ERROR: praxis_commit.py exit={result.returncode}. "
                   f"stderr: {result.stderr[:200]}")

    for f in (commit_json, done_txt, stderr_txt):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    return success


# ---------------------------------------------------------------------------
# Wait for ORCHESTRA to complete a session
# ---------------------------------------------------------------------------

def _wait_for_session_completion(
    expected_topic: str,
    known_sessions: set,
    root:           str,
    timeout_sec:    int = ORCHESTRA_TIMEOUT_SEC,
) -> Tuple[Optional[Dict], bool]:
    """
    Poll session_graph.json until a new completed session appears for expected_topic.
    Returns (node: Optional[Dict], concurrent_seen: bool).

    Matching strategy (in priority order):
      1. Exact match: node_topic == expected (stripped)
      2. Substring match: expected in node_topic or node_topic in expected
      3. FIFO fallback: accept first unseen completed session after a full poll
         cycle with no match; logs topic mismatch for audit.

    FIFO fallback handles ORCHESTRA normalizing, prefixing, or truncating
    topic strings. The mismatch is logged so you can detect drift.

    Returns the session node dict, or None on timeout.
    known_sessions: set of session_ids already seen before this wait.
    """
    deadline         = time.monotonic() + timeout_sec
    expected         = expected_topic.strip()
    first_poll       = True
    concurrent_seen  = False   # don't FIFO on the very first poll â€” give exact/substr a chance
    _log(root, f"Waiting for ORCHESTRA session for topic: {expected[:80]}...")

    while time.monotonic() < deadline:
        if _stop_requested(root):
            _log(root, "STOP file detected while waiting for session completion.")
            return None, concurrent_seen

        # One graph read per poll â€” tri-state result eliminates TOCTOU race
        # between separate isfile check and open. Missing = wait; corrupt = fail-safe.
        graph_state, graph = _read_graph(root)

        if graph_state == _GraphState.CORRUPT:
            concurrent_seen = True
            _log(root, "WARNING: session_graph.json exists but is unreadable. "
                       "Treating as concurrent; FIFO fallback disabled.")
            completed = []
        elif graph_state == _GraphState.MISSING:
            # Normal at startup â€” graph hasn't been created yet. Just wait.
            completed = []
        else:
            # Baseline known_sessions exactly once: if the graph was missing/corrupt
            # when _process_document seeded known_sessions, the baseline may be empty.
            # On the first successful graph read, snapshot all current session IDs so
            # pre-existing sessions are never treated as new-unseen.
            # After this, only sessions added by known_sessions.add(sid) at match
            # time extend the baseline â€” re-snapshotting every poll would zero out
            # new_unseen and stall the loop permanently.
            if first_poll and not known_sessions:
                for n in graph:
                    if isinstance(n, dict):
                        sid0 = str(n.get("session_id", "")).strip()
                        if sid0:
                            known_sessions.add(sid0)

            # Derive completed list and active count in the same pass.
            # Blank/unknown status counts as active â€” see _TERMINAL_STATUSES.
            completed = []
            active    = 0
            for n in graph:
                if not isinstance(n, dict):
                    continue
                st = str(n.get("status", "")).strip().lower()
                if st in _COMPLETED_STATUSES:
                    completed.append(n)
                elif st in _TERMINAL_STATUSES:
                    pass   # non-active terminal â€” neither completed nor active
                else:
                    active += 1  # blank, unknown, or any non-terminal
            if active > 1:
                concurrent_seen = True
                _log(root, f"WARNING: {active} active/unknown-status sessions detected. "
                           "FIFO fallback disabled for this wait.")

        # Build new_unseen: completed sessions not yet in known_sessions.
        # Explicitly skip nodes with empty/missing session_id to prevent
        # phantom nodes (broker wrote node before setting ID) from polluting
        # the loop or matching via empty-string equality.
        new_unseen = []
        for n in completed:
            sid = str(n.get("session_id", "")).strip()
            if not sid:
                continue
            if sid not in known_sessions:
                new_unseen.append(n)

        # Poll-local tripwire: belt-and-suspenders when broker only writes
        # status on completion and in-progress nodes aren't present.
        if len(new_unseen) > 1:
            concurrent_seen = True
            _log(root, f"WARNING: {len(new_unseen)} new completed sessions in one poll. "
                       "FIFO fallback disabled.")

        # Priority 1 & 2: exact or substring match (always attempted)
        for node in new_unseen:
            node_topic = str(node.get("topic", "")).strip()
            sid        = str(node.get("session_id", "")).strip()

            if node_topic == expected:
                _log(root, f"Session {sid} matched (exact): {expected[:80]}")
                known_sessions.add(sid)
                return node, concurrent_seen

            if expected and (expected in node_topic or node_topic in expected):
                _log(root, f"Session {sid} matched (substring): {expected[:80]}")
                known_sessions.add(sid)
                return node, concurrent_seen

        # Priority 3: FIFO fallback â€” only when serial execution is confirmed.
        # Uses concurrent_seen (sticky across polls) not concurrent (poll-local)
        # so a single quiet poll cannot re-enable fallback after concurrency fires.
        if not first_poll and new_unseen and not concurrent_seen:
            node       = new_unseen[0]
            node_topic = str(node.get("topic", "")).strip()
            sid        = str(node.get("session_id", "")).strip()
            _log(root, f"Session {sid} accepted (FIFO fallback). "
                       f"Topic mismatch: expected={expected[:80]!r} got={node_topic[:80]!r}")
            known_sessions.add(sid)
            return node, concurrent_seen

        first_poll = False
        time.sleep(ORCHESTRA_POLL_SEC)

    _log(root, f"TIMEOUT waiting for session completion after {timeout_sec}s.")
    return None, concurrent_seen


# ---------------------------------------------------------------------------
# Single-document processing
# ---------------------------------------------------------------------------

def _process_document(
    doc_path:              str,
    root:                  str,
    dry_run:               bool,
    auto_approve:          bool,
    max_topics:            int,
    gate_tracker:          _GateTracker,
    topics_queued_count:   List[int],   # mutable [n] for cross-call accumulation
) -> List[bool]:
    """
    Run the full pipeline for one corpus document.
    Returns list of gate outcomes (True/False) for each topic debated.
    """
    outcomes: List[bool] = []
    fname = os.path.basename(doc_path)
    _log(root, f"Processing document: {fname}")

    if topics_queued_count[0] >= max_topics:
        _log(root, f"Max topics cap ({max_topics}) reached. Skipping {fname}.")
        return outcomes

    # ---- Step 2: extract topics ------------------------------------------
    topics = topic_extractor.extract_topics(doc_path, root=root)
    if not topics:
        _log(root, f"No topics extracted from {fname}. Marking failed.")
        corpus_writer.mark_source_failed(doc_path, None, "no topics extracted", root)
        return outcomes

    _log(root, f"Extracted {len(topics)} topic(s) from {fname}.")

    # ---- Step 3: domain check -------------------------------------------
    approved_topics: List[str] = []
    for topic in topics:
        if topics_queued_count[0] + len(approved_topics) >= max_topics:
            _log(root, f"Max topics cap reached mid-document. Stopping topic check for {fname}.")
            break
        result = domain_checker.check_topic(topic, root=root)
        if result["approved"]:
            approved_topics.append(topic)
        else:
            _log(root, f"REJECTED by domain (score={result['score']:.4f}): {topic[:120]}")

    if not approved_topics:
        _log(root, f"All topics rejected by domain checker for {fname}. Marking skipped.")
        corpus_scan.update_entry_status(
            root=root, path=doc_path, status="skipped",
            session_id=None,
            note="all topics rejected by domain checker",
        )
        return outcomes

    _log(root, f"{len(approved_topics)} topic(s) approved for {fname}.")

    # ---- Step 4: write to orchestra queue --------------------------------
    if dry_run:
        for t in approved_topics:
            _log(root, f"[DRY-RUN] Would queue topic: {t[:120]}")
        topics_queued_count[0] += len(approved_topics)
        _log(root, f"[DRY-RUN] Skipping ORCHESTRA wait and gate for {fname}.")
        return outcomes
    else:
        written = _write_topics_to_queue(approved_topics, root, dry_run=False)
        topics_queued_count[0] += written

    # ---- Steps 5-9: per-topic session processing -------------------------
    # Seed known_sessions from ALL statuses (not just complete) so that nodes
    # broker wrote early (pending/in_progress) are never treated as new unseen.
    known_sessions = _get_all_session_ids(root)

    for topic in approved_topics:
        if _stop_requested(root):
            _log(root, "STOP file detected. Halting topic processing.")
            break

        # ---- Step 5: wait for ORCHESTRA session -------------------------
        node, concurrent_seen = _wait_for_session_completion(topic, known_sessions, root)
        if node is None:
            _log(root, f"No session completed for topic: {topic[:80]}. Skipping.")
            outcomes.append(False)
            gate_tracker.record(False)
            continue

        session_id     = str(node.get("session_id", "")).strip()
        # Use the topic broker actually debated, not what we queued.
        # On exact/substring match these are identical. On FIFO mismatch,
        # using the queue topic would write a lie into corpus and PRAXIS.
        node_topic      = str(node.get("topic", "")).strip()
        effective_topic = node_topic if node_topic else topic

        # ---- Step 6: read synthesis -------------------------------------
        synthesis = _get_latest_synthesis_for_session(session_id, root, allow_fifo_fallback=not concurrent_seen)
        if not synthesis:
            _log(root, f"No synthesis found for session={session_id}. Gate will fail.")
            outcomes.append(False)
            gate_tracker.record(False)
            corpus_writer.mark_source_failed(doc_path, session_id, "no synthesis found", root)
            continue

        # ---- Step 7: quality gate (BEFORE any commit) -------------------
        # Confidence comes from the session node written by broker.ps1.
        # Default is 0.0 (fail-closed): missing confidence blocks the gate.
        # If broker writes confidence_score into the session node, it will
        # be used; otherwise the gate fails on confidence, which is correct.
        confidence = 0.0
        raw_conf   = node.get("confidence_score") or node.get("confidence")
        if raw_conf is not None:
            try:
                confidence = float(raw_conf)
            except (TypeError, ValueError):
                _log(root, f"WARNING: non-numeric confidence in session node: {raw_conf!r}. "
                           f"Defaulting to 0.0 (gate will fail on confidence).")

        gate_result = quality_gate.evaluate(
            synthesis  = synthesis,
            session_id = session_id,
            confidence = confidence,
            root       = root,
        )

        gate_tracker.record(gate_result["passed"])
        outcomes.append(gate_result["passed"])

        if not gate_result["passed"]:
            reasons = "; ".join(gate_result.get("reasons", []))
            _log(root, f"Gate FAILED session={session_id}: {reasons}")
            corpus_writer.mark_source_failed(
                doc_path, session_id,
                f"quality gate failed: {reasons}", root,
            )
            continue

        # ---- Step 8: PRAXIS commit (only on gate pass) ------------------
        commit_ok = _run_praxis_commit(
            session_id = session_id,
            topic      = effective_topic,
            synthesis  = synthesis,
            root       = root,
            dry_run    = dry_run,
        )
        if not commit_ok:
            _log(root, f"WARNING: PRAXIS commit failed for session={session_id}. "
                       "Corpus document will still be written.")

        # ---- Step 9: write corpus document ------------------------------
        session_meta = {
            "session_id":         session_id,
            "topic":              effective_topic,
            "source_documents":   [doc_path],
            "seeded_by_sessions": list(node.get("seeded_by", [])),
            "topics_extracted":   len(topics),
        }

        dest = corpus_writer.write_corpus_document(
            synthesis    = synthesis,
            gate_result  = gate_result,
            session_meta = session_meta,
            root         = root,
            auto_approve = auto_approve,
        )

        if dest:
            _log(root, f"Corpus document written: {os.path.basename(dest)}")
        else:
            _log(root, f"WARNING: corpus_writer returned None for session={session_id}")

    return outcomes


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def run_cycle(
    root:         str  = DEFAULT_ROOT,
    auto_approve: bool = False,
    dry_run:      bool = False,
    max_topics:   int  = DEFAULT_MAX_TOPICS,
) -> None:
    r"""
    Execute one full Phase 8 research cycle.

    Autonomous stop conditions:
      1. STOP file at E:\SOVEREIGN\corpus\STOP
      2. Corpus saturated (no new documents, queue empty)
      3. Gate failure rate > 50% over last 10 sessions

    Never raises. All errors are logged and the cycle continues unless
    a stop condition is met.
    """
    _log(root, "=" * 60)
    _log(root, f"Cycle started. auto_approve={auto_approve} dry_run={dry_run} max_topics={max_topics}")

    gate_tracker = _GateTracker(GATE_WINDOW)

    # ---- Step 1: scan corpus ---------------------------------------------
    if _stop_requested(root):
        _log(root, "STOP file detected at cycle start. Exiting.")
        return

    try:
        new_docs = corpus_scan.scan_corpus(root)
    except Exception as exc:
        _log(root, f"ERROR: corpus scan failed: {type(exc).__name__}: {exc}")
        new_docs = []

    queued = corpus_scan.get_queued_paths(root)

    _log(root, f"New docs this scan: {len(new_docs)}. Total queued: {len(queued)}.")

    # ---- Saturation check -----------------------------------------------
    if not queued:
        _log(root, "STOP: corpus saturated â€” no new documents and queue is empty.")
        return

    # topics_queued_count is a mutable list so _process_document can update it
    topics_queued_count = [0]

    # ---- Process each queued document -----------------------------------
    for doc_path in queued:
        if _stop_requested(root):
            _log(root, "STOP file detected. Halting cycle.")
            break

        if topics_queued_count[0] >= max_topics:
            _log(root, f"Max topics cap ({max_topics}) reached. Ending document loop.")
            break

        if gate_tracker.should_stop():
            _log(root, f"STOP: quality gate failure rate "
                       f"{gate_tracker.failure_rate():.1%} > "
                       f"{GATE_FAILURE_RATE_MAX:.0%} over last {GATE_WINDOW} sessions.")
            break

        try:
            _process_document(
                doc_path            = doc_path,
                root                = root,
                dry_run             = dry_run,
                auto_approve        = auto_approve,
                max_topics          = max_topics,
                gate_tracker        = gate_tracker,
                topics_queued_count = topics_queued_count,
            )
        except Exception as exc:
            _log(root, f"ERROR: unhandled exception processing {doc_path}: "
                       f"{type(exc).__name__}: {exc}")
            corpus_writer.mark_source_failed(
                doc_path, None,
                f"unhandled exception: {type(exc).__name__}: {exc}",
                root,
            )
            continue

    _log(root, f"Cycle complete. Topics queued: {topics_queued_count[0]}. "
               f"Gate failure rate: {gate_tracker.failure_rate():.1%}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="SOVEREIGN Phase 8 â€” cycle_runner.py: automated research cycle orchestrator."
    )
    ap.add_argument(
        "--auto-approve", action="store_true",
        help=r"Write directly to corpus\generated\ instead of corpus\pending_approval\."
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Log all decisions and queue topics, but write nothing to disk."
    )
    ap.add_argument(
        "--max-topics", type=int, default=DEFAULT_MAX_TOPICS,
        metavar="N",
        help=f"Maximum topics added to queue per cycle (default: {DEFAULT_MAX_TOPICS})."
    )
    ap.add_argument(
        "--root", default=DEFAULT_ROOT,
        help=f"Override SOVEREIGN root directory (default: {DEFAULT_ROOT})."
    )
    return ap.parse_args()


def main() -> int:
    args = _parse_args()

    if args.max_topics < 1:
        print("[CYCLE_RUNNER] --max-topics must be >= 1.", file=sys.stderr)
        return 1

    try:
        run_cycle(
            root         = args.root,
            auto_approve = args.auto_approve,
            dry_run      = args.dry_run,
            max_topics   = args.max_topics,
        )
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C â€” same as STOP file semantics
        print("\n[CYCLE_RUNNER] Interrupted by user.", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
