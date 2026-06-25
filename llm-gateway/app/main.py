"""
LLM Gateway
Proxy unificado para modelos de linguagem locais via LiteLLM + Ollama.
Abstrai o provedor de LLM para os demais serviços.
"""

import os
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any

app = FastAPI(title="LLM Gateway", version="1.0.0")
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

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.2")
NAME_SERVER_URL = os.getenv("NAME_SERVER_URL", "http://localhost:8000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "llm-gateway")
SERVICE_URL = os.getenv("SERVICE_URL", "http://localhost:8002")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))

registration_task: asyncio.Task | None = None


class ChatRequest(BaseModel):
    messages: list[dict]          # [{"role": "user", "content": "..."}]
    model: str = DEFAULT_MODEL
    temperature: float = 0.7
    max_tokens: int = 1024
    stream: bool = False


class ChatResponse(BaseModel):
    model: str
    message: dict                 # {"role": "assistant", "content": "..."}
    usage: dict | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Envia mensagens ao LLM local via Ollama e retorna a resposta.
    Outros serviços chamam APENAS este endpoint — nunca o Ollama diretamente.
    """
    payload = {
        "model": request.model,
        "messages": request.messages,
        "options": {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        },
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json=payload,
            )
            response.raise_for_status()
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Ollama unavailable")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=str(e))

    data = response.json()
    return ChatResponse(
        model=request.model,
        message=data["message"],
        usage={
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        },
    )


@app.get("/models")
async def list_models():
    """Lista os modelos disponíveis no Ollama."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            await client.get(f"{OLLAMA_URL}/api/tags")
            ollama_ok = True
        except Exception:
            ollama_ok = False
    return {"status": "ok" if ollama_ok else "degraded", "ollama": ollama_ok}


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
