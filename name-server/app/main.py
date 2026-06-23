"""
Name Server — Service Registry
Equivalente ao Eureka, mas em Python puro.
Serviços se registram via POST /register e consultam via GET /services.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import asyncio

app = FastAPI(title="Name Server", version="1.0.0")

# Registro em memória: { service_name: [{ url, last_heartbeat }] }
registry: dict[str, list[dict]] = {}

HEARTBEAT_TIMEOUT_SECONDS = 30


class ServiceInstance(BaseModel):
    name: str       # ex: "agent-service"
    url: str        # ex: "http://agent-service:8000"


@app.post("/register", status_code=201)
async def register(instance: ServiceInstance):
    """Registra ou renova uma instância de serviço."""
    if instance.name not in registry:
        registry[instance.name] = []

    # Atualiza se já existe, senão adiciona
    for entry in registry[instance.name]:
        if entry["url"] == instance.url:
            entry["last_heartbeat"] = datetime.utcnow()
            return {"status": "renewed"}

    registry[instance.name].append({
        "url": instance.url,
        "last_heartbeat": datetime.utcnow(),
    })
    return {"status": "registered"}


@app.delete("/register/{name}")
async def deregister(name: str, url: str):
    """Remove uma instância do registro."""
    if name not in registry:
        raise HTTPException(status_code=404, detail="Service not found")
    registry[name] = [e for e in registry[name] if e["url"] != url]
    return {"status": "deregistered"}


@app.get("/services")
async def list_services():
    """Lista todos os serviços registrados e ativos."""
    _evict_expired()
    return {
        name: [e["url"] for e in instances]
        for name, instances in registry.items()
    }


@app.get("/services/{name}")
async def get_service(name: str):
    """Retorna a URL de uma instância do serviço (round-robin simples)."""
    _evict_expired()
    instances = registry.get(name, [])
    if not instances:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    # Round-robin: rotaciona a lista
    instance = instances[0]
    registry[name] = instances[1:] + [instances[0]]
    return {"url": instance["url"]}


@app.get("/health")
async def health():
    return {"status": "ok", "registered_services": list(registry.keys())}


def _evict_expired():
    """Remove instâncias que não enviaram heartbeat recentemente."""
    cutoff = datetime.utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
    for name in list(registry.keys()):
        registry[name] = [e for e in registry[name] if e["last_heartbeat"] > cutoff]
