# praxis_commit.py — SOVEREIGN PRAXIS Phase 3 write-back
# Reads commit.json, chunks+embeds entries, upserts to ChromaDB.
# Exit codes: 0=success, 1=missing file, 2=invalid schema, 3=embed failure
# UTF-8 no-BOM. All HTTP via urllib.request only.

import sys
import os
import json
import hashlib
from datetime import datetime, timezone
import urllib.request

PRAXIS_DIR   = os.path.dirname(os.path.abspath(__file__))
COMMIT_JSON  = os.path.join(PRAXIS_DIR, "commit.json")
DONE_TXT     = os.path.join(PRAXIS_DIR, "commit_done.txt")
DB_DIR       = os.path.join(PRAXIS_DIR, "db")

OLLAMA_URL   = "http://127.0.0.1:11434"
EMBED_MODEL  = "nomic-embed-text"
COLLECTION   = "praxis_memory"

CHUNK_SIZE   = 1800
CHUNK_OVER   = 180

# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────

def write_done(msg: str):
    with open(DONE_TXT, "w", encoding="utf-8") as f:
        f.write(msg)

def fail(code: int, msg: str):
    write_done(f"ERROR: {msg}")
    sys.exit(code)

def embed(text: str) -> list:
    """Embed text via Ollama REST. Returns float list or raises."""
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "embedding" not in body:
        raise ValueError(f"No embedding key in response: {body}")
    return body["embedding"]

def chunk_text(text: str) -> list:
    """
    Split text into overlapping chunks bounded by paragraph breaks
    where possible. Returns list of str.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = text.split("\n\n")

    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= CHUNK_SIZE:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > CHUNK_SIZE:
                start = 0
                while start < len(para):
                    end = start + CHUNK_SIZE
                    chunks.append(para[start:end])
                    start = end - CHUNK_OVER
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks if chunks else [text[:CHUNK_SIZE]]

def make_doc_id(session_id: str, entry_type: str, chunk_index: int) -> str:
    raw = f"{session_id}::{entry_type}::{chunk_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────

def main():
    # ── 1. Load commit.json ──────────────────────────────────
    if not os.path.isfile(COMMIT_JSON):
        fail(1, f"commit.json not found at {COMMIT_JSON}")

    with open(COMMIT_JSON, "r", encoding="utf-8-sig") as f:
        try:
            payload = json.load(f)
        except json.JSONDecodeError as e:
            fail(2, f"commit.json JSON parse error: {e}")

    # ── 2. Validate schema ───────────────────────────────────
    required_top = {"session_id", "topic", "entries"}
    missing = required_top - set(payload.keys())
    if missing:
        fail(2, f"commit.json missing keys: {missing}")

    session_id = str(payload["session_id"]).strip()
    topic      = str(payload["topic"]).strip()
    entries    = payload["entries"]

    if not session_id:
        fail(2, "session_id is empty")
    if not isinstance(entries, list) or len(entries) == 0:
        fail(2, "entries must be a non-empty list")

    valid_types = {"synthesis", "ledger"}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            fail(2, f"entries[{i}] is not a dict")
        if "type" not in entry or "content" not in entry:
            fail(2, f"entries[{i}] missing 'type' or 'content'")
        if entry["type"] not in valid_types:
            fail(2, f"entries[{i}] unknown type '{entry['type']}' — must be synthesis|ledger")

    # ── 3. Open ChromaDB ─────────────────────────────────────
    if not os.path.isdir(DB_DIR):
        fail(2, f"ChromaDB dir not found: {DB_DIR}")

    try:
        import chromadb
    except ImportError:
        fail(2, "chromadb not importable — is it installed?")

    client     = chromadb.PersistentClient(path=DB_DIR)
    collection = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"}
    )

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # ── 4. Chunk, embed, upsert ──────────────────────────────
    total_chunks = 0

    for entry in entries:
        entry_type = entry["type"]
        content    = str(entry["content"]).strip()

        if not content:
            continue

        chunks = chunk_text(content)

        for ci, chunk in enumerate(chunks):
            doc_id = make_doc_id(session_id, entry_type, ci)

            try:
                vector = embed(chunk)
            except Exception as e:
                fail(3, f"Embed failed for {entry_type} chunk {ci}: {e}")

            metadata = {
                "source":      f"session:{session_id}",
                "doc_id":      doc_id,
                "chunk_index": ci,
                "session_id":  session_id,
                "topic":       topic,
                "type":        entry_type,
                "timestamp":   timestamp,
            }

            collection.upsert(
                ids=[doc_id],
                embeddings=[vector],
                documents=[chunk],
                metadatas=[metadata],
            )
            total_chunks += 1

    write_done("OK")
    print(f"[praxis_commit] committed {total_chunks} chunk(s) for session {session_id}", flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()

