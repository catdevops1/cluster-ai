# Cluster AI 🤖⎈

A natural language Kubernetes cluster assistant running on bare-metal. Ask questions about your cluster in plain English and get real-time answers powered by **Claude** (cloud, fast) or **Ollama** (local, private).

**Live at:** `cluster-ai.catdevops.net`(private)

---

## What It Does

- Fetches **live cluster data** (nodes, pods, deployments, namespaces..) from the Kubernetes API
- Sends that data as context to an LLM (Claude or Ollama)
- Returns human-readable answers with health indicators (✅ ⚠️ ❌)
- Blocks sensitive queries (secrets, tokens, credentials)

**Example questions:**
- "How many pods are running?"
- "What is the status of all nodes?"
- "Are there any pods in CrashLoopBackOff?"
- "Is ArgoCD healthy?"
- "Show me all deployments"

---

## Architecture

```
Browser (React UI)
    │
    ├── POST /api/ask { question, provider }
    │
FastAPI Backend (cluster-ai-api)
    │
    ├── Kubernetes API (via ServiceAccount) → live cluster data
    │
    ├── [provider=ollama] → Ollama pod (http://ollama:11434)
    │                         └── llama3.2:1b (local, free)
    │
    └── [provider=claude] → api.anthropic.com
                              └── claude-haiku-4-5-20251001
```

---

## Stack

| Component | Technology |
|-----------|------------|
| Frontend | React + Vite |
| Backend | FastAPI (Python) |
| Local LLM | Ollama (`llama3.2:1b`) |
| Cloud LLM | Anthropic Claude (`claude-haiku-4-5-20251001`) |
| Container Runtime | containerd |
| Orchestration | Kubernetes (bare-metal) |
| GitOps | ArgoCD (auto-sync) |
| Secrets | HashiCorp Vault + External Secrets Operator + AWS KMS |
| Ingress | Envoy Gateway + Cloudflare Tunnel |
| Storage | Longhorn (PVC for Ollama models) |
| Registry | GitHub Container Registry (ghcr.io) |

---

## Repository Structure

```
cluster-ai/
├── backend/
│   ├── main.py              # FastAPI app (Ollama + Claude routing)
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # React UI with provider toggle
│   │   └── main.jsx
│   ├── nginx.conf           # Reverse proxy to FastAPI
│   ├── Dockerfile
│   └── package.json
└── k8s/
    ├── namespace.yaml
    ├── fastapi.yaml         # cluster-ai-api Deployment
    ├── frontend.yaml        # cluster-ai-frontend Deployment
    ├── ollama.yaml          # Ollama Deployment (node04)
    ├── ollama-pull-job.yaml # Job to pull llama3.2:1b on deploy
    ├── rbac.yaml            # Read-only ClusterRole + binding
    ├── services.yaml
    ├── httproute.yaml       # Envoy Gateway HTTPRoute
    └── argocd-app.yaml
```

---

## Security

- **Read-only RBAC** — `cluster-ai-sa` ServiceAccount has `get`/`list` only on pods, nodes, deployments, namespaces, services. No secrets access.
- **Blocked queries** — requests containing `secret`, `token`, `password`, `credential`, `kubeconfig` are rejected with a friendly message before reaching the LLM.
- **API key management** — `ANTHROPIC_API_KEY` stored in HashiCorp Vault, synced to Kubernetes Secret via External Secrets Operator. Never hardcoded.
- **AWS KMS** — Vault auto-unseal uses AWS KMS. Master key never lives on the cluster.

---

## LLM Providers

### Ollama (Local)
- Model: `llama3.2:1b`
- Runs on `node04` (8 CPU, 32GB RAM)
- GPU: NVIDIA GTX 780M (CUDA 3.0 — too old for GPU inference, runs CPU-only)
- Response time: ~60 seconds
- Cost: Free

### Claude (Cloud)
- Model: `claude-haiku-4-5-20251001`
- Runs on Anthropic's infrastructure
- Response time: ~2-3 seconds
- Cost: ~$0.00025 per query (~20,000 queries per $5)

---

## Secrets Setup

API key stored in Vault:
```bash
k exec -it -n vault vault-0 -- vault kv put secret/cluster-ai/config \
  OLLAMA_URL="http://ollama.cluster-ai.svc.cluster.local:11434" \
  ANTHROPIC_API_KEY="sk-ant-..."
```

External Secret syncs to Kubernetes Secret automatically every hour.

Force immediate sync:
```bash
k annotate externalsecret cluster-ai-secret -n cluster-ai \
  force-sync=$(date +%s) --overwrite
```

---

## CI/CD

GitHub Actions builds and pushes Docker images to `ghcr.io/catdevops1/cluster-ai-*:latest` on every push to `main`. ArgoCD detects the Git change and syncs the manifests. Manual rollout required after image rebuild (using `latest` tag):

```bash
k rollout restart deployment/cluster-ai-api -n cluster-ai
k rollout restart deployment/cluster-ai-frontend -n cluster-ai
```

> **Known limitation:** ArgoCD does not auto-rollout on `latest` tag changes. Future improvement: use commit SHA tags.

---

## Local Development

```bash
# Port-forward to access UI locally
kubectl port-forward -n cluster-ai service/cluster-ai-frontend 8080:80 --address=0.0.0.0

# Access at
http://<node-ip>:8080
```

---

## Related Repos

- [`homelab-k8s-config-pub`](https://github.com/catdevops1/homelab-k8s-config-pub) — GitOps manifests (descheduler, external secrets, gateway, longhorn, vault)
