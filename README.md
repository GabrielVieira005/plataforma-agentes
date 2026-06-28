
# Plataforma de Agentes Conversacionais
**Engenharia de Software II — Parceiro: Nubo AI**

MVP de uma plataforma para execução de agentes de IA conversacionais com arquitetura de microsserviços, executando localmente via Python + Docker para infraestrutura.

---

## Sumário

1. [Arquitetura](#arquitetura)
2. [Pré-requisitos](#pré-requisitos)
3. [Instalação](#instalação)
4. [Rodando os serviços](#rodando-os-serviços)
5. [Frontend](#frontend)
6. [Testando a plataforma](#testando-a-plataforma)
7. [Portas](#portas)
8. [Resolução de problemas](#resolução-de-problemas)

---

## Arquitetura

```
Browser (Frontend — http://localhost:3000)
        │
        ▼
   api-gateway :80        ◄────  name-server :8000
        │                              ▲
        ▼                              │ (registro)
 agent-service :8006  ────────────────┘
   │  (ciclo agêntico: raciocínio → ação → observação)
   ├──► llm-gateway :8002        →  Ollama :11434
   ├──► memory-service :8003     →  Redis :6379 + PostgreSQL :5432
   ├──► retrieval-service :8004  →  ChromaDB :8001 + RabbitMQ :5672
   └──► tool-registry :8005      (calculadora, datetime, ferramentas externas)

Observabilidade:
   todos os serviços FastAPI  →  Jaeger :16686  (rastreamento distribuído)
   RabbitMQ UI    →  http://localhost:15672
```

### Microsserviços

| # | Serviço | Responsabilidade | Stack |
|---|---------|-----------------|-------|
| 1 | agent-service | Ciclo agêntico (raciocínio → ação → observação) | FastAPI |
| 2 | llm-gateway | Proxy unificado para o LLM local | FastAPI + Ollama |
| 3 | memory-service | Histórico de conversação (curto e longo prazo) | FastAPI + Redis + PostgreSQL |
| 4 | retrieval-service | Busca semântica em documentos (RAG) | FastAPI + ChromaDB |
| 5 | tool-registry | Ferramentas invocáveis pelos agentes | FastAPI |
| 6 | api-gateway | Roteamento, rate limiting, circuit breaker | FastAPI |
| 7 | name-server | Descoberta de serviços (equivalente ao Eureka) | FastAPI |

---

## Pré-requisitos

### Softwares obrigatórios

| Software | Versão | Link | Observação |
|----------|--------|------|------------|
| Python | 3.12 | https://python.org/downloads | Versão validada para todos os serviços, incluindo RAG |
| Ollama | qualquer | https://ollama.com | LLM local |
| Docker Desktop | qualquer | https://docker.com/products/docker-desktop | Para Redis, RabbitMQ, ChromaDB |
| PostgreSQL | 16 | https://postgresql.org/download/windows | Instalar com pgAdmin |

### Modelo LLM
Após instalar o Ollama, baixar o modelo (necessário apenas uma vez, ~2GB):
```powershell
ollama pull llama3.2
```

---

## Instalação

### 1. Configurar PostgreSQL

Abrir o **SQL Shell (psql)** no menu iniciar, apertar Enter 4 vezes para aceitar os defaults e digitar a senha definida na instalação. Depois executar:

```sql
CREATE USER agent WITH PASSWORD 'agent';
CREATE DATABASE memory OWNER agent;
\q
```

### 2. Subir infraestrutura com Docker

Abrir o **Docker Desktop** e aguardar ficar com status "running". Depois:

```powershell
# Redis (memória de curto prazo)
docker run -d --name redis -p 6379:6379 redis:7-alpine

# RabbitMQ (mensageria assíncrona)
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine

# ChromaDB (banco vetorial para RAG)
docker run -d --name chromadb -p 8001:8000 chromadb/chroma:0.5.23
```

Verificar se subiram:
```powershell
docker ps
```

Deve listar `redis`, `rabbitmq` e `chromadb` com status `Up`.

> **Alternativa para Redis no WSL:** Se preferir não usar Docker para o Redis, abrir o terminal Ubuntu/WSL e rodar:
> ```bash
> sudo service redis-server start
> redis-cli ping  # deve retornar PONG
> ```

### 3. Instalar dependências Python

Em cada pasta de serviço, instalar os requirements. Exemplo:
```powershell
cd agent-service
pip install -r requirements.txt
```

Para rodar em todas as pastas ao mesmo tempo da raiz:
```
python -m pip install -r retrieval-service/requirements.txt
python -m pip install -r tool-registry/requirements.txt
python -m pip install -r agent-service/requirements.txt
python -m pip install -r api-gateway/requirements.txt
python -m pip install -r llm-gateway/requirements.txt
python -m pip install -r memory-service/requirements.txt
python -m pip install -r name-server/requirements.txt
```

Repetir para: `llm-gateway`, `memory-service`, `tool-registry`, `api-gateway`, `name-server`.

Para o `retrieval-service`, instalar manualmente (evita compilação do C++):
```powershell
cd retrieval-service
pip install chromadb-client==0.5.23 aio-pika fastapi "uvicorn[standard]" httpx pydantic
```

---

## Rodando os serviços

Abrir **um terminal PowerShell separado para cada serviço** dentro da pasta `plataforma-agentes/`.

> **Dica:** Use `python -m uvicorn` em vez de `uvicorn` para evitar problemas de PATH no Windows.

**Terminal 1 — name-server** (inicia primeiro)
```powershell
cd name-server
python -m uvicorn app.main:app --port 8000 --reload
```

**Terminal 2 — llm-gateway**
```powershell
cd llm-gateway
python -m uvicorn app.main:app --port 8002 --reload
```

**Terminal 3 — memory-service**
```powershell
cd memory-service
$env:DATABASE_URL="postgresql://agent:agent@localhost/memory"
python -m uvicorn app.main:app --port 8003 --reload
```

**Terminal 4 — tool-registry**
```powershell
cd tool-registry
python -m uvicorn app.main:app --port 8005 --reload
```

**Terminal 5 — agent-service**
```powershell
cd agent-service
python -m uvicorn app.main:app --port 8006 --reload
```

**Terminal 6 — retrieval-service** *(opcional — requer ChromaDB e RabbitMQ)*
```powershell
cd retrieval-service
python -m uvicorn app.main:app --port 8004 --reload
```

**Terminal 7 — api-gateway** *(opcional — circuit breaker e roteamento)*
```powershell
cd api-gateway
$env:AGENT_SERVICE_URL="http://localhost:8006"
$env:NAME_SERVER_URL="http://localhost:8000"
python -m uvicorn app.main:app --port 80 --reload
```

### Ordem recomendada de inicialização

```
name-server → llm-gateway → memory-service → tool-registry → agent-service
```

Os demais (retrieval-service, api-gateway) são opcionais para o fluxo básico de chat.

---

## Frontend

O frontend é um único arquivo HTML sem dependências de build.

### Iniciar o servidor de desenvolvimento
```powershell
python -m http.server 3000 --directory frontend
```

### Acessar
```
http://localhost:3000
```

> **Por que não abrir o arquivo diretamente?**  
> Abrir `index.html` via `file://` faz o navegador bloquear as requisições para `localhost` por política de CORS. O servidor Python resolve isso.

### Funcionalidades do frontend

- **Chat** com o agente, histórico de sessões salvo no navegador
- **Múltiplas sessões** — crie e alterne entre conversas diferentes
- **Painel de serviços** — status em tempo real de todos os microsserviços
- **RAG** — indexar documentos e fazer buscas semânticas diretamente pela interface

---

## Testando a plataforma

### Health check de todos os serviços

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8000/health -Method GET | Select-Object -ExpandProperty Content
Invoke-WebRequest -Uri http://localhost:8002/health -Method GET | Select-Object -ExpandProperty Content
Invoke-WebRequest -Uri http://localhost:8003/health -Method GET | Select-Object -ExpandProperty Content
Invoke-WebRequest -Uri http://localhost:8004/health -Method GET | Select-Object -ExpandProperty Content
Invoke-WebRequest -Uri http://localhost:8005/health -Method GET | Select-Object -ExpandProperty Content
Invoke-WebRequest -Uri http://localhost:8006/health -Method GET | Select-Object -ExpandProperty Content
```

**curl (Linux/Mac/WSL):**
```bash
curl http://localhost:8000/health
curl http://localhost:8002/health
curl http://localhost:8003/health
curl http://localhost:8004/health
curl http://localhost:8005/health
curl http://localhost:8006/health
```

---

### Chat com o agente

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8006/chat `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"session_id": "s1", "message": "Quanto eh 25 vezes 48?"}' `
  | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl -X POST http://localhost:8006/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "s1", "message": "Quanto eh 25 vezes 48?"}'
```

Resposta esperada:
```json
{"session_id": "s1", "response": "25 vezes 48 é 1200.", "iterations": 2}
```

---

### Testar memória (agente lembra o contexto)

**PowerShell:**
```powershell
# Primeira mensagem
Invoke-WebRequest -Uri http://localhost:8006/chat `
  -Method POST -ContentType "application/json" `
  -Body '{"session_id": "memoria-teste", "message": "Meu nome eh Gabriel"}' `
  | Select-Object -ExpandProperty Content

# Segunda mensagem — agente deve lembrar o nome
Invoke-WebRequest -Uri http://localhost:8006/chat `
  -Method POST -ContentType "application/json" `
  -Body '{"session_id": "memoria-teste", "message": "Qual eh o meu nome?"}' `
  | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl -X POST http://localhost:8006/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "memoria-teste", "message": "Meu nome eh Gabriel"}'

curl -X POST http://localhost:8006/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "memoria-teste", "message": "Qual eh o meu nome?"}'
```

---

### Consultar histórico de sessão

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8003/sessions/memoria-teste/messages `
  -Method GET | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl http://localhost:8003/sessions/memoria-teste/messages
```

---

### Listar ferramentas disponíveis

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8005/tools `
  -Method GET | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl http://localhost:8005/tools
```

---

### Invocar ferramenta diretamente

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8005/tools/invoke `
  -Method POST -ContentType "application/json" `
  -Body '{"tool_name": "calculator", "parameters": {"expression": "144 / 12"}}' `
  | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl -X POST http://localhost:8005/tools/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "calculator", "parameters": {"expression": "144 / 12"}}'
```

---

### Ingerir documento no RAG

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8004/ingest/sync `
  -Method POST -ContentType "application/json" `
  -Body '{"documents": [{"content": "FastAPI eh um framework Python moderno para construir APIs REST de alta performance.", "metadata": {"source": "docs", "autor": "Gabriel"}}]}' `
  | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl -X POST http://localhost:8004/ingest/sync \
  -H "Content-Type: application/json" \
  -d '{"documents": [{"content": "FastAPI eh um framework Python moderno para construir APIs REST de alta performance.", "metadata": {"source": "docs"}}]}'
```

### Ingerir URL no RAG

**curl:**
```bash
curl -X POST http://localhost:8004/ingest/sync \
  -H "Content-Type: application/json" \
  -d '{"documents": [{"url": "https://example.com", "metadata": {"source": "web"}}]}'
```

Isso fará com que o serviço acesse a página, extraia o texto e o indexe no ChromaDB.

---

### Busca semântica no RAG

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8004/query `
  -Method POST -ContentType "application/json" `
  -Body '{"query": "framework para APIs em Python", "n_results": 3}' `
  | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl -X POST http://localhost:8004/query \
  -H "Content-Type: application/json" \
  -d '{"query": "framework para APIs em Python", "n_results": 3}'
```

---

### Testar circuit breaker (api-gateway)

```powershell
# 1. Pare o agent-service (CTRL+C no terminal dele)

# 2. Tente chamar pelo gateway 4 vezes
# Tentativas 1-3: "Agent Service unavailable"
# Tentativa 4+:  "circuit breaker open" (circuito aberto)
Invoke-WebRequest -Uri http://localhost/agent/chat `
  -Method POST -ContentType "application/json" `
  -Body '{"session_id": "s1", "message": "oi"}' `
  | Select-Object -ExpandProperty Content

# 3. Verifique o estado do circuit breaker
Invoke-WebRequest -Uri http://localhost/health `
  -Method GET | Select-Object -ExpandProperty Content
```

---

### Listar modelos disponíveis no Ollama

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8002/models `
  -Method GET | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl http://localhost:8002/models
```

---

### Verificar serviços registrados no name-server

**PowerShell:**
```powershell
Invoke-WebRequest -Uri http://localhost:8000/services `
  -Method GET | Select-Object -ExpandProperty Content
```

**curl:**
```bash
curl http://localhost:8000/services
```

---

## Portas

| Serviço | Porta | URL |
|---------|-------|-----|
| Frontend | 3000 | http://localhost:3000 |
| api-gateway | 80 | http://localhost |
| name-server | 8000 | http://localhost:8000 |
| ChromaDB | 8001 | http://localhost:8001 |
| llm-gateway | 8002 | http://localhost:8002 |
| memory-service | 8003 | http://localhost:8003 |
| retrieval-service | 8004 | http://localhost:8004 |
| tool-registry | 8005 | http://localhost:8005 |
| agent-service | 8006 | http://localhost:8006 |
| RabbitMQ UI | 15672 | http://localhost:15672 (guest/guest) |
| Ollama | 11434 | http://localhost:11434 |
| Redis | 6379 | — |
| PostgreSQL | 5432 | — |

---

## Resolução de problemas

### `uvicorn` não reconhecido no PowerShell
Use sempre `python -m uvicorn` em vez de `uvicorn` diretamente.

### Erro de CORS no frontend
Cada `main.py` precisa ter o middleware de CORS logo após `app = FastAPI(...)`:
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Frontend mostra serviço offline mas curl retorna ok
É CORS. Abrir F12 → Console no navegador e verificar o erro vermelho. Garantir que o middleware de CORS está adicionado no serviço em questão.

### memory-service: erro de conexão com Redis
O Redis não está rodando. Iniciar via Docker:
```powershell
docker start redis
```
Ou via WSL:
```bash
sudo service redis-server start
```

### memory-service: erro de conexão com PostgreSQL
Verificar se o PostgreSQL está rodando. No Windows, abrir o "Services" (services.msc) e procurar por `postgresql-x64-16`.

### OpenTelemetry não aparece no Jaeger
Verificar se o `jaeger` está rodando no Docker Compose e se os serviços foram iniciados com `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317`.
Depois gerar tráfego real, por exemplo chamando `/agent/chat` pelo gateway. O Jaeger só mostra serviços depois que eles exportam pelo menos um span.

### `pydantic-core` falha na instalação
Ocorre quando o pip tenta compilar do zero. Usar versões com wheels pré-compilados:
```
pydantic==2.9.2
```

### `chromadb` falha na instalação (erro de C++)
Instalar apenas o cliente HTTP, sem a biblioteca completa:
```powershell
pip install chromadb-client==0.5.23
```
O ChromaDB server roda via Docker na porta 8001.

### RabbitMQ não inicia (erro ERLANG_HOME)
O instalador do Windows do RabbitMQ pode ter problemas com a versão do Erlang. Usar Docker:
```powershell
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine
```

### Docker: container com nome já existe
```powershell
docker rm -f redis rabbitmq chromadb
# Depois recriar com docker run
```

### Restartar todos os containers Docker de uma vez
```powershell
docker start redis rabbitmq chromadb
```

---

## Docker Compose (Entrega 5)

Para rodar toda a infraestrutura e serviços em containers:

```bash
docker compose up --build
```

Frontend:

```text
http://localhost:3000
```

Testar gateway:

```bash
curl http://localhost/health
curl http://localhost/health/services
```

Testar chat:

```bash
curl -sS -X POST http://localhost/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"docker-test","message":"Responda uma frase curta."}'
```

Baixar modelo no Ollama (apenas na primeira vez):
```bash
docker exec -it plataforma-agentes-ollama-1 ollama pull llama3.2
```

Parar tudo:
```bash
docker compose down
```

---

## Kubernetes (Entrega 7)

Os manifests ficam em `k8s/`.

Antes de aplicar, confirme que o Kubernetes do Docker Desktop está ativo:

```bash
kubectl config get-contexts
kubectl config use-context docker-desktop
kubectl cluster-info
```

Se `kubectl` tentar acessar `localhost:8080` e falhar, o problema é o contexto do Kubernetes, não os manifests. Abra o Docker Desktop, vá em **Settings > Kubernetes**, habilite Kubernetes e espere o status ficar running.

Kubernetes não faz build local das imagens automaticamente. Antes de aplicar os manifests, crie as imagens:

```bash
docker build -t plataforma-agentes/name-server:latest ./name-server
docker build -t plataforma-agentes/llm-gateway:latest ./llm-gateway
docker build -t plataforma-agentes/memory-service:latest ./memory-service
docker build -t plataforma-agentes/retrieval-service:latest ./retrieval-service
docker build -t plataforma-agentes/tool-registry:latest ./tool-registry
docker build -t plataforma-agentes/agent-service:latest ./agent-service
docker build -t plataforma-agentes/api-gateway:latest ./api-gateway
docker build -t plataforma-agentes/frontend:latest ./frontend
```

Aplicar:

```bash
cd /Users/zk/Desktop/2026/es2/repo/plataforma-agentes
kubectl apply -k k8s/
kubectl get pods -n plataforma-agentes
```

Forma mais confiável para rodar localmente, em terminais separados:

Terminal 1:

```bash
kubectl port-forward -n plataforma-agentes svc/frontend 3000:80
```

Terminal 2:

```bash
kubectl port-forward -n plataforma-agentes svc/api-gateway 8080:80
```

Abrir:

```text
http://localhost:3000
```

Testar:

```bash
curl http://localhost:3000
curl http://localhost:8080/health
curl http://localhost:8080/health/services
curl -sS -X POST http://localhost:8080/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"k8s-test","message":"Responda uma frase curta."}'
```

Se `/health` estiver ok mas o chat no frontend disser que não conseguiu chamar o gateway, reinicie o port-forward do `api-gateway`. Depois de um rollout, o port-forward antigo pode ficar preso em um pod substituído:

```bash
# Ctrl+C no port-forward antigo primeiro
kubectl port-forward -n plataforma-agentes svc/api-gateway 8080:80
```

Se o teste de chat retornar `504`, a chamada chegou ao gateway, mas o caminho `agent-service -> llm-gateway -> Ollama` excedeu o timeout configurado. Verifique:

```bash
kubectl logs -n plataforma-agentes deploy/llm-gateway --tail=160
kubectl logs -n plataforma-agentes deploy/ollama --tail=160
```

Se alterar o `frontend-config`, reinicie o deployment e também reinicie o port-forward do frontend:

```bash
kubectl apply -k k8s/
kubectl rollout restart deployment/frontend -n plataforma-agentes
```

Durante o desenvolvimento local, alguns serviços Python também recebem código via ConfigMap para evitar imagens `:latest` antigas no Docker Desktop. Depois de alterar código de `agent-service`, `api-gateway` ou `llm-gateway` e aplicar os manifests, reinicie o deployment alterado:

```bash
kubectl apply -k k8s/
kubectl rollout restart deployment/agent-service -n plataforma-agentes
```

Mais detalhes: `k8s/README.md`.

---

## Observabilidade (Entrega 6)

O projeto usa OpenTelemetry para rastreamento distribuído e Jaeger para visualização.

Serviços instrumentados:

- `api-gateway`
- `agent-service`
- `llm-gateway`
- `memory-service`
- `retrieval-service`
- `tool-registry`
- `name-server`

O que é rastreado automaticamente:

- requisições FastAPI recebidas por cada serviço
- chamadas HTTP feitas com `httpx`
- propagação de contexto entre serviços via headers W3C Trace Context

Spans manuais importantes:

- `gateway.proxy_agent`
- `gateway.proxy_request`
- `agent.chat`
- `agent.rag_query`
- `agent.llm_call`
- `agent.get_history`
- `agent.save_message`
- `agent.invoke_tool`
- `llm.ollama_chat`
- `memory.add_message`
- `memory.get_messages`
- `retrieval.ingest_sync`
- `retrieval.ingest_async`
- `retrieval.ingest_file`
- `retrieval.query`
- `tool.invoke`
- `tool.invoke_builtin`
- `registry.register`

Para testar:

```bash
docker compose up --build
curl -sS -X POST http://localhost/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"otel-test","message":"Com base nos documentos, responda algo simples sobre FastAPI."}'
```

Depois abrir:

```text
http://localhost:16686
```

No Jaeger, selecionar um serviço como `api-gateway` ou `agent-service` e clicar em **Find Traces**. Uma chamada de chat deve mostrar uma cadeia parecida com:

```text
api-gateway
  → agent-service
    → tool-registry
    → retrieval-service
    → memory-service
    → llm-gateway
      → ollama
```

---

## Status das entregas

| Entrega | Descrição | Status |
|---------|-----------|--------|
| 1 | agent-service + llm-gateway funcionando via REST | ✅ |
| 2 | api-gateway + name-server + circuit breaker | ✅ |
| 3 | memory-service (Redis + PostgreSQL) + retrieval-service (ChromaDB) | ✅ |
| 4 | RabbitMQ para ingestão assíncrona de documentos | ✅ |
| 5 | Dockerfiles + docker-compose.yaml | ✅ |
| 6 | OpenTelemetry + Jaeger | ✅ |
| 7 | Manifests Kubernetes | ✅ |
| 8 | Relatório técnico + vídeo de demonstração | ⏳ |

---

## Estrutura do repositório

```
plataforma-agentes/
├── docker-compose.yaml
├── README.md
├── frontend/
│   └── index.html              # Interface web (abrir com http.server)
├── agent-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # Ciclo agêntico principal
├── llm-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # Proxy para Ollama
├── memory-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # Redis + PostgreSQL
├── retrieval-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # ChromaDB + RabbitMQ consumer
├── tool-registry/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # Ferramentas dos agentes
├── api-gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/main.py             # Circuit breaker + rate limiting
└── name-server/
    ├── Dockerfile
    ├── requirements.txt
    └── app/main.py             # Service registry
