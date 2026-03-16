import os
import chromadb
from chromadb.config import Settings

root = r"E:\SOVEREIGN"
db_dir = os.path.join(root, "praxis", "db")

client = chromadb.Client(Settings(
    persist_directory=db_dir,
    anonymized_telemetry=False
))

col = client.get_or_create_collection("praxis_memory")

# Chroma count
print("DB DIR:", db_dir)
print("COLLECTION:", col.name)
print("COUNT:", col.count())