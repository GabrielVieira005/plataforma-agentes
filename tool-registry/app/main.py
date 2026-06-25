"""
Tool Registry
Registra e expõe ferramentas que os agentes podem invocar.
Ferramentas built-in: calculadora, consulta de data/hora.
Ferramentas externas podem ser registradas dinamicamente.
"""

import math
import os
import asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any
import httpx

app = FastAPI(title="Tool Registry", version="1.0.0")
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

NAME_SERVER_URL = os.getenv("NAME_SERVER_URL", "http://localhost:8000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "tool-registry")
SERVICE_URL = os.getenv("SERVICE_URL", "http://localhost:8005")
REGISTRATION_INTERVAL_SECONDS = int(os.getenv("REGISTRATION_INTERVAL_SECONDS", "10"))

registration_task: asyncio.Task | None = None


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
    name = req.tool_name

    # Built-in
    if name in _builtin_tools:
        return _invoke_builtin(name, req.parameters)

    # Externa
    if name in _external_tools:
        import httpx
        tool = _external_tools[name]
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
    if name == "calculator":
        expr = params.get("expression", "")
        try:
            # Avaliação segura (apenas operações matemáticas)
            allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
            result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
            return {"result": result}
        except Exception as e:
            return {"error": str(e)}

    if name == "get_datetime":
        return {"datetime": datetime.now().isoformat(), "timezone": "local"}

    return {"error": "Unknown builtin"}
