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
import re
import unicodedata
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .telemetry import setup_telemetry

# ── Config ────────────────────────────────────────────────────────

LLM_GATEWAY_URL     = os.getenv("LLM_GATEWAY_URL",     "http://localhost:8002")
MEMORY_SERVICE_URL  = os.getenv("MEMORY_SERVICE_URL",  "http://localhost:8003")
RETRIEVAL_SERVICE_URL = os.getenv("RETRIEVAL_SERVICE_URL", "http://localhost:8004")
TOOL_REGISTRY_URL   = os.getenv("TOOL_REGISTRY_URL",   "http://localhost:8005")
NAME_SERVER_URL     = os.getenv("NAME_SERVER_URL",     "http://localhost:8000")
SERVICE_NAME        = os.getenv("SERVICE_NAME",        "agent-service")
SERVICE_URL         = os.getenv("SERVICE_URL",         "http://localhost:8006")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))
MAX_ITERATIONS      = int(os.getenv("MAX_ITERATIONS", "5"))
LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "180"))
LLM_MAX_TOKENS      = int(os.getenv("LLM_MAX_TOKENS", "384"))

registration_task: asyncio.Task | None = None

app = FastAPI(title="Agent Service", version="1.0.0")
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

Regras para responder com documentos/RAG:
- Os trechos do RAG ja foram selecionados como relevantes. Trate-os como evidencias uteis, nao como ruido.
- Quando houver documentos relevantes no contexto, use esses documentos como a principal fonte da resposta.
- Seja agressivo no uso do RAG: se um trecho encontrado declarar um fato direto, responda esse fato diretamente na primeira frase.
- Para perguntas como "o que voce sabe sobre X", "quem é X" ou "me fale sobre X", qualquer frase recuperada que mencione X e declare algo sobre X e uma resposta relevante.
- Exemplo obrigatório: se o documento diz "a Amanda odeia o Nicolas" e o usuário pergunta "o que voce sabe sobre a Amanda?", responda "Sei que a Amanda odeia o Nicolas." Se quiser, acrescente depois: "Nao encontrei mais contexto sobre ela."
- Se o documento tiver informação parcial, responda primeiro com o fato parcial. Só depois diga brevemente o que não aparece no documento.
- NUNCA diga que "não há informações relevantes" ou "não há informações suficientes" quando houver um trecho que declare algo sobre o assunto perguntado.
- Evite respostas que comecem recusando, relativizando ou dizendo que a informação é limitada quando existir um fato útil no RAG.
- Não peça para o usuário tentar novamente se já houver algum dado útil nos documentos.
- Não invente informações fora dos documentos. Se precisar inferir, deixe claro que é uma inferência.
- Se o usuário perguntar "quais", "qual", "liste" ou "me responda com o que voce tem", liste objetivamente os itens encontrados.
- Se a categoria perguntada pelo usuário não bater perfeitamente com o texto do documento, explique com cuidado. Exemplo: "O documento cita OpenTelemetry e Jaeger como ferramentas de observabilidade; ele não chama explicitamente esses itens de frameworks."

Se o usuário fornecer um link, use a ferramenta fetch_url para acessar a página e extrair seu conteúdo.
Se quiser responder com base em informações previamente indexadas, utilize a busca semântica com query_rag.
Se quiser indexar dados tabulares (CSV), use a ferramenta ingest_csv com parâmetro 'url' ou 'csv_text'.

Quando tiver a resposta final, responda normalmente em linguagem natural.
NAO use JSON na resposta final."""


# ── Rotas ─────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    with tracer.start_as_current_span("agent.chat") as span:
        span.set_attribute("session_id", req.session_id)
        span.set_attribute("model", req.model)

        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT_SECONDS) as client:
            # 1. Busca ferramentas disponíveis
            tools = await _get_tools(client)

            # 2. Recupera histórico da sessão
            history = await _get_history(client, req.session_id)

            # 3. Busca documentos relevantes no RAG para a pergunta atual
            retrieval_results = await _query_rag(client, req.message)

            # 4. Ciclo agêntico
            direct_rag_answer = _direct_rag_answer(req.message, retrieval_results)
            if direct_rag_answer:
                response, iterations = direct_rag_answer, 1
            else:
                response, iterations = await _agentic_loop(
                    client, req, history, retrieval_results, tools, span
                )

            # 5. Persiste apenas conversas concluídas com sucesso
            await _save_message(client, req.session_id, "user", req.message)
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
            ("retrieval-service", RETRIEVAL_SERVICE_URL),
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
    retrieval_results: list[dict],
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
    ]

    if retrieval_results:
        messages.append({
            "role": "system",
            "content": _format_retrieval_results(retrieval_results),
        })

    messages.append({"role": "user", "content": req.message})

    for iteration in range(1, MAX_ITERATIONS + 1):
        with tracer.start_as_current_span("agent.iteration") as iteration_span:
            iteration_span.set_attribute("agent.iteration", iteration)
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
            iteration_span.set_attribute("tool.name", tool_name)
            observation = await _invoke_tool(client, tool_name, tool_params)

            # Adiciona raciocínio e observação ao histórico de contexto
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": f"[Resultado da ferramenta '{tool_name}']: {json.dumps(observation)}",
            })

    return "Não consegui chegar a uma resposta após o número máximo de iterações.", MAX_ITERATIONS


def _format_retrieval_results(results: list[dict]) -> str:
    formatted = [
        "Documentos relevantes encontrados no RAG:",
        "Os trechos abaixo devem ser tratados como evidencias relevantes para a pergunta do usuario.",
        "Se um trecho mencionar a entidade perguntada e declarar algo sobre ela, responda esse fato diretamente na primeira frase.",
        "Nao diga que nao ha informacoes relevantes quando existir um trecho com um fato direto. Limitações so podem aparecer depois do fato.",
    ]
    for index, item in enumerate(results, start=1):
        content = item.get("content", "").strip()
        metadata = item.get("metadata", {})
        source = metadata.get("source", metadata.get("content_type", "desconhecido"))
        snippet = content[:1200].replace("\n", " ")
        formatted.append(
            f"[{index}] fonte: {source} | trecho: {snippet}"
        )
    return "\n".join(formatted)


def _direct_rag_answer(query: str, results: list[dict]) -> str | None:
    subject = _extract_direct_rag_subject(query)
    if not subject:
        return None

    facts = []
    normalized_subject = _normalize_text(subject)
    for item in results:
        content = item.get("content", "")
        for fact in _split_fact_candidates(content):
            if normalized_subject in _normalize_text(fact):
                cleaned = fact.strip(" .;:")
                if cleaned and cleaned not in facts:
                    facts.append(cleaned)
            if len(facts) >= 3:
                break
        if len(facts) >= 3:
            break

    if not facts:
        return None

    if len(facts) == 1:
        return f"Sei que {facts[0]}. Nao encontrei mais contexto sobre isso nos documentos."

    formatted_facts = "\n".join(f"- {fact}." for fact in facts)
    return f"Sei estas informacoes nos documentos:\n{formatted_facts}\nNao encontrei mais contexto sobre isso nos documentos."


def _extract_direct_rag_subject(query: str) -> str | None:
    normalized = _normalize_text(query).strip()
    patterns = [
        r"^o que (?:voce|vc) sabe sobre (?:o |a |os |as )?(.+?)\??$",
        r"^quem (?:e|eh) (?:o |a )?(.+?)\??$",
        r"^me fale sobre (?:o |a )?(.+?)\??$",
    ]
    for pattern in patterns:
        match = re.match(pattern, normalized)
        if match:
            subject = match.group(1).strip(" ?!.:,;")
            return subject or None
    return None


def _normalize_text(value: str) -> str:
    without_accents = unicodedata.normalize("NFKD", value)
    ascii_text = without_accents.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.lower().split())


def _split_fact_candidates(content: str) -> list[str]:
    compact = " ".join(content.split())
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|[\n;]+", compact)
        if part.strip()
    ]


def _parse_tool_call(content: str) -> dict | None:
    """Tenta extrair uma chamada de ferramenta do conteúdo do LLM."""
    content = content.strip()
    start = content.find("{")
    if start == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(content[start:])
        if "action" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


# ── Helpers de integração ─────────────────────────────────────────

async def _query_rag(client: httpx.AsyncClient, query: str, n_results: int = 3) -> list[dict]:
    with tracer.start_as_current_span("agent.rag_query") as span:
        span.set_attribute("rag.n_results", n_results)
        span.set_attribute("rag.query.length", len(query))
        try:
            r = await client.post(
                f"{RETRIEVAL_SERVICE_URL}/query",
                json={"query": query, "n_results": n_results},
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            span.set_attribute("rag.results.count", len(results))
            return results
        except Exception as exc:
            span.record_exception(exc)
            return []


async def _call_llm(client: httpx.AsyncClient, messages: list, model: str) -> dict:
    """Chama o LLM Gateway. Levanta HTTPException em caso de falha (circuit breaker futuro)."""
    with tracer.start_as_current_span("agent.llm_call") as span:
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.messages.count", len(messages))
        try:
            r = await client.post(
                f"{LLM_GATEWAY_URL}/chat",
                json={
                    "messages": messages,
                    "model": model,
                    "max_tokens": LLM_MAX_TOKENS,
                },
            )
            r.raise_for_status()
            return r.json()["message"]
        except httpx.HTTPStatusError as e:
            span.record_exception(e)
            status_code = 503 if e.response.status_code >= 500 else 502
            raise HTTPException(
                status_code=status_code,
                detail=_response_detail(e.response, "LLM Gateway error"),
            )
        except httpx.TimeoutException as e:
            span.record_exception(e)
            raise HTTPException(
                status_code=504,
                detail="LLM Gateway demorou para responder. Tente novamente com uma pergunta mais curta ou aguarde o modelo terminar de carregar.",
            )
        except httpx.RequestError as e:
            span.record_exception(e)
            raise HTTPException(status_code=503, detail="LLM Gateway unavailable")


async def _get_tools(client: httpx.AsyncClient) -> list[dict]:
    with tracer.start_as_current_span("agent.get_tools") as span:
        try:
            r = await client.get(f"{TOOL_REGISTRY_URL}/tools")
            r.raise_for_status()
            tools = r.json().get("tools", [])
            span.set_attribute("tools.count", len(tools))
            return tools
        except Exception as exc:
            span.record_exception(exc)
            return []


async def _get_history(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    with tracer.start_as_current_span("agent.get_history") as span:
        span.set_attribute("session_id", session_id)
        try:
            r = await client.get(f"{MEMORY_SERVICE_URL}/sessions/{session_id}/messages")
            r.raise_for_status()
            messages = r.json().get("messages", [])
            span.set_attribute("memory.messages.count", len(messages))
            return messages
        except Exception as exc:
            span.record_exception(exc)
            return []


async def _save_message(client: httpx.AsyncClient, session_id: str, role: str, content: str):
    with tracer.start_as_current_span("agent.save_message") as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("message.role", role)
        span.set_attribute("message.length", len(content))
        try:
            await client.post(
                f"{MEMORY_SERVICE_URL}/sessions/{session_id}/messages",
                json={"session_id": session_id, "message": {"role": role, "content": content}},
            )
        except Exception as exc:
            span.record_exception(exc)
            pass  # Não bloqueia a resposta ao cliente


async def _invoke_tool(client: httpx.AsyncClient, tool_name: str, params: dict) -> dict:
    with tracer.start_as_current_span("agent.invoke_tool") as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.parameters.count", len(params))
        try:
            r = await client.post(
                f"{TOOL_REGISTRY_URL}/tools/invoke",
                json={"tool_name": tool_name, "parameters": params},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            span.record_exception(e)
            return {"error": str(e)}


def _response_detail(response: httpx.Response, fallback: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("detail") or data)
    except ValueError:
        pass
    text = response.text.strip()
    return text or fallback


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
