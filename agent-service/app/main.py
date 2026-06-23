"""
Agent Service — Núcleo da Plataforma
Implementa o ciclo agêntico: raciocínio → ação → observação.

Fluxo:
1. Recebe mensagem do usuário
2. Recupera histórico (memory-service)
3. Raciocina com o LLM (llm-gateway)
4. Se o LLM pede uma ferramenta → invoca (tool-registry) → observa resultado
5. Repete até ter resposta final
6. Persiste no histórico e retorna ao cliente
"""

import os
import json
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

# ── Telemetria ────────────────────────────────────────────────────

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

provider = TracerProvider()
try:
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
    )
except Exception:
    pass  # Jaeger não está rodando, ignorar
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent-service")

# ── Config ────────────────────────────────────────────────────────

LLM_GATEWAY_URL     = os.getenv("LLM_GATEWAY_URL",     "http://localhost:8002")
MEMORY_SERVICE_URL  = os.getenv("MEMORY_SERVICE_URL",  "http://localhost:8003")
RETRIEVAL_SERVICE_URL = os.getenv("RETRIEVAL_SERVICE_URL", "http://localhost:8004")
TOOL_REGISTRY_URL   = os.getenv("TOOL_REGISTRY_URL",   "http://localhost:8005")
MAX_ITERATIONS      = int(os.getenv("MAX_ITERATIONS", "5"))

app = FastAPI(title="Agent Service", version="1.0.0")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Modelos ────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str
    model: str = "llama3.2"


class ChatResponse(BaseModel):
    session_id: str
    response: str
    iterations: int


# ── System prompt do agente ────────────────────────────────────────

SYSTEM_PROMPT = """Você é um assistente inteligente com acesso a ferramentas.
Quando precisar usar uma ferramenta, responda EXATAMENTE neste formato JSON:
{{"action": "tool_name", "parameters": {{"param": "value"}}}}

Ferramentas disponíveis: {tools}

Quando tiver a resposta final, responda normalmente em linguagem natural.
NAO use JSON na resposta final."""


# ── Rotas ─────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    with tracer.start_as_current_span("agent.chat") as span:
        span.set_attribute("session_id", req.session_id)
        span.set_attribute("model", req.model)

        async with httpx.AsyncClient(timeout=120.0) as client:
            # 1. Busca ferramentas disponíveis
            tools = await _get_tools(client)

            # 2. Recupera histórico da sessão
            history = await _get_history(client, req.session_id)

            # 3. Salva mensagem do usuário
            await _save_message(client, req.session_id, "user", req.message)

            # 4. Ciclo agêntico
            response, iterations = await _agentic_loop(
                client, req, history, tools, span
            )

            # 5. Salva resposta do assistente
            await _save_message(client, req.session_id, "assistant", response)

        return ChatResponse(
            session_id=req.session_id,
            response=response,
            iterations=iterations,
        )


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=5.0) as client:
        services = {}
        for name, url in [
            ("llm-gateway", LLM_GATEWAY_URL),
            ("memory-service", MEMORY_SERVICE_URL),
            ("tool-registry", TOOL_REGISTRY_URL),
        ]:
            try:
                r = await client.get(f"{url}/health")
                services[name] = r.json().get("status", "ok")
            except Exception:
                services[name] = "unreachable"
    return {"status": "ok", "dependencies": services}


# ── Ciclo Agêntico ────────────────────────────────────────────────

async def _agentic_loop(
    client: httpx.AsyncClient,
    req: ChatRequest,
    history: list[dict],
    tools: list[dict],
    span,
) -> tuple[str, int]:
    """
    Ciclo raciocínio → ação → observação.
    Retorna (resposta_final, número_de_iterações).
    """
    tool_names = [t["name"] for t in tools]
    system = SYSTEM_PROMPT.format(tools=", ".join(tool_names))

    messages = [
        {"role": "system", "content": system},
        *history[-10:],              # últimas 10 mensagens de contexto
        {"role": "user", "content": req.message},
    ]

    for iteration in range(1, MAX_ITERATIONS + 1):
        with tracer.start_as_current_span(f"agent.iteration.{iteration}"):
            # Raciocínio: chama o LLM
            llm_response = await _call_llm(client, messages, req.model)
            content = llm_response.get("content", "")

            # Verifica se é uma chamada de ferramenta
            tool_call = _parse_tool_call(content)

            if tool_call is None:
                # Resposta final
                return content, iteration

            # Ação: invoca a ferramenta
            tool_name = tool_call["action"]
            tool_params = tool_call.get("parameters", {})
            observation = await _invoke_tool(client, tool_name, tool_params)

            # Adiciona raciocínio e observação ao histórico de contexto
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": f"[Resultado da ferramenta '{tool_name}']: {json.dumps(observation)}",
            })

    return "Não consegui chegar a uma resposta após o número máximo de iterações.", MAX_ITERATIONS


def _parse_tool_call(content: str) -> dict | None:
    """Tenta extrair uma chamada de ferramenta do conteúdo do LLM."""
    content = content.strip()
    if not content.startswith("{"):
        return None
    try:
        data = json.loads(content)
        if "action" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


# ── Helpers de integração ─────────────────────────────────────────

async def _call_llm(client: httpx.AsyncClient, messages: list, model: str) -> dict:
    """Chama o LLM Gateway. Levanta HTTPException em caso de falha (circuit breaker futuro)."""
    try:
        r = await client.post(
            f"{LLM_GATEWAY_URL}/chat",
            json={"messages": messages, "model": model},
        )
        r.raise_for_status()
        return r.json()["message"]
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="LLM Gateway unavailable")


async def _get_tools(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(f"{TOOL_REGISTRY_URL}/tools")
        r.raise_for_status()
        return r.json().get("tools", [])
    except Exception:
        return []


async def _get_history(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    try:
        r = await client.get(f"{MEMORY_SERVICE_URL}/sessions/{session_id}/messages")
        r.raise_for_status()
        return r.json().get("messages", [])
    except Exception:
        return []


async def _save_message(client: httpx.AsyncClient, session_id: str, role: str, content: str):
    try:
        await client.post(
            f"{MEMORY_SERVICE_URL}/sessions/{session_id}/messages",
            json={"session_id": session_id, "message": {"role": role, "content": content}},
        )
    except Exception:
        pass  # Não bloqueia a resposta ao cliente


async def _invoke_tool(client: httpx.AsyncClient, tool_name: str, params: dict) -> dict:
    try:
        r = await client.post(
            f"{TOOL_REGISTRY_URL}/tools/invoke",
            json={"tool_name": tool_name, "parameters": params},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}
