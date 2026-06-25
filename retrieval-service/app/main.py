"""
Retrieval Service — RAG (Retrieval-Augmented Generation)
Busca semântica em documentos usando ChromaDB.
Também consome fila RabbitMQ para ingestão assíncrona de documentos.
"""

import os
import asyncio
import uuid
import json
import os
import asyncio
import uuid
import json
import csv
import io
from html.parser import HTMLParser
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
import httpx
import chromadb
from pydantic import BaseModel
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
COLLECTION_NAME = "documents"

chroma_client: chromadb.AsyncHttpClient = None
collection = None
rabbit_connection = None


@app.on_event("startup")
async def startup():
    global chroma_client, collection
    chroma_client = await chromadb.AsyncHttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    collection = await chroma_client.get_or_create_collection(COLLECTION_NAME)
    # Inicia consumidor RabbitMQ em background
    asyncio.create_task(_start_consumer())


@app.on_event("shutdown")
async def shutdown():
    if rabbit_connection:
        await rabbit_connection.close()


# ── Modelos ────────────────────────────────────────────────────────

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


def _extract_text_from_html(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


async def _fetch_url_content(url: str) -> tuple[str, dict]:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; RAGFetcher/1.0; +https://example.com)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                text = _extract_text_from_html(response.text)
            else:
                text = response.text
            metadata = {"source": url, "content_type": content_type}
            return text, metadata
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao acessar URL '{url}': {exc}")


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


# ── Rotas ─────────────────────────────────────────────────────────

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


@app.post("/ingest", status_code=202)
async def ingest_documents(req: IngestRequest):
    """
    Enfileira documentos para ingestão assíncrona via RabbitMQ.
    Aceita documentos com 'content' ou 'url'.
    """
    prepared_docs = await _prepare_documents(req)
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
    """Ingestão síncrona (para testes sem RabbitMQ). Aceita 'content' ou 'url'."""
    prepared_docs = await _prepare_documents(req)
    ids = [str(uuid.uuid4()) for _ in prepared_docs]
    await collection.add(
        ids=ids,
        documents=[d["content"] for d in prepared_docs],
        metadatas=[d["metadata"] for d in prepared_docs],
    )
    return {"status": "indexed", "ids": ids}


@app.post("/ingest/file", status_code=201)
async def ingest_file(file: UploadFile = File(...)):
    """Recebe upload de arquivo (CSV) e indexa cada linha como documento.
    Cada linha vira um documento com conteúdo 'col: valor' por coluna.
    """
    try:
        raw = await file.read()
        try:
            text = raw.decode('utf-8')
        except Exception:
            text = raw.decode('latin-1')

        reader = csv.DictReader(io.StringIO(text))
        docs = []
        for row in reader:
            parts = [f"{k}: {v}" for k, v in row.items()]
            doc_text = "\n".join(parts)
            docs.append({"content": doc_text, "metadata": {"source": file.filename}})

        if not docs:
            raise HTTPException(status_code=400, detail="CSV vazio ou sem cabeçalho")

        ids = [str(uuid.uuid4()) for _ in docs]
        await collection.add(
            ids=ids,
            documents=[d["content"] for d in docs],
            metadatas=[d["metadata"] for d in docs],
        )
        return {"status": "indexed", "count": len(docs), "ids": ids}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest/file", status_code=201)
async def ingest_file(file: UploadFile = File(...)):
    """Ingestão de arquivo único. Suporta CSV e texto simples."""
    filename = file.filename or "uploaded"
    content_type = file.content_type or "text/plain"
    raw_bytes = await file.read()
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
    )
    return {"status": "indexed", "id": doc_id, "filename": filename}


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
