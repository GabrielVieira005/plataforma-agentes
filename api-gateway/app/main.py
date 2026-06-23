"""
API Gateway
Ponto de entrada único para todos os serviços.
- Roteamento para os microsserviços
- Rate limiting simples (por IP)
- Circuit breaker (via tenacity)
"""

import os
import time
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx

app = FastAPI(title="API Gateway", version="1.0.0")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

AGENT_SERVICE_URL  = os.getenv("AGENT_SERVICE_URL",  "http://localhost:8006")
NAME_SERVER_URL    = os.getenv("NAME_SERVER_URL",     "http://localhost:8000")

# ── Rate Limiting ─────────────────────────────────────────────────

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))   # por minuto
_request_counts: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    window = 60.0
    _request_counts[ip] = [t for t in _request_counts[ip] if now - t < window]
    if len(_request_counts[ip]) >= RATE_LIMIT_REQUESTS:
        return False
    _request_counts[ip].append(now)
    return True


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ip = request.client.host
    if not _check_rate_limit(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Limit: 60/min."},
        )
    return await call_next(request)


# ── Circuit Breaker ────────────────────────────────────────────────
# Estado simples: conta falhas consecutivas e abre o circuito

class SimpleCircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.state = "closed"   # closed | open | half-open

    def record_success(self):
        self.failures = 0
        self.state = "closed"

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "open"

    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half-open"
                return True
            return False
        return True  # half-open: permite um request de teste


agent_cb = SimpleCircuitBreaker(failure_threshold=3, recovery_timeout=30)


# ── Rotas de proxy ────────────────────────────────────────────────

@app.api_route("/agent/{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
async def proxy_agent(path: str, request: Request):
    """Proxy para o Agent Service com circuit breaker."""
    if not agent_cb.allow_request():
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Agent Service temporarily unavailable (circuit breaker open).",
                "fallback": "Por favor, tente novamente em alguns segundos.",
            },
        )

    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.request(
                method=request.method,
                url=f"{AGENT_SERVICE_URL}/{path}",
                content=body,
                headers={k: v for k, v in request.headers.items() if k != "host"},
                params=dict(request.query_params),
            )
        agent_cb.record_success()
        return JSONResponse(status_code=r.status_code, content=r.json())

    except (httpx.ConnectError, httpx.TimeoutException):
        agent_cb.record_failure()
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Agent Service unavailable.",
                "fallback": "O serviço está temporariamente indisponível.",
            },
        )


@app.get("/services")
async def proxy_services():
    """Lista todos os serviços registrados no name-server."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{NAME_SERVER_URL}/services")
        return r.json()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "circuit_breaker": {
            "agent-service": {
                "state": agent_cb.state,
                "failures": agent_cb.failures,
            }
        },
    }
