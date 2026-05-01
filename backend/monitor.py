# backend/monitor.py
from kubernetes import client, config
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import os
import logging

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL       = "claude-haiku-4-5-20251001"

# How many restarts before we alert
RESTART_THRESHOLD = 50

# Track already-alerted issues to avoid spam
_alerted = set()

# ── Kubernetes helpers ───────────────────────────────────────
def get_k8s_client():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()

def detect_issues() -> list[dict]:
    v1, apps_v1 = get_k8s_client()
    issues = []

    # Check pods
    for p in v1.list_pod_for_all_namespaces().items:
        ns   = p.metadata.namespace
        name = p.metadata.name

        for cs in (p.status.container_statuses or []):
            # CrashLoopBackOff
            if cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                issues.append({
                    "type": "CrashLoopBackOff",
                    "namespace": ns,
                    "name": name,
                    "detail": f"restarts: {cs.restart_count}"
                })
            # High restarts
            elif cs.restart_count >= RESTART_THRESHOLD:
                issues.append({
                    "type": "HighRestarts",
                    "namespace": ns,
                    "name": name,
                    "detail": f"restarts: {cs.restart_count}"
                })

    # Check nodes
    for n in v1.list_node().items:
        ready = next((c for c in n.status.conditions if c.type == "Ready"), None)
        if ready and ready.status != "True":
            issues.append({
                "type": "NodeNotReady",
                "namespace": "cluster",
                "name": n.metadata.name,
                "detail": f"status: {ready.status}"
            })

    # Check deployments
    for d in apps_v1.list_deployment_for_all_namespaces().items:
        desired = d.spec.replicas or 0
        ready   = d.status.ready_replicas or 0
        if desired > 0 and ready == 0:
            issues.append({
                "type": "DeploymentDown",
                "namespace": d.metadata.namespace,
                "name": d.metadata.name,
                "detail": f"ready: {ready}/{desired}"
            })

    return issues

# ── Claude summary ───────────────────────────────────────────
async def get_claude_summary(issues: list[dict]) -> str:
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured."

    issue_text = "\n".join(
        f"- [{i['type']}] {i['namespace']}/{i['name']} ({i['detail']})"
        for i in issues
    )

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 300,
        "system": "You are a Kubernetes cluster monitoring assistant. Be concise and actionable.",
        "messages": [{
            "role": "user",
            "content": f"These issues were detected in my Kubernetes cluster:\n{issue_text}\n\nProvide a brief 2-3 sentence summary of what's wrong and the most important action to take."
        }]
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
            return next((b["text"] for b in content if b["type"] == "text"), "No summary.")
        except Exception as e:
            logger.error(f"Claude summary error: {e}")
            return "Could not generate summary."

# ── Telegram ─────────────────────────────────────────────────
async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            await http.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            })
        except Exception as e:
            logger.error(f"Telegram error: {e}")

# ── Main monitor loop ────────────────────────────────────────
async def monitor_cluster():
    logger.info("Running cluster health check...")
    try:
        issues = detect_issues()
    except Exception as e:
        logger.error(f"Failed to detect issues: {e}")
        return

    if not issues:
        logger.info("Cluster healthy — no issues detected")
        # Clear alert history when cluster recovers
        _alerted.clear()
        return

    # Filter out already alerted issues to avoid spam
    new_issues = [
        i for i in issues
        if f"{i['type']}:{i['namespace']}/{i['name']}" not in _alerted
    ]

    if not new_issues:
        logger.info("Issues exist but already alerted — skipping")
        return

    # Mark as alerted
    for i in new_issues:
        _alerted.add(f"{i['type']}:{i['namespace']}/{i['name']}")

    # Build message
    lines = ["🚨 <b>Cluster Alert — catdevops.net</b>\n"]
    for i in new_issues:
        emoji = "❌" if i["type"] in ("CrashLoopBackOff", "DeploymentDown", "NodeNotReady") else "⚠️"
        lines.append(f"{emoji} <b>{i['type']}</b>: {i['namespace']}/{i['name']} ({i['detail']})")

    summary = await get_claude_summary(new_issues)
    lines.append(f"\n🤖 <b>Claude says:</b>\n{summary}")
    lines.append("\n— Cluster AI Monitor")

    message = "\n".join(lines)
    await send_telegram(message)
    logger.info(f"Alert sent for {len(new_issues)} new issues")

# ── Scheduler setup ──────────────────────────────────────────
def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        monitor_cluster,
        "interval",
        minutes=59,
        id="cluster_monitor",
        replace_existing=True,
        executor="threadpool",
    )
    return scheduler
