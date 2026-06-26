"""
Retrieval Service — RAG (Retrieval-Augmented Generation)
Busca semântica em documentos usando ChromaDB.
Também consome fila RabbitMQ para ingestão assíncrona de documentos.
"""

import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
import uuid
from html.parser import HTMLParser

import aio_pika
import chromadb
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel
from .telemetry import setup_telemetry

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

chroma_client = None
collection = None
rabbit_connection = None
consumer_task: asyncio.Task | None = None
registration_task: asyncio.Task | None = None
tracer = setup_telemetry(app, SERVICE_NAME)


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


class Document(BaseModel):
    content: str | None = None
    url: str | None = None
    metadata: dict = {}


class IngestRequest(BaseModel):
    documents: list[Document]


class QueryRequest(BaseModel):
    query: str
    n_results: int = 5


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._texts: list[str] = []
        self._ignore = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._ignore = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self._ignore = False

    def handle_data(self, data):
        if not self._ignore:
            self._texts.append(data)

    def get_text(self) -> str:
        return " ".join(text.strip() for text in self._texts if text.strip())


@app.post("/ingest", status_code=202)
async def ingest_documents(req: IngestRequest):
    """Enfileira documentos para ingestão assíncrona via RabbitMQ."""
    with tracer.start_as_current_span("retrieval.ingest_async") as span:
        prepared_docs = await _prepare_documents(req)
        span.set_attribute("documents.count", len(prepared_docs))
        async with await aio_pika.connect_robust(RABBITMQ_URL) as conn:
            channel = await conn.channel()
            queue = await channel.declare_queue("document_ingest", durable=True)
            for doc in prepared_docs:
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(doc).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=queue.name,
                )
        return {"status": "queued", "count": len(prepared_docs)}


@app.post("/ingest/sync", status_code=201)
async def ingest_sync(req: IngestRequest):
    """Ingestão síncrona para testes e frontend. Aceita 'content' ou 'url'."""
    with tracer.start_as_current_span("retrieval.ingest_sync") as span:
        prepared_docs = await _prepare_documents(req)
        ids = [str(uuid.uuid4()) for _ in prepared_docs]
        documents = [d["content"] for d in prepared_docs]
        span.set_attribute("documents.count", len(documents))
        await collection.add(
            ids=ids,
            documents=documents,
            metadatas=[d["metadata"] for d in prepared_docs],
            embeddings=_embed_texts(documents),
        )
        return {"status": "indexed", "ids": ids}


@app.post("/ingest/file", status_code=201)
async def ingest_file(file: UploadFile = File(...)):
    """Ingestão de arquivo único. Suporta CSV e texto simples."""
    with tracer.start_as_current_span("retrieval.ingest_file") as span:
        filename = file.filename or "uploaded"
        content_type = file.content_type or "text/plain"
        span.set_attribute("file.name", filename)
        span.set_attribute("file.content_type", content_type)
        raw_bytes = await file.read()
        span.set_attribute("file.size", len(raw_bytes))
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1", errors="replace")

        if filename.lower().endswith(".csv") or "csv" in content_type:
            text = _csv_to_text(text)
            metadata = {"source": filename, "content_type": "text/csv", "type": "csv"}
        else:
            metadata = {"source": filename, "content_type": content_type}

        doc_id = str(uuid.uuid4())
        await collection.add(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
            embeddings=_embed_texts([text]),
        )
        return {"status": "indexed", "id": doc_id, "filename": filename}


@app.post("/query")
async def query(req: QueryRequest):
    """Busca semântica: retorna os documentos mais relevantes para a query."""
    with tracer.start_as_current_span("retrieval.query") as span:
        span.set_attribute("rag.query.length", len(req.query))
        span.set_attribute("rag.n_results", req.n_results)
        results = await collection.query(
            query_embeddings=_embed_texts([req.query]),
            n_results=req.n_results,
        )
        documents = results["documents"][0] if results["documents"] else []
        metadatas = results["metadatas"][0] if results["metadatas"] else []
        span.set_attribute("rag.results.count", len(documents))
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


async def _prepare_documents(req: IngestRequest) -> list[dict]:
    prepared = []
    for doc in req.documents:
        if doc.url:
            content, url_metadata = await _fetch_url_content(doc.url)
            metadata = {**doc.metadata, **url_metadata}
            prepared.append({"content": content, "metadata": metadata})
        elif doc.content:
            prepared.append({"content": doc.content, "metadata": doc.metadata})
        else:
            raise HTTPException(
                status_code=400,
                detail="Cada documento precisa ter 'content' ou 'url'.",
            )
    return prepared


async def _fetch_url_content(url: str) -> tuple[str, dict]:
    with tracer.start_as_current_span("retrieval.fetch_url") as span:
        span.set_attribute("url", url)
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; RAGFetcher/1.0)",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                span.set_attribute("http.response.content_type", content_type)
                if "text/html" in content_type:
                    text = _extract_text_from_html(response.text)
                else:
                    text = response.text
                span.set_attribute("document.length", len(text))
                metadata = {"source": url, "content_type": content_type}
                return text, metadata
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            raise HTTPException(status_code=400, detail=f"Falha ao acessar URL '{url}': {exc}")


def _extract_text_from_html(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def _csv_to_text(csv_text: str) -> str:
    rows = []
    try:
        reader = csv.reader(csv_text.splitlines())
        for row in reader:
            if row:
                rows.append(" | ".join(cell.strip() for cell in row if cell.strip()))
    except csv.Error:
        rows = csv_text.splitlines()
    return "\n".join(rows)


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
                    with tracer.start_as_current_span("retrieval.consume_ingest_message") as span:
                        data = json.loads(message.body)
                        content = data["content"]
                        doc_id = str(uuid.uuid4())
                        span.set_attribute("document.length", len(content))
                        await collection.add(
                            ids=[doc_id],
                            documents=[content],
                            metadatas=[data.get("metadata", {})],
                            embeddings=_embed_texts([content]),
                        )
    except Exception as e:
        print(f"[retrieval-service] RabbitMQ consumer error: {e}")


async def _connect_chroma():
    last_error = None
    with tracer.start_as_current_span("retrieval.connect_chroma") as span:
        span.set_attribute("chroma.host", CHROMA_HOST)
        span.set_attribute("chroma.port", CHROMA_PORT)
        span.set_attribute("chroma.collection", COLLECTION_NAME)
        for attempt in range(1, CHROMA_CONNECT_RETRIES + 1):
            try:
                client = await chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
                docs = await client.get_or_create_collection(COLLECTION_NAME)
                span.set_attribute("chroma.connect.attempt", attempt)
                return client, docs
            except Exception as e:
                last_error = e
                span.record_exception(e)
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
