# backend/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kubernetes import client, config
import httpx
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cluster AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://cluster-ai.catdevops.net"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://ollama:11434")
MODEL_NAME       = os.getenv("MODEL_NAME", "llama3.2:1b")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL     = "claude-3-5-sonnet-20241022"

# ── Kubernetes client ────────────────────────────────────────
def get_k8s_client():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()

# ── Format cluster data as plain text for LLM context ────────
def fetch_cluster_data() -> str:
    v1, apps_v1 = get_k8s_client()
    lines = []

    lines.append("=== NODES ===")
    for n in v1.list_node().items:
        status  = next((c.type for c in n.status.conditions if c.status == "True"), "Unknown")
        roles   = [k.replace("node-role.kubernetes.io/", "") for k in n.metadata.labels if "node-role" in k]
        cpu     = n.status.capacity.get("cpu", "?")
        mem     = n.status.capacity.get("memory", "?")
        version = n.status.node_info.kubelet_version
        lines.append(f"  {n.metadata.name:20} {status:10} roles={','.join(roles) or 'worker':15} cpu={cpu} mem={mem} ver={version}")

    lines.append("\n=== PODS (all namespaces) ===")
    for p in v1.list_pod_for_all_namespaces().items:
        ns       = p.metadata.namespace
        name     = p.metadata.name
        phase    = p.status.phase or "Unknown"
        restarts = sum(cs.restart_count for cs in (p.status.container_statuses or []))
        ready    = sum(1 for cs in (p.status.container_statuses or []) if cs.ready)
        total    = len(p.spec.containers)
        lines.append(f"  {ns:25} {name:50} {ready}/{total}  {phase:20} restarts={restarts}")

    lines.append("\n=== DEPLOYMENTS ===")
    for d in apps_v1.list_deployment_for_all_namespaces().items:
        ns      = d.metadata.namespace
        name    = d.metadata.name
        desired = d.spec.replicas or 0
        ready   = d.status.ready_replicas or 0
        lines.append(f"  {ns:25} {name:40} ready={ready}/{desired}")

    lines.append("\n=== NAMESPACES ===")
    ns_list = [n.metadata.name for n in v1.list_namespace().items]
    lines.append("  " + ", ".join(ns_list))

    return "\n".join(lines)

# ── Shared helpers ───────────────────────────────────────────
BLOCKED = ["secret", "token", "password", "credential", "kubeconfig", "serviceaccount token"]

def build_system_prompt(cluster_ctx: str) -> str:
    return f"""You are a Kubernetes cluster assistant for catdevops.net.
You have access to the following LIVE cluster data fetched right now:

{cluster_ctx}

Rules:
- Answer ONLY based on the cluster data above
- Be concise and friendly
- Use ✅ for healthy, ⚠️ for warnings, ❌ for errors
- Never reveal secrets, tokens, or credentials
- If asked for anything outside cluster scope, politely decline
- Do not make up pod names or node names not in the data above"""

# ── Routes ───────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "ollama_model": MODEL_NAME, "claude_model": CLAUDE_MODEL}

@app.get("/api/cluster-data")
def cluster_data():
    try:
        return {"data": fetch_cluster_data()}
    except Exception as e:
        logger.error(f"cluster-data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class AskRequest(BaseModel):
    question: str
    provider: str = "ollama"  # "ollama" or "claude"

@app.post("/api/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")

    if any(b in req.question.lower() for b in BLOCKED):
        raise HTTPException(status_code=400, detail="Query not permitted")

    try:
        cluster_ctx = fetch_cluster_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch cluster data: {e}")

    system_prompt = build_system_prompt(cluster_ctx)

    if req.provider == "claude":
        return await ask_claude(req.question, system_prompt)
    else:
        return await ask_ollama(req.question, system_prompt)

# ── Ollama ───────────────────────────────────────────────────
async def ask_ollama(question: str, system_prompt: str) -> dict:
    payload = {
        "model": MODEL_NAME,
        "prompt": question,
        "system": system_prompt,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=300.0) as http:
        try:
            res = await http.post(f"{OLLAMA_URL}/api/generate", json=payload)
            res.raise_for_status()
            return {"answer": res.json().get("response", "No response from model"), "provider": "ollama"}
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Ollama timed out")
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            raise HTTPException(status_code=502, detail=f"Ollama error: {e}")

# ── Claude ───────────────────────────────────────────────────
async def ask_claude(question: str, system_prompt: str) -> dict:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Claude API key not configured")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": question}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            res = await http.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
            res.raise_for_status()
            content = res.json().get("content", [])
            answer = next((b["text"] for b in content if b["type"] == "text"), "No response")
            return {"answer": answer, "provider": "claude"}
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Claude API timed out")
        except Exception as e:
            logger.error(f"Claude error: {e}")
            raise HTTPException(status_code=502, detail=f"Claude error: {e}")
