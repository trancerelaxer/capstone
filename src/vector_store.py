import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

BASE_DIR = Path(__file__).resolve().parent.parent
QDRANT_PATH = BASE_DIR / "data" / "cache" / "qdrant_db"
QDRANT_PATH.mkdir(parents=True, exist_ok=True)
try:
    client = QdrantClient(path=str(QDRANT_PATH))
except RuntimeError as exc:
    if "already accessed by another instance of Qdrant client" in str(exc):
        raise RuntimeError(
            f"Qdrant cache at '{QDRANT_PATH}' is locked by another process. "
            "Stop the other app instance (or close processes using this path) and restart."
        ) from exc
    else:
        raise

COLLECTION = "genai_knowledge"


def _get_vector_size(config_vectors):
    if isinstance(config_vectors, dict):
        first_vector = next(iter(config_vectors.values()))
        return int(first_vector.size)
    return int(config_vectors.size)


def _recreate_collection(vector_size):
    client.recreate_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )


def init_collection(vector_size=384, reset=False):
    collection_names = {c.name for c in client.get_collections().collections}
    if reset or COLLECTION not in collection_names:
        _recreate_collection(vector_size)
        return

    info = client.get_collection(COLLECTION)
    existing_size = _get_vector_size(info.config.params.vectors)
    if existing_size != vector_size:
        _recreate_collection(vector_size)


def get_collection_status() -> dict[str, Any]:
    collection_names = {c.name for c in client.get_collections().collections}
    if COLLECTION not in collection_names:
        return {"exists": False, "points": 0, "vector_size": None}

    info = client.get_collection(COLLECTION)
    vector_size = _get_vector_size(info.config.params.vectors)
    count_result = client.count(collection_name=COLLECTION, exact=False)
    points_count = int(getattr(count_result, "count", 0))
    return {"exists": True, "points": points_count, "vector_size": vector_size}


def _make_point_id(text, metadata):
    source = metadata.get("source", "")
    chunk_index = metadata.get("chunk_index", "")
    raw = f"{source}|{chunk_index}|{text}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return int(digest, 16)


def insert_chunks(chunks, embeddings, metadatas=None):
    if metadatas is None:
        metadatas = [{} for _ in chunks]
    if len(chunks) != len(embeddings) or len(chunks) != len(metadatas):
        raise ValueError("chunks, embeddings, and metadatas must have the same length.")

    points = [
        PointStruct(
            id=_make_point_id(chunks[i], metadatas[i]),
            vector=(
                embeddings[i].tolist()
                if isinstance(embeddings[i], np.ndarray)
                else embeddings[i]
            ),
            payload={"text": chunks[i], **metadatas[i]},
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=COLLECTION, points=points)


def search(query_vector, top_k=5):
    if isinstance(query_vector, np.ndarray):
        query_vector = query_vector.tolist()
    results = client.query_points(
        collection_name=COLLECTION, query=query_vector, limit=top_k
    )
    output = []
    for r in results.points:
        if not r.payload or "text" not in r.payload:
            continue
        source = r.payload.get("source")
        if source:
            output.append(f"[Source: {source}] {r.payload['text']}")
        else:
            output.append(r.payload["text"])
    return output


def close_client():
    client.close()
