"""
Tool Registry
Registra e expõe ferramentas que os agentes podem invocar.
Ferramentas built-in: calculadora, consulta de data/hora.
Ferramentas externas podem ser registradas dinamicamente.
"""

import os
import asyncio
import math
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from html.parser import HTMLParser
import httpx
import csv
import io
import base64
from typing import Any
from .telemetry import setup_telemetry

RETRIEVAL_SERVICE_URL = os.getenv("RETRIEVAL_SERVICE_URL", "http://localhost:8004")
NAME_SERVER_URL = os.getenv("NAME_SERVER_URL", "http://localhost:8000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "tool-registry")
SERVICE_URL = os.getenv("SERVICE_URL", "http://localhost:8005")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))

registration_task: asyncio.Task | None = None

app = FastAPI(title="Tool Registry", version="1.0.0")
tracer = setup_telemetry(app, SERVICE_NAME)
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    global registration_task
    registration_task = asyncio.create_task(_registration_loop())


@app.on_event("shutdown")
async def shutdown():
    await _stop_registration()

# ── Registro de ferramentas ────────────────────────────────────────

# Ferramentas built-in (executadas internamente)
_builtin_tools: dict[str, dict] = {
    "calculator": {
        "name": "calculator",
        "description": "Avalia expressões matemáticas. Ex: '2 + 2 * 10'",
        "parameters": {"expression": "string — expressão matemática"},
        "type": "builtin",
    },
    "get_datetime": {
        "name": "get_datetime",
        "description": "Retorna a data e hora atual.",
        "parameters": {},
        "type": "builtin",
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Busca o conteúdo textual de uma URL e retorna um resumo extraído.",
        "parameters": {
            "url": "string — URL para acessar",
            "max_chars": "integer — limite de caracteres no texto retornado (opcional)",
        },
        "type": "builtin",
    },
    "query_rag": {
        "name": "query_rag",
        "description": "Pesquisa semanticamente documentos indexados no RAG e retorna os resultados mais relevantes.",
        "parameters": {
            "query": "string — consulta para buscar nos documentos indexados",
            "n_results": "integer — número de resultados a retornar (opcional, padrão 3)",
        },
        "type": "builtin",
    },
    "ingest_csv": {
        "name": "ingest_csv",
        "description": "Ingesta um CSV remoto (ou texto CSV) e indexa as linhas no RAG.",
        "parameters": {
                "csv_base64": "string — conteúdo do arquivo CSV codificado em base64 (recomendado)",
                "csv_text": "string — conteúdo CSV em texto (alternativa)",
                "filename": "string — nome do arquivo (opcional)",
                "max_rows": "integer — limite de linhas a indexar (opcional)",
        },
        "type": "builtin",
    },
}

# Ferramentas externas registradas dinamicamente
_external_tools: dict[str, dict] = {}


# ── Modelos ────────────────────────────────────────────────────────

class ExternalTool(BaseModel):
    name: str
    description: str
    parameters: dict
    endpoint: str        # URL onde a ferramenta está hospedada


class InvokeRequest(BaseModel):
    tool_name: str
    parameters: dict = {}


# ── Rotas ─────────────────────────────────────────────────────────

@app.get("/tools")
async def list_tools():
    """Lista todas as ferramentas disponíveis."""
    all_tools = {**_builtin_tools, **_external_tools}
    return {"tools": list(all_tools.values())}


@app.post("/tools/register", status_code=201)
async def register_tool(tool: ExternalTool):
    """Registra uma ferramenta externa."""
    _external_tools[tool.name] = tool.model_dump()
    return {"status": "registered", "name": tool.name}


@app.delete("/tools/{name}")
async def remove_tool(name: str):
    if name not in _external_tools:
        raise HTTPException(status_code=404, detail="Tool not found or is built-in")
    del _external_tools[name]
    return {"status": "removed"}


@app.post("/tools/invoke")
async def invoke_tool(req: InvokeRequest) -> dict:
    """
    Invoca uma ferramenta pelo nome.
    Ferramentas builtin são executadas localmente.
    Ferramentas externas são delegadas ao endpoint registrado.
    """
    with tracer.start_as_current_span("tool.invoke") as span:
        name = req.tool_name
        span.set_attribute("tool.name", name)
        span.set_attribute("tool.parameters.count", len(req.parameters))

        # Built-in
        if name in _builtin_tools:
            return _invoke_builtin(name, req.parameters)

        # Externa
        if name in _external_tools:
            import httpx
            tool = _external_tools[name]
            span.set_attribute("tool.type", "external")
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(tool["endpoint"], json=req.parameters)
                r.raise_for_status()
                return r.json()

        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "tool_count": len(_builtin_tools) + len(_external_tools)}


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


async def _stop_registration():
    if registration_task is None:
        return
    registration_task.cancel()
    try:
        await registration_task
    except asyncio.CancelledError:
        pass


# ── Implementações built-in ────────────────────────────────────────

def _invoke_builtin(name: str, params: dict) -> dict:
    with tracer.start_as_current_span("tool.invoke_builtin") as span:
        span.set_attribute("tool.name", name)
        if name == "calculator":
            expr = params.get("expression", "")
            span.set_attribute("calculator.expression.length", len(expr))
            try:
                # Avaliação segura (apenas operações matemáticas)
                allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
                result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
                return {"result": result}
            except Exception as e:
                span.record_exception(e)
                return {"error": str(e)}

        if name == "get_datetime":
            return {"datetime": datetime.now().isoformat(), "timezone": "local"}

        if name == "fetch_url":
            url = params.get("url")
            max_chars = params.get("max_chars", 2000)
            if not url:
                return {"error": "Parâmetro 'url' é obrigatório."}
            span.set_attribute("url", url)
            try:
                with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                    response = client.get(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; RAGTool/1.0; +https://example.com)",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                    )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    html = response.text
                    title, text = _extract_title_and_text(html)
                    snippet = text[:int(max_chars)].strip()
                    span.set_attribute("http.response.content_type", content_type)
                    span.set_attribute("document.length", len(text))
                    return {
                        "url": url,
                        "title": title,
                        "content": snippet,
                        "content_type": content_type,
                    }
            except httpx.HTTPError as e:
                span.record_exception(e)
                return {"error": f"Falha ao acessar URL: {e}"}

        if name == "query_rag":
            query = params.get("query") or params.get("q")
            n_results = params.get("n_results", 3)
            if not query:
                return {"error": "Parâmetro 'query' é obrigatório."}
            span.set_attribute("rag.query.length", len(query))
            span.set_attribute("rag.n_results", int(n_results))
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(
                        f"{RETRIEVAL_SERVICE_URL}/query",
                        json={"query": query, "n_results": int(n_results)},
                    )
                    response.raise_for_status()
                    data = response.json()
                    span.set_attribute("rag.results.count", len(data.get("results", [])))
                    return data
            except httpx.HTTPError as e:
                span.record_exception(e)
                return {"error": f"Falha ao consultar RAG: {e}"}

        if name == "ingest_csv":
            url = params.get("url")
            csv_text = params.get("csv_text")
            max_rows = int(params.get("max_rows", 1000))
            if not url and not csv_text:
                return {"error": "Forneça 'url' ou 'csv_text' para ingest_csv."}
            try:
                if url:
                    span.set_attribute("url", url)
                    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                        r = client.get(url)
                        r.raise_for_status()
                        text = r.text
                        source = url
                else:
                    text = str(csv_text)
                    source = "inline-csv"

                # Parse CSV
                reader = csv.DictReader(io.StringIO(text))
                docs = []
                for i, row in enumerate(reader):
                    if i >= max_rows:
                        break
                    parts = [f"{k}: {v}" for k, v in row.items()]
                    doc_text = "\n".join(parts)
                    docs.append({"content": doc_text, "metadata": {"source": source}})

                if not docs:
                    return {"error": "CSV vazio ou sem cabeçalho"}

                span.set_attribute("documents.count", len(docs))
                # Send to retrieval-service ingest sync
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(f"{RETRIEVAL_SERVICE_URL}/ingest/sync", json={"documents": docs})
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPError as e:
                span.record_exception(e)
                return {"error": f"Falha ao ingest CSV: {e}"}

    return {"error": "Unknown builtin"}


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


def _extract_title_and_text(html: str) -> tuple[str, str]:
    title = ""
    title_start = html.find("<title>")
    title_end = html.find("</title>")
    if title_start != -1 and title_end != -1 and title_start < title_end:
        title = html[title_start + 7:title_end].strip()
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return title, parser.get_text()
