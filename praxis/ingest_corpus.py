import os
import sys
import glob
import hashlib
from typing import List, Tuple, Dict

import chromadb
import ollama

# =========================
# CONFIG (CANONICAL)
# =========================
COLLECTION_NAME = "praxis_memory"
EMBED_MODEL = "nomic-embed-text"  # must exist in `ollama list`

SUPPORTED_EXTS = {".txt", ".md", ".json", ".log"}

# Deterministic chunking
CHUNK_SIZE_CHARS = 1600
CHUNK_OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 80


def log(msg: str):
    print(msg, flush=True)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        return f.read()


def read_pdf_file(path: str) -> str:
    try:
        import PyPDF2  # type: ignore
    except Exception:
        return ""

    try:
        parts = []
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
        return "\n\n".join(parts)
    except Exception:
        return ""


def normalize_text(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")
    return t.strip()


def chunk_text(t: str, size: int, overlap: int) -> List[Tuple[int, int, str]]:
    chunks: List[Tuple[int, int, str]] = []
    n = len(t)
    if n <= 0:
        return chunks

    step = max(1, size - overlap)
    start = 0
    while start < n:
        end = min(n, start + size)
        chunk = t[start:end].strip()
        if len(chunk) >= MIN_CHUNK_CHARS:
            chunks.append((start, end, chunk))
        if end >= n:
            break
        start += step
    return chunks


def embed(text: str) -> List[float]:
    r = ollama.embeddings(model=EMBED_MODEL, prompt=text)
    return r["embedding"]


def get_paths(corpus_dir: str) -> List[str]:
    paths = []
    for ext in SUPPORTED_EXTS:
        paths.extend(glob.glob(os.path.join(corpus_dir, f"**/*{ext}"), recursive=True))
    # optional PDFs
    paths.extend(glob.glob(os.path.join(corpus_dir, "**/*.pdf"), recursive=True))
    return sorted(set(paths), key=lambda p: p.lower())


def main():
    root = r"E:\SOVEREIGN"
    root = os.environ.get("SOVEREIGN_ROOT", root)

    praxis_dir = os.path.join(root, "praxis")
    db_dir = os.path.join(praxis_dir, "db")
    corpus_dir = os.path.join(root, "corpus")

    if not os.path.isdir(corpus_dir):
        log(f"Corpus directory missing: {corpus_dir}")
        sys.exit(1)

    os.makedirs(db_dir, exist_ok=True)

    # ✅ CRITICAL: Use PersistentClient, not Client(Settings(...))
    client = chromadb.PersistentClient(path=db_dir)
    col = client.get_or_create_collection(COLLECTION_NAME)

    paths = get_paths(corpus_dir)
    if not paths:
        log(f"No corpus files found in {corpus_dir}")
        log(f"COLLECTION '{COLLECTION_NAME}' COUNT NOW: {col.count()}")
        log(f"DB DIR: {db_dir}")
        return

    total_files = 0
    total_chunks = 0

    for path in paths:
        rel = os.path.relpath(path, corpus_dir)
        ext = os.path.splitext(path)[1].lower()

        raw = ""
        if ext in SUPPORTED_EXTS:
            raw = read_text_file(path)
        elif ext == ".pdf":
            raw = read_pdf_file(path)
            if not raw.strip():
                log(f"Skipped (no PDF text or PyPDF2 missing): {rel}")
                continue
        else:
            continue

        text = normalize_text(raw)
        if not text:
            log(f"Skipped empty: {rel}")
            continue

        chunks = chunk_text(text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        if not chunks:
            log(f"Skipped (no valid chunks): {rel}")
            continue

        ids: List[str] = []
        docs: List[str] = []
        metas: List[Dict] = []
        embs: List[List[float]] = []

        for idx, (c0, c1, ctext) in enumerate(chunks):
            cid = sha1(f"{rel}|{idx}|{sha1(ctext)}")

            ids.append(cid)
            docs.append(ctext)
            metas.append({
                "source_file": rel,
                "chunk_index": idx,
                "char_start": c0,
                "char_end": c1,
                "ext": ext,
                "type": "corpus"
            })
            embs.append(embed(ctext))

        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        total_files += 1
        total_chunks += len(chunks)
        log(f"Ingested {rel} ({len(chunks)} chunks)")

    # Some versions persist automatically; call persist if available.
    if hasattr(client, "persist"):
        try:
            client.persist()
        except Exception:
            pass

    # ✅ HARD VERIFY: reopen DB in a fresh client and recount
    verify_client = chromadb.PersistentClient(path=db_dir)
    verify_col = verify_client.get_or_create_collection(COLLECTION_NAME)
    verify_count = verify_col.count()

    log(f"Done. Files={total_files} Chunks={total_chunks}")
    log(f"COLLECTION '{COLLECTION_NAME}' COUNT NOW: {verify_count}")
    log(f"DB DIR: {db_dir}")

    if verify_count <= 0:
        log("ERROR: Verify count is 0. This indicates ingestion did not persist to disk.")
        log("Check that db_dir is correct and not blocked by permissions/AV.")


if __name__ == "__main__":
    main()
