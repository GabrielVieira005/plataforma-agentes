"""
Retrieval Service — RAG (Retrieval-Augmented Generation)
Busca semântica em documentos usando ChromaDB.
Também consome fila RabbitMQ para ingestão assíncrona de documentos.
"""

import os
import asyncio
import uuid
import json
import inspect
import hashlib
import math
import re
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import chromadb
import aio_pika

app = FastAPI(title="Retrieval Service", version="1.0.0")
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
NAME_SERVER_URL = os.getenv("NAME_SERVER_URL", "http://localhost:8000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "retrieval-service")
SERVICE_URL = os.getenv("SERVICE_URL", "http://localhost:8004")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))
CHROMA_CONNECT_RETRIES = int(os.getenv("CHROMA_CONNECT_RETRIES", "30"))
CHROMA_CONNECT_DELAY_SECONDS = float(os.getenv("CHROMA_CONNECT_DELAY_SECONDS", "1"))
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))
COLLECTION_NAME = "documents"

chroma_client: chromadb.AsyncHttpClient = None
collection = None
rabbit_connection = None
consumer_task: asyncio.Task | None = None
registration_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global chroma_client, collection, consumer_task, registration_task
    chroma_client, collection = await _connect_chroma()
    consumer_task = asyncio.create_task(_start_consumer())
    registration_task = asyncio.create_task(_registration_loop())


@app.on_event("shutdown")
async def shutdown():
    await _cancel_task(consumer_task)
    await _cancel_task(registration_task)
    if rabbit_connection:
        await rabbit_connection.close()


# ── Modelos ────────────────────────────────────────────────────────

class Document(BaseModel):
    content: str
    metadata: dict = {}


class IngestRequest(BaseModel):
    documents: list[Document]


class QueryRequest(BaseModel):
    query: str
    n_results: int = 5


# ── Rotas ─────────────────────────────────────────────────────────

@app.post("/ingest", status_code=202)
async def ingest_documents(req: IngestRequest):
    """
    Enfileira documentos para ingestão assíncrona via RabbitMQ.
    Retorna imediatamente (não bloqueante).
    """
    async with await aio_pika.connect_robust(RABBITMQ_URL) as conn:
        channel = await conn.channel()
        queue = await channel.declare_queue("document_ingest", durable=True)
        for doc in req.documents:
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"content": doc.content, "metadata": doc.metadata}).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue.name,
            )
    return {"status": "queued", "count": len(req.documents)}


@app.post("/ingest/sync", status_code=201)
async def ingest_sync(req: IngestRequest):
    """Ingestão síncrona (para testes sem RabbitMQ)."""
    ids = [str(uuid.uuid4()) for _ in req.documents]
    await collection.add(
        ids=ids,
        documents=[d.content for d in req.documents],
        metadatas=[d.metadata for d in req.documents],
        embeddings=_embed_texts([d.content for d in req.documents]),
    )
    return {"status": "indexed", "ids": ids}


@app.post("/query")
async def query(req: QueryRequest):
    """Busca semântica: retorna os documentos mais relevantes para a query."""
    results = await collection.query(
        query_embeddings=_embed_texts([req.query]),
        n_results=req.n_results,
    )
    documents = results["documents"][0] if results["documents"] else []
    metadatas = results["metadatas"][0] if results["metadatas"] else []
    return {
        "results": [
            {"content": doc, "metadata": meta}
            for doc, meta in zip(documents, metadatas)
        ]
    }


@app.get("/health")
async def health():
    try:
        await chroma_client.heartbeat()
        chroma_ok = True
    except Exception:
        chroma_ok = False
    return {"status": "ok" if chroma_ok else "degraded", "chroma": chroma_ok}


# ── Consumidor RabbitMQ ────────────────────────────────────────────

async def _start_consumer():
    """Consome a fila de ingestão e indexa no ChromaDB."""
    global rabbit_connection
    try:
        rabbit_connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await rabbit_connection.channel()
        queue = await channel.declare_queue("document_ingest", durable=True)

        async with queue.iterator() as q:
            async for message in q:
                async with message.process():
                    data = json.loads(message.body)
                    doc_id = str(uuid.uuid4())
                    await collection.add(
                        ids=[doc_id],
                        documents=[data["content"]],
                        metadatas=[data.get("metadata", {})],
                        embeddings=_embed_texts([data["content"]]),
                    )
    except Exception as e:
        print(f"[retrieval-service] RabbitMQ consumer error: {e}")


async def _connect_chroma():
    last_error = None
    for _ in range(CHROMA_CONNECT_RETRIES):
        try:
            client = chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            if inspect.isawaitable(client):
                client = await client
            docs = await client.get_or_create_collection(COLLECTION_NAME)
            return client, docs
        except Exception as e:
            last_error = e
            await asyncio.sleep(CHROMA_CONNECT_DELAY_SECONDS)
    raise RuntimeError(f"Could not connect to ChromaDB: {last_error}")


async def _registration_loop():
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{NAME_SERVER_URL}/register",
                    json={"name": SERVICE_NAME, "url": SERVICE_URL},
                )
        except Exception:
            pass
        await asyncio.sleep(REGISTRATION_INTERVAL_SECONDS)


async def _cancel_task(task: asyncio.Task | None):
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _embed_texts(texts: list[str]) -> list[list[float]]:
    return [_embed_text(text) for text in texts]


def _embed_text(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    tokens = re.findall(r"\w+", text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]
