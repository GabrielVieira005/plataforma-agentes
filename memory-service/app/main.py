"""
Memory Service
Armazena e recupera histórico de conversação.
- Redis: memória de curto prazo (mensagens recentes da sessão)
- PostgreSQL: memória de longo prazo (histórico persistido)
"""

import os
import json
import asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import httpx
import redis.asyncio as redis
import asyncpg

app = FastAPI(title="Memory Service", version="1.0.0")
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://agent:agent@localhost/memory")
NAME_SERVER_URL = os.getenv("NAME_SERVER_URL", "http://localhost:8000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "memory-service")
SERVICE_URL = os.getenv("SERVICE_URL", "http://localhost:8003")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))
SHORT_TERM_TTL = 3600  # 1 hora no Redis

redis_client: redis.Redis = None
pg_pool: asyncpg.Pool = None
registration_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global redis_client, pg_pool, registration_task
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    pg_pool = await asyncpg.create_pool(DATABASE_URL)
    await _create_tables()
    registration_task = asyncio.create_task(_registration_loop())


@app.on_event("shutdown")
async def shutdown():
    await _stop_registration()
    await redis_client.close()
    await pg_pool.close()


async def _create_tables():
    async with pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        """)


# ── Modelos ────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str       # "user" | "assistant" | "tool"
    content: str


class AddMessageRequest(BaseModel):
    session_id: str
    message: Message


# ── Rotas ─────────────────────────────────────────────────────────

@app.post("/sessions/{session_id}/messages", status_code=201)
async def add_message(session_id: str, req: AddMessageRequest):
    """Adiciona uma mensagem ao histórico (Redis + Postgres)."""
    msg = {"role": req.message.role, "content": req.message.content}

    # Curto prazo: Redis (lista, TTL)
    key = f"session:{session_id}:messages"
    await redis_client.rpush(key, json.dumps(msg))
    await redis_client.expire(key, SHORT_TERM_TTL)

    # Longo prazo: PostgreSQL
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(session_id, role, content) VALUES($1, $2, $3)",
            session_id, req.message.role, req.message.content,
        )

    return {"status": "ok"}


@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, limit: int = 20, source: str = "redis"):
    """
    Recupera o histórico de uma sessão.
    source='redis' → curto prazo (últimas mensagens)
    source='postgres' → longo prazo (histórico completo)
    """
    if source == "redis":
        key = f"session:{session_id}:messages"
        raw = await redis_client.lrange(key, -limit, -1)
        return {"messages": [json.loads(m) for m in raw]}

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content FROM messages WHERE session_id=$1 ORDER BY id DESC LIMIT $2",
            session_id, limit,
        )
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return {"messages": messages}


@app.delete("/sessions/{session_id}")
async def clear_session(session_id: str):
    """Limpa a memória de curto prazo de uma sessão."""
    key = f"session:{session_id}:messages"
    await redis_client.delete(key)
    return {"status": "cleared"}


@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    try:
        async with pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        pg_ok = True
    except Exception:
        pg_ok = False
    return {"redis": redis_ok, "postgres": pg_ok}


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
