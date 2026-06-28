# Kubernetes Manifests

These manifests are the Kubernetes version of `docker-compose.yaml`.

Docker Compose runs containers on one local machine. Kubernetes runs the same idea in a cluster:

- `Deployment` keeps app containers running and restarts them if they crash.
- `Service` gives each workload a stable internal DNS name, such as `http://agent-service:8000`.
- `ConfigMap` stores non-secret configuration.
- `Secret` stores credentials and connection strings.
- `PersistentVolumeClaim` asks the cluster for disk storage.
- `Ingress` exposes HTTP services through hostnames when an ingress controller exists.

## Files

```text
k8s/
├── namespace.yaml          # isolated namespace for the platform
├── configmap.yaml          # shared non-secret environment variables
├── secrets.yaml            # Postgres and RabbitMQ credentials
├── infra.yaml              # Redis, Postgres, RabbitMQ, ChromaDB, Ollama, Jaeger
├── apps.yaml               # platform microservices
├── ingress.yaml            # optional hostname routing
├── ollama-model-job.yaml   # pulls llama3.2 into Ollama
└── kustomization.yaml      # lets kubectl apply the folder as one unit
```

## Images

Compose builds images from local folders. Kubernetes expects images to already exist.

For a local cluster using Docker Desktop Kubernetes, build:

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

For Minikube, build inside Minikube's Docker daemon:

```bash
eval $(minikube docker-env)
# then run the docker build commands above
```

For kind, build locally and load the images:

```bash
kind load docker-image plataforma-agentes/name-server:latest
kind load docker-image plataforma-agentes/llm-gateway:latest
kind load docker-image plataforma-agentes/memory-service:latest
kind load docker-image plataforma-agentes/retrieval-service:latest
kind load docker-image plataforma-agentes/tool-registry:latest
kind load docker-image plataforma-agentes/agent-service:latest
kind load docker-image plataforma-agentes/api-gateway:latest
kind load docker-image plataforma-agentes/frontend:latest
```

For a real cloud cluster, push the images to a registry and replace the image names in `apps.yaml`, for example:

```text
ghcr.io/<org>/plataforma-agentes/agent-service:<tag>
```

If you rebuild an image but the pod still behaves like the old code, check the running image digest:

```bash
kubectl get pod -n plataforma-agentes -l app=api-gateway \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

Docker Desktop Kubernetes may keep using an older local `:latest` image. In that case, reset Docker Desktop Kubernetes or use a real image registry/tag for the changed image.

## Docker Desktop Kubernetes Check

Before applying the manifests, make sure `kubectl` is connected to Docker Desktop Kubernetes:

```bash
kubectl config get-contexts
kubectl config use-context docker-desktop
kubectl cluster-info
```

If `kubectl` prints `The connection to the server localhost:8080 was refused`, Kubernetes is not configured or not running. Open Docker Desktop, go to **Settings > Kubernetes**, enable Kubernetes, apply/restart, and wait until Docker Desktop shows Kubernetes as running.

## Apply

```bash
cd /Users/zk/Desktop/2026/es2/repo/plataforma-agentes
kubectl apply -k k8s/
kubectl get pods -n plataforma-agentes
kubectl get services -n plataforma-agentes
```

Wait until pods are `Running`:

```bash
kubectl wait --for=condition=available deployment/api-gateway -n plataforma-agentes --timeout=180s
kubectl wait --for=condition=available deployment/agent-service -n plataforma-agentes --timeout=180s
```

## Access Locally

The recommended local workflow is port-forwarding. The frontend ConfigMap points to the API Gateway at `http://localhost:8080`, matching the commands below.

The manifests also expose `frontend` on NodePort `30000` and `api-gateway` on NodePort `30080`, but some local Kubernetes setups do not route NodePorts through `localhost` correctly.

### Option A: NodePort

```bash
curl http://localhost:30080/health
```

```text
http://localhost:30000
```

If this works and you want to use NodePort for the frontend too, change `frontend-config` so `API_URL` and `HEALTH_URL` point to `http://localhost:30080`, then restart the frontend deployment.

### Option B: Port-Forward

Terminal 1:

```bash
kubectl port-forward -n plataforma-agentes svc/frontend 3000:80
```

Terminal 2:

```bash
kubectl port-forward -n plataforma-agentes svc/api-gateway 8080:80
```

Terminal 3:

```bash
curl -v http://localhost:3000
curl -v http://localhost:8080/health
```

Then open:

```text
http://localhost:3000
```

If you change `frontend-config`, apply the manifests or edit the ConfigMap, then restart the frontend deployment:

```bash
kubectl apply -k k8s/
kubectl rollout restart deployment/frontend -n plataforma-agentes
```

The frontend copies `frontend-config` into Nginx when the pod starts. Restarting the deployment is required after config changes.

The frontend HTML is also mounted from the `frontend-html` ConfigMap generated from `k8s/frontend-index.html`. This avoids stale Docker Desktop frontend images during local Kubernetes testing.

After the rollout, restart the frontend port-forward. A port-forward can break when the pod it was attached to is replaced:

```bash
# Ctrl+C the old frontend port-forward first
kubectl port-forward -n plataforma-agentes svc/frontend 3000:80
```

Verify:

```bash
curl -v http://localhost:3000
curl -v http://localhost:8080/health
```

Jaeger:

```bash
kubectl port-forward -n plataforma-agentes svc/jaeger 16686:16686
open http://localhost:16686
```

RabbitMQ UI:

```bash
kubectl port-forward -n plataforma-agentes svc/rabbitmq 15672:15672
open http://localhost:15672
```

Default RabbitMQ credentials are `guest` / `guest`.

## Test Checklist

Check pod readiness:

```bash
kubectl get pods -n plataforma-agentes
kubectl get svc -n plataforma-agentes
kubectl get endpoints -n plataforma-agentes
```

Check frontend:

```bash
curl -v http://localhost:3000
```

Check API Gateway:

```bash
curl -v http://localhost:8080/health
curl -v http://localhost:8080/health/services
```

If using NodePort instead of port-forward, use:

```bash
curl -v http://localhost:30080/health
curl -v http://localhost:30080/health/services
```

Test chat through port-forward:

```bash
curl -sS -X POST http://localhost:8080/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"k8s-test","message":"Responda uma frase curta."}'
```

Test chat through NodePort:

```bash
curl -sS -X POST http://localhost:30080/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"k8s-test","message":"Responda uma frase curta."}'
```

Debug logs:

```bash
kubectl logs -n plataforma-agentes deploy/frontend --tail=100
kubectl logs -n plataforma-agentes deploy/api-gateway --tail=100
kubectl logs -n plataforma-agentes deploy/agent-service --tail=100
kubectl logs -n plataforma-agentes deploy/llm-gateway --tail=100
```

## Optional Ingress

`ingress.yaml` assumes an NGINX ingress controller and these local hostnames:

```text
plataforma-agentes.local
api.plataforma-agentes.local
jaeger.plataforma-agentes.local
rabbitmq.plataforma-agentes.local
```

For local testing, add them to `/etc/hosts` pointing to the ingress IP.

For cloud, replace the hosts with real DNS names and configure TLS certificates.

## Production Notes

These manifests are deployment artifacts for Entrega 7, not a complete hardened production platform.

Before real production use:

- push images to a registry and pin immutable tags
- replace in-cluster Postgres/RabbitMQ/Chroma/Ollama with managed services when appropriate
- replace demo secrets with externally managed secrets
- configure TLS on Ingress
- add HorizontalPodAutoscalers for stateless services
- add NetworkPolicies
- tune CPU/memory requests using real measurements
- configure backup/restore for persistent data
- consider running `name-server` with more than one replica only after its registry state is externalized

## Cleanup

```bash
kubectl delete -k k8s/
```
