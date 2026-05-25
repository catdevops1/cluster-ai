# backend/telegram_bot.py
import asyncio
from kubernetes import client, config
import httpx
import os
import logging

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL       = "claude-haiku-4-5-20251001"

_offset: int = 0  # tracks last processed Telegram update


# ── Kubernetes client ────────────────────────────────────────
def k8s():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    configuration = client.Configuration.get_default_copy()
    configuration.retries = 1
    api_client = client.ApiClient(configuration)
    return (
        client.CoreV1Api(api_client),
        client.AppsV1Api(api_client),
    )


# ── Whitelist — read-only commands only ─────────────────────
HELP_TEXT = """
🤖 <b>Cluster AI Bot Commands</b>

/get nodes — list all nodes
/get ns — list namespaces
/get pods &lt;namespace&gt; — list pods in namespace
/get pods all — list all pods
/get deployments &lt;namespace&gt; — list deployments
/get deployments all — list all deployments
/get events &lt;namespace&gt; — recent warning events
/logs &lt;namespace&gt; &lt;pod&gt; — last 30 lines of pod logs
/status — full cluster summary via Claude
/alerts — current active alerts
/help — show this message
""".strip()


# ── Command handlers ─────────────────────────────────────────
def cmd_get_nodes(v1) -> str:
    lines = ["📊 <b>Nodes</b>\n"]
    for n in v1.list_node().items:
        status  = next((c.type for c in n.status.conditions if c.status == "True"), "Unknown")
        roles   = [k.replace("node-role.kubernetes.io/", "") for k in n.metadata.labels if "node-role" in k]
        cpu     = n.status.capacity.get("cpu", "?")
        mem     = n.status.capacity.get("memory", "?")
        icon    = "✅" if status == "Ready" else "❌"
        lines.append(f"{icon} <b>{n.metadata.name}</b> [{','.join(roles) or 'worker'}] cpu={cpu} mem={mem}")
    return "\n".join(lines)


def cmd_get_namespaces(v1) -> str:
    ns_list = [n.metadata.name for n in v1.list_namespace().items]
    lines   = ["📁 <b>Namespaces</b>\n"] + [f"  • {ns}" for ns in sorted(ns_list)]
    return "\n".join(lines)


def cmd_get_pods(v1, namespace: str) -> str:
    if namespace == "all":
        pods = v1.list_pod_for_all_namespaces().items
        title = "📦 <b>Pods — all namespaces</b>\n"
    else:
        pods  = v1.list_namespaced_pod(namespace).items
        title = f"📦 <b>Pods — {namespace}</b>\n"

    if not pods:
        return f"{title}\nNo pods found."

    lines = [title]
    for p in pods:
        ns       = p.metadata.namespace
        name     = p.metadata.name
        phase    = p.status.phase or "Unknown"
        restarts = sum(cs.restart_count for cs in (p.status.container_statuses or []))
        ready    = sum(1 for cs in (p.status.container_statuses or []) if cs.ready)
        total    = len(p.spec.containers)

        # pick waiting reason if available
        reason = phase
        for cs in (p.status.container_statuses or []):
            if cs.state.waiting:
                reason = cs.state.waiting.reason or phase
                break

        icon = "✅" if phase == "Running" and ready == total else "❌" if reason in ("CrashLoopBackOff", "Error", "OOMKilled") else "⚠️"
        ns_prefix = f"{ns}/" if namespace == "all" else ""
        lines.append(f"{icon} {ns_prefix}<b>{name}</b> {ready}/{total} {reason} restarts={restarts}")

    return "\n".join(lines)


def cmd_get_deployments(apps_v1, namespace: str) -> str:
    if namespace == "all":
        deps  = apps_v1.list_deployment_for_all_namespaces().items
        title = "🚀 <b>Deployments — all namespaces</b>\n"
    else:
        deps  = apps_v1.list_namespaced_deployment(namespace).items
        title = f"🚀 <b>Deployments — {namespace}</b>\n"

    if not deps:
        return f"{title}\nNo deployments found."

    lines = [title]
    for d in deps:
        desired = d.spec.replicas or 0
        ready   = d.status.ready_replicas or 0
        icon    = "✅" if ready == desired else "❌" if ready == 0 else "⚠️"
        ns_prefix = f"{d.metadata.namespace}/" if namespace == "all" else ""
        lines.append(f"{icon} {ns_prefix}<b>{d.metadata.name}</b> ready={ready}/{desired}")

    return "\n".join(lines)


def cmd_get_events(v1, namespace: str) -> str:
    events = v1.list_namespaced_event(namespace).items
    warns  = [e for e in events if e.type == "Warning"]
    warns.sort(key=lambda e: e.last_timestamp or e.event_time or "", reverse=True)

    if not warns:
        return f"✅ <b>Events — {namespace}</b>\n\nNo warnings found."

    lines = [f"⚠️ <b>Warning Events — {namespace}</b>\n"]
    for e in warns[:10]:  # cap at 10
        obj  = f"{e.involved_object.kind}/{e.involved_object.name}"
        lines.append(f"• <b>{e.reason}</b> {obj}\n  {e.message[:120]}")

    return "\n".join(lines)


def cmd_get_logs(v1, namespace: str, pod_name: str) -> str:
    # find pod — allow partial name match
    pods = v1.list_namespaced_pod(namespace).items
    match = next((p for p in pods if pod_name in p.metadata.name), None)

    if not match:
        return f"❌ Pod matching <b>{pod_name}</b> not found in <b>{namespace}</b>"

    try:
        logs = v1.read_namespaced_pod_log(
            name=match.metadata.name,
            namespace=namespace,
            tail_lines=30,
            timestamps=False,
        )
        if not logs.strip():
            return f"📋 <b>Logs — {match.metadata.name}</b>\n\n(no log output)"
        # Telegram message limit ~4096 chars
        truncated = logs[-3000:] if len(logs) > 3000 else logs
        return f"📋 <b>Logs — {match.metadata.name}</b>\n\n<pre>{truncated}</pre>"
    except Exception as e:
        return f"❌ Could not fetch logs: {e}"


async def cmd_status() -> str:
    if not ANTHROPIC_API_KEY:
        return "❌ Claude API key not configured."

    # gather cluster snapshot
    v1, apps_v1 = await asyncio.to_thread(k8s)
    nodes       = await asyncio.to_thread(lambda: v1.list_node().items)
    pods        = await asyncio.to_thread(lambda: v1.list_pod_for_all_namespaces().items)
    deps        = await asyncio.to_thread(lambda: apps_v1.list_deployment_for_all_namespaces().items)

    unhealthy_pods = [p for p in pods if p.status.phase not in ("Running", "Succeeded")]
    down_deps      = [d for d in deps if (d.status.ready_replicas or 0) == 0 and (d.spec.replicas or 0) > 0]
    node_issues    = [n for n in nodes if not any(c.status == "True" and c.type == "Ready" for c in n.status.conditions)]

    snapshot = (
        f"Nodes: {len(nodes)} total, {len(node_issues)} not ready\n"
        f"Pods: {len(pods)} total, {len(unhealthy_pods)} unhealthy\n"
        f"Deployments: {len(deps)} total, {len(down_deps)} down\n"
    )

    prompt = (
        f"You are a Kubernetes SRE. Current cluster snapshot for catdevops.net:\n\n{snapshot}\n"
        "Give a brief friendly status summary (max 6 lines). Use ✅ if all healthy, ⚠️ for warnings, ❌ for critical issues."
    )

    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            resp = await http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": CLAUDE_MODEL, "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            summary = resp.json()["content"][0]["text"]
            return f"🤖 <b>Cluster Status</b>\n\n{summary}"
        except Exception as e:
            return f"❌ Claude error: {e}"


# ── Route a parsed command ───────────────────────────────────
async def handle_command(text: str, active_alerts: set) -> str:
    parts = text.strip().split()
    if not parts:
        return HELP_TEXT

    cmd = parts[0].lower()

    # /help
    if cmd == "/help":
        return HELP_TEXT

    # /alerts
    if cmd == "/alerts":
        if not active_alerts:
            return "✅ <b>Active Alerts</b>\n\nNo active alerts."
        lines = ["🚨 <b>Active Alerts</b>\n"]
        for a in sorted(active_alerts):
            lines.append(f"• {a}")
        return "\n".join(lines)

    # /status
    if cmd == "/status":
        return await cmd_status()

    # /logs <namespace> <pod>
    if cmd == "/logs":
        if len(parts) < 3:
            return "Usage: /logs &lt;namespace&gt; &lt;pod&gt;"
        ns, pod = parts[1], parts[2]
        v1, _ = await asyncio.to_thread(k8s)
        return await asyncio.to_thread(cmd_get_logs, v1, ns, pod)

    # /get <resource> [namespace]
    if cmd == "/get":
        if len(parts) < 2:
            return "Usage: /get &lt;nodes|ns|pods|deployments|events&gt; [namespace]"

        resource = parts[1].lower()
        ns       = parts[2] if len(parts) > 2 else "default"

        v1, apps_v1 = await asyncio.to_thread(k8s)

        if resource in ("nodes", "node"):
            return await asyncio.to_thread(cmd_get_nodes, v1)
        elif resource in ("ns", "namespaces", "namespace"):
            return await asyncio.to_thread(cmd_get_namespaces, v1)
        elif resource in ("pods", "pod", "po"):
            return await asyncio.to_thread(cmd_get_pods, v1, ns)
        elif resource in ("deployments", "deployment", "deploy"):
            return await asyncio.to_thread(cmd_get_deployments, apps_v1, ns)
        elif resource in ("events", "event", "ev"):
            return await asyncio.to_thread(cmd_get_events, v1, ns)
        else:
            return f"❌ Unknown resource <b>{resource}</b>\n\n{HELP_TEXT}"

    return f"❌ Unknown command <b>{cmd}</b>\n\n{HELP_TEXT}"


# ── Send a message ───────────────────────────────────────────
async def send_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            await http.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        except Exception as e:
            logger.error(f"Telegram send error: {e}")


# ── Poll for updates ─────────────────────────────────────────
async def poll_telegram(active_alerts: set):
    global _offset
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=35.0) as http:
            resp = await http.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": _offset, "timeout": 30, "allowed_updates": ["message"]},
            )
            data = resp.json()
            for update in data.get("result", []):
                _offset = update["update_id"] + 1
                msg = update.get("message", {})

                # only accept messages from your configured chat
                if str(msg.get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
                    continue

                text = msg.get("text", "").strip()
                if not text.startswith("/"):
                    continue

                logger.info(f"Telegram command: {text}")
                reply = await handle_command(text, active_alerts)
                await send_message(reply)

    except Exception as e:
        logger.warning(f"Telegram poll error: {e}")


# ── Scheduler job (called every 3s from apscheduler) ─────────
def create_bot_job(scheduler, active_alerts: set):
    scheduler.add_job(
        poll_telegram,
        "interval",
        seconds=3,
        args=[active_alerts],
        id="telegram_bot",
        replace_existing=True,
    )
