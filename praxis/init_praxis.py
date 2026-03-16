import os
import chromadb
from chromadb.config import Settings

def main():
    root = r"E:\SOVEREIGN"
    praxis_db = os.path.join(root, "praxis", "db")
    os.makedirs(praxis_db, exist_ok=True)

    client = chromadb.Client(Settings(
        persist_directory=praxis_db,
        anonymized_telemetry=False
    ))

    client.get_or_create_collection(
        name="praxis_memory",
        metadata={"hnsw:space": "cosine"}
    )

    print("PRAXIS initialized:", praxis_db)

if __name__ == "__main__":
    main()
