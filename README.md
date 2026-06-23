# Plataforma de Agentes Conversacionais

Projeto final de Engenharia de Software II — parceiro Nubo AI.

## Arquitetura

```
Cliente HTTP
    │
    ▼
api-gateway  ←──────  name-server (service registry)
    │
    ▼
agent-service  (ciclo agêntico: raciocínio → ação → observação)
    ├──► llm-gateway  (Ollama local)
    ├──► memory-service  (Redis + PostgreSQL)
    ├──► retrieval-service  (ChromaDB + RabbitMQ)
    └──► tool-registry  (calculadora, datetime, ...)
```

## Microsserviços e portas

| Serviço           | Porta | Tecnologia              |
|-------------------|-------|-------------------------|
| api-gateway       | 80    | FastAPI                 |
| name-server       | 8000  | FastAPI                 |
| chromadb          | 8001  | ChromaDB                |
| llm-gateway       | 8002  | FastAPI + Ollama        |
| memory-service    | 8003  | FastAPI + Redis + PG    |
| retrieval-service | 8004  | FastAPI + ChromaDB      |
| tool-registry     | 8005  | FastAPI                 |
| agent-service     | 8006  | FastAPI                 |
| rabbitmq UI       | 15672 | RabbitMQ Management     |
| jaeger UI         | 16686 | Jaeger                  |

## Como rodar localmente (sem Docker)

### Pré-requisitos
- Python 3.12+
- Redis rodando em localhost:6379
- PostgreSQL rodando em localhost:5432
- Ollama instalado: https://ollama.ai

### 1. Instalar Ollama e baixar um modelo
```bash
ollama pull llama3.2
```

### 2. Iniciar cada serviço em terminais separados

```bash
# Terminal 1 — name-server
cd name-server && pip install -r requirements.txt
uvicorn app.main:app --port 8000 --reload

# Terminal 2 — llm-gateway
cd llm-gateway && pip install -r requirements.txt
uvicorn app.main:app --port 8002 --reload

# Terminal 3 — memory-service
cd memory-service && pip install -r requirements.txt
DATABASE_URL=postgresql://agent:agent@localhost/memory \
uvicorn app.main:app --port 8003 --reload

# Terminal 4 — tool-registry
cd tool-registry && pip install -r requirements.txt
uvicorn app.main:app --port 8005 --reload

# Terminal 5 — agent-service
cd agent-service && pip install -r requirements.txt
uvicorn app.main:app --port 8006 --reload

# Terminal 6 — api-gateway
cd api-gateway && pip install -r requirements.txt
uvicorn app.main:app --port 80 --reload
```

## Como rodar com Docker Compose

```bash
# Subir toda a plataforma
docker compose up --build

# Baixar um modelo no Ollama (uma única vez)
docker exec -it plataforma-agentes-ollama-1 ollama pull llama3.2
```

## Testar

### Chat com o agente
```bash
curl -X POST http://localhost/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "teste-1", "message": "Quanto é 25 * 48?"}'
```

### Verificar circuit breaker
```bash
# Derrube o llm-gateway e tente chamar o agente
docker compose stop llm-gateway
curl -X POST http://localhost/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "teste-2", "message": "oi"}'
# Deve retornar mensagem de fallback do circuit breaker
```

### Ingerir documentos (RAG)
```bash
curl -X POST http://localhost:8004/ingest/sync \
  -H "Content-Type: application/json" \
  -d '{"documents": [{"content": "FastAPI é um framework web moderno para Python.", "metadata": {"source": "docs"}}]}'
```

### Consultar ferramentas disponíveis
```bash
curl http://localhost:8005/tools
```

## Entregas implementadas

- [x] **Entrega 1** — agent-service + llm-gateway comunicando via REST
- [x] **Entrega 2** — name-server + api-gateway + circuit breaker
- [x] **Entrega 3** — memory-service (Redis + Postgres) + retrieval-service (ChromaDB)
- [x] **Entrega 4** — RabbitMQ para ingestão assíncrona de documentos
- [x] **Entrega 5** — Dockerfiles + docker-compose.yaml
- [ ] **Entrega 6** — OpenTelemetry + Jaeger (parcial — instrumentação no agent-service)
- [ ] **Entrega 7** — Manifests Kubernetes
- [ ] **Entrega 8** — Relatório técnico + vídeo

## Estrutura de arquivos

```
plataforma-agentes/
├── docker-compose.yaml
├── README.md
├── agent-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
├── llm-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
├── memory-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
├── retrieval-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
├── tool-registry/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
├── api-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py
└── name-server/
    ├── Dockerfile
    ├── requirements.txt
    └── app/main.py
```
