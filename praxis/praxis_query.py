import os
import sys
import json
import argparse
import tempfile
import urllib.error
import urllib.request

COLLECTION_NAME = "praxis_memory"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_TIMEOUT_SEC = 60


def _write_text_atomic(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or None, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def embed(text: str, model: str = EMBED_MODEL, timeout_sec: int = EMBED_TIMEOUT_SEC) -> list[float]:
    payload = json.dumps({"model": model, "prompt": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL + "/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        body = json.loads(r.read())
    emb = body.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise ValueError("embedding missing in Ollama response")
    return [float(x) for x in emb]


def _load_query_payload(query_file: str, cli_top_n: int) -> tuple[str, int]:
    with open(query_file, "r", encoding="utf-8-sig", errors="ignore") as f:
        raw = f.read().strip()

    if not raw:
        raise ValueError("empty query")

    top_n = max(1, int(cli_top_n))

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw, top_n

    if isinstance(obj, dict):
        query = str(obj.get("query", "")).strip()
        if not query:
            raise ValueError("query payload missing 'query'")

        n_results = obj.get("n_results")
        if n_results is not None:
            try:
                top_n = max(1, int(n_results))
            except (TypeError, ValueError):
                pass
        return query, top_n

    if isinstance(obj, str):
        query = obj.strip()
        if not query:
            raise ValueError("empty query")
        return query, top_n

    return raw, top_n


def _format_results(
    docs: list,
    metas: list,
    dists: list,
) -> list[dict]:
    out: list[dict] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
        dist = dists[i] if i < len(dists) else None
        source = str(meta.get("source", meta.get("source_file", "unknown"))).strip() or "unknown"
        topic = str(meta.get("topic", "")).strip()
        try:
            distance = float(dist) if dist is not None else None
        except (TypeError, ValueError):
            distance = None

        out.append(
            {
                "type": str(meta.get("type", "memory_chunk")).strip() or "memory_chunk",
                "source": source,
                "topic": topic,
                "chunk_index": meta.get("chunk_index", "?"),
                "distance": distance,
                "confidence": "?",
                "content": str(doc or "").strip(),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    praxis_dir = os.path.join(args.root, "praxis")
    db_dir = os.path.join(praxis_dir, "db")
    query_file = os.path.join(praxis_dir, "query.txt")
    result_file = os.path.join(praxis_dir, "result.txt")

    if not os.path.exists(query_file):
        print("query.txt not found", file=sys.stderr)
        return 1

    try:
        query, top_n = _load_query_payload(query_file, args.top_n)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"failed to read query.txt: {exc}", file=sys.stderr)
        return 2

    if not os.path.isdir(db_dir):
        print("db_dir missing: " + db_dir, file=sys.stderr)
        return 3

    try:
        import chromadb
    except ImportError as exc:
        print(f"chromadb import failed: {exc}", file=sys.stderr)
        return 4

    client = chromadb.PersistentClient(path=db_dir)
    col = client.get_or_create_collection(COLLECTION_NAME)
    count = col.count()

    entries: list[dict] = []
    if count > 0:
        try:
            qvec = embed(query)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"embedding failed: {exc}", file=sys.stderr)
            return 5

        n = min(max(1, int(top_n)), count)
        results = col.query(
            query_embeddings=[qvec],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        entries = _format_results(docs, metas, dists)

    payload = json.dumps(entries, ensure_ascii=False, indent=2)
    try:
        _write_text_atomic(result_file, payload)
    except OSError as exc:
        print(f"failed to write result.txt: {exc}", file=sys.stderr)
        return 6

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
