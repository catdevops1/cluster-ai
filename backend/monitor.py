# backend/monitor.py
from kubernetes import client, config
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone
import httpx
import os
import logging

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL            = "claude-haiku-4-5-20251001"

RESTART_DELTA_THRESHOLD = 3    # new restarts per 5-min window to alert
CERT_EXPIRY_WARN_DAYS   = 14   # alert if cert expires within this many days

# External endpoints to probe — update with your actual public URLs
EXTERNAL_ENDPOINTS = [
    "https://catdevops.net",
    "https://fleet-track.catdevops.net",
    "https://job-track.catdevops.net",
    "https://invoice-track.catdevops.net",
    "https://metrics.catdevops.net",
]

_restart_snapshot: dict[str, int] = {}
_seeded: bool = False
_alerted: set[str] = set()


# ── Kubernetes client ────────────────────────────────────────
def k8s():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return (
        client.CoreV1Api(),
        client.AppsV1Api(),
        client.CustomObjectsApi(),
    )


# ── 1. Pod restarts + CrashLoopBackOff ──────────────────────
def check_pods(v1) -> list[dict]:
    global _seeded, _restart_snapshot
    issues = []
    pods = v1.list_pod_for_all_namespaces().items

    if not _seeded:
        for pod in pods:
            key = f"{pod.metadata.namespace}/{pod.metadata.name}"
            for cs in (pod.status.container_statuses or []):
                _restart_snapshot[key] = cs.restart_count
        _seeded = True
        logger.info(f"Restart snapshot seeded for {len(_restart_snapshot)} pods — no alerts on first run")
        return []

    for pod in pods:
        ns, name = pod.metadata.namespace, pod.metadata.name
        key = f"{ns}/{name}"
        for cs in (pod.status.container_statuses or []):
            current = cs.restart_count
            if cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                issues.append({"type": "CrashLoopBackOff", "namespace": ns,
                                "name": name, "detail": f"restarts: {current}"})
            else:
                prev  = _restart_snapshot.get(key, current)
                delta = current - prev
                if delta >= RESTART_DELTA_THRESHOLD:
                    issues.append({"type": "HighRestarts", "namespace": ns,
                                   "name": name, "detail": f"+{delta} new restarts (total: {current})"})
            _restart_snapshot[key] = current

    return issues


# ── 2. Node health ───────────────────────────────────────────
def check_nodes(v1) -> list[dict]:
    issues = []
    for node in v1.list_node().items:
        name = node.metadata.name
        for cond in (node.status.conditions or []):
            if cond.type == "Ready" and cond.status != "True":
                issues.append({"type": "NodeNotReady", "namespace": "kube-system",
                                "name": name, "detail": cond.reason})
            if cond.type == "DiskPressure" and cond.status == "True":
                issues.append({"type": "NodeDiskPressure", "namespace": "kube-system",
                                "name": name, "detail": "disk pressure condition active"})
            if cond.type == "MemoryPressure" and cond.status == "True":
                issues.append({"type": "NodeMemoryPressure", "namespace": "kube-system",
                                "name": name, "detail": "memory pressure condition active"})
    return issues


# ── 3. Deployment health ─────────────────────────────────────
def check_deployments(apps_v1) -> list[dict]:
    issues = []
    for dep in apps_v1.list_deployment_for_all_namespaces().items:
        desired = dep.spec.replicas or 0
        ready   = dep.status.ready_replicas or 0
        if desired > 0 and ready == 0:
            issues.append({"type": "DeploymentDown",
                            "namespace": dep.metadata.namespace,
                            "name": dep.metadata.name,
                            "detail": f"0/{desired} replicas ready"})
    return issues


# ── 4. Certificate expiry (cert-manager CRD) ─────────────────
def check_certificates(custom) -> list[dict]:
    issues = []
    try:
        certs = custom.list_cluster_custom_object(
            group="cert-manager.io", version="v1", plural="certificates"
        )
        now = datetime.now(timezone.utc)
        for cert in certs.get("items", []):
            ns   = cert["metadata"]["namespace"]
            name = cert["metadata"]["name"]

            for cond in cert.get("status", {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") != "True":
                    issues.append({"type": "CertNotReady", "namespace": ns,
                                   "name": name, "detail": cond.get("message", "not ready")})

            expiry_str = cert.get("status", {}).get("notAfter")
            if expiry_str:
                expiry    = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                days_left = (expiry - now).days
                if days_left <= CERT_EXPIRY_WARN_DAYS:
                    issues.append({"type": "CertExpiringSoon", "namespace": ns,
                                   "name": name,
                                   "detail": f"expires in {days_left} days ({expiry.strftime('%Y-%m-%d')})"})
    except Exception as e:
        logger.warning(f"Certificate check failed: {e}")
    return issues


# ── 5. Longhorn volume health ────────────────────────────────
def check_longhorn(custom) -> list[dict]:
    issues = []
    try:
        volumes = custom.list_cluster_custom_object(
            group="longhorn.io", version="v1beta2", plural="volumes"
        )
        for vol in volumes.get("items", []):
            name       = vol["metadata"]["name"]
            ns         = vol["metadata"].get("namespace", "longhorn-system")
            state      = vol.get("status", {}).get("state", "")
            robustness = vol.get("status", {}).get("robustness", "")
            if robustness in ("degraded", "faulted"):
                issues.append({"type": "LonghornVolumeDegraded", "namespace": ns,
                                "name": name, "detail": f"state={state} robustness={robustness}"})
    except Exception as e:
        logger.warning(f"Longhorn check failed: {e}")
    return issues


# ── 6. ArgoCD application health ─────────────────────────────
def check_argocd(custom) -> list[dict]:
    issues = []
    try:
        apps = custom.list_cluster_custom_object(
            group="argoproj.io", version="v1alpha1", plural="applications"
        )
        for app in apps.get("items", []):
            name        = app["metadata"]["name"]
            ns          = app["metadata"]["namespace"]
            health      = app.get("status", {}).get("health", {}).get("status", "")
            sync_status = app.get("status", {}).get("sync", {}).get("status", "")
            if health == "Degraded":
                issues.append({"type": "ArgoCDDegraded", "namespace": ns,
                                "name": name, "detail": f"health=Degraded sync={sync_status}"})
            elif sync_status == "OutOfSync":
                issues.append({"type": "ArgoCDOutOfSync", "namespace": ns,
                                "name": name, "detail": "application is OutOfSync"})
    except Exception as e:
        logger.warning(f"ArgoCD check failed: {e}")
    return issues


# ── 7. Vault sealed check ────────────────────────────────────
async def check_vault() -> list[dict]:
    issues = []
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as http:
            resp = await http.get("http://vault.vault.svc.cluster.local:8200/v1/sys/health")
            if resp.json().get("sealed"):
                issues.append({"type": "VaultSealed", "namespace": "vault",
                                "name": "vault", "detail": "Vault is sealed — secrets unavailable"})
    except Exception as e:
        logger.warning(f"Vault check failed: {e}")
    return issues


# ── 8. External endpoint probe ───────────────────────────────
async def check_endpoints() -> list[dict]:
    issues = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
        for url in EXTERNAL_ENDPOINTS:
            try:
                resp = await http.get(url)
                if resp.status_code >= 500:
                    issues.append({"type": "EndpointDown", "namespace": "external",
                                   "name": url, "detail": f"HTTP {resp.status_code}"})
            except Exception as e:
                issues.append({"type": "EndpointUnreachable", "namespace": "external",
                               "name": url, "detail": str(e)[:80]})
    return issues


# ── Claude summary ───────────────────────────────────────────
async def get_claude_summary(issues: list[dict]) -> str:
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured."

    issue_text = "\n".join(
        f"- [{i['type']}] {i['namespace']}/{i['name']}: {i['detail']}"
        for i in issues
    )
    prompt = (
        "You are a Kubernetes SRE. These issues were just detected in a bare-metal cluster:\n\n"
        f"{issue_text}\n\n"
        "Write a concise alert summary (max 8 lines). Include:\n"
        "1. The most critical issue and why\n"
        "2. Specific kubectl commands to investigate\n"
        "3. Likely root cause if obvious\n"
        "Keep it actionable and brief — this goes to Telegram."
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": CLAUDE_MODEL, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            return resp.json()["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude summary error: {e}")
        return "Could not generate summary."


# ── Telegram ─────────────────────────────────────────────────
async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            await http.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")


# ── Main monitor loop ────────────────────────────────────────
async def monitor_cluster():
    logger.info("Running cluster health check...")
    try:
        v1, apps_v1, custom = k8s()
        issues = (
            check_pods(v1)
            + check_nodes(v1)
            + check_deployments(apps_v1)
            + check_certificates(custom)
            + check_longhorn(custom)
            + check_argocd(custom)
            + await check_vault()
            + await check_endpoints()
        )
    except Exception as e:
        logger.error(f"Monitor error: {e}")
        return

    if not issues:
        logger.info("Cluster healthy — no issues detected")
        _alerted.clear()
        return

    new_issues = [
        i for i in issues
        if f"{i['type']}:{i['namespace']}/{i['name']}" not in _alerted
    ]

    if not new_issues:
        logger.info("Issues exist but already alerted — skipping")
        return

    for i in new_issues:
        _alerted.add(f"{i['type']}:{i['namespace']}/{i['name']}")

    TYPE_EMOJI = {
        "CrashLoopBackOff":       "❌",
        "DeploymentDown":         "❌",
        "NodeNotReady":           "❌",
        "VaultSealed":            "❌",
        "EndpointDown":           "❌",
        "EndpointUnreachable":    "❌",
        "LonghornVolumeDegraded": "🔴",
        "ArgoCDDegraded":         "🔴",
        "CertNotReady":           "🔴",
        "CertExpiringSoon":       "⏰",
        "ArgoCDOutOfSync":        "🟡",
        "HighRestarts":           "⚠️",
        "NodeDiskPressure":       "⚠️",
        "NodeMemoryPressure":     "⚠️",
    }

    lines = ["🚨 <b>Cluster Alert — catdevops.net</b>\n"]
    for i in new_issues:
        emoji = TYPE_EMOJI.get(i["type"], "⚠️")
        lines.append(f"{emoji} <b>{i['type']}</b>: {i['namespace']}/{i['name']} ({i['detail']})")

    summary = await get_claude_summary(new_issues)
    lines.append(f"\n🤖 <b>Claude says:</b>\n{summary}")
    lines.append("\n— Cluster AI Monitor")

    await send_telegram("\n".join(lines))
    logger.info(f"Alert sent for {len(new_issues)} new issues")


# ── Scheduler ────────────────────────────────────────────────
def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(monitor_cluster, "interval", minutes=5,
                      id="cluster_monitor", replace_existing=True)
    return scheduler
