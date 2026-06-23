"""
Retrieval Service — RAG (Retrieval-Augmented Generation)
Busca semântica em documentos usando ChromaDB.
Também consome fila RabbitMQ para ingestão assíncrona de documentos.
"""

import os
import asyncio
import uuid
import json
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import chromadb
import aio_pika

app = FastAPI(title="Retrieval Service", version="1.0.0")

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
COLLECTION_NAME = "documents"

chroma_client: chromadb.AsyncHttpClient = None
collection = None
rabbit_connection = None


@app.on_event("startup")
async def startup():
    global chroma_client, collection
    chroma_client = chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    collection = await chroma_client.get_or_create_collection(COLLECTION_NAME)
    # Inicia consumidor RabbitMQ em background
    asyncio.create_task(_start_consumer())


@app.on_event("shutdown")
async def shutdown():
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
    )
    return {"status": "indexed", "ids": ids}


@app.post("/query")
async def query(req: QueryRequest):
    """Busca semântica: retorna os documentos mais relevantes para a query."""
    results = await collection.query(
        query_texts=[req.query],
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
    return {"chroma": chroma_ok}


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
                    )
    except Exception as e:
        print(f"[retrieval-service] RabbitMQ consumer error: {e}")
