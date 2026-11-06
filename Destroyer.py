#!/usr/bin/env python3
"""Receive security webhooks and restart affected Kubernetes pods."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import datetime
import hmac
import json
import logging
import os
import ssl
from urllib import error, parse, request


LOGGER = logging.getLogger("destroyer")

SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SERVICE_ACCOUNT_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
DEFAULT_EXCLUDED_NAMESPACES = "kube-system,kube-public,kube-node-lease,neuvector"


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _env_set(name: str, default: str = "") -> set[str]:
    return {
        value.strip()
        for value in os.environ.get(name, default).split(",")
        if value.strip()
    }


TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL")
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN")
ENABLE_POD_REDEPLOY = _env_bool("ENABLE_POD_REDEPLOY", False)
REDEPLOY_MIN_AGE_SECONDS = _env_int("REDEPLOY_MIN_AGE_SECONDS", 15)
REDEPLOY_DRY_RUN = _env_bool("REDEPLOY_DRY_RUN", True)
REDEPLOY_ALLOWED_NAMESPACES = _env_set("REDEPLOY_ALLOWED_NAMESPACES")
REDEPLOY_EXCLUDED_NAMESPACES = _env_set(
    "REDEPLOY_EXCLUDED_NAMESPACES", DEFAULT_EXCLUDED_NAMESPACES
)
DEBUG = _env_bool("DEBUG")
K8S_TIMEOUT_SECONDS = _env_int("K8S_TIMEOUT_SECONDS", 5, minimum=1)
MAX_BODY_BYTES = _env_int("MAX_BODY_BYTES", 1_048_576, minimum=1)
POD_NAME = os.environ.get("POD_NAME") or os.environ.get("HOSTNAME", "")
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "")


def _debug(message: str) -> None:
    if DEBUG:
        LOGGER.info(message)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_event_time(event: dict) -> datetime.datetime:
    """Return an event timestamp, accepting seconds or milliseconds."""
    raw = event.get("reported_timestamp") or event.get("timestamp") or event.get("time")
    if raw is None:
        return _utcnow()
    try:
        value = int(raw)
        if value > 1_000_000_000_000:
            value //= 1000
        return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return _utcnow()


def _k8s_api_base() -> str:
    custom_api = os.environ.get("KUBERNETES_API_URL")
    if custom_api:
        _debug(f"Using custom Kubernetes API endpoint: {custom_api}")
        return custom_api.rstrip("/")

    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get(
        "KUBERNETES_SERVICE_PORT", "443"
    )
    return f"https://{host}:{port}"


def _k8s_request(
    method: str, path: str, body: bytes | None = None, timeout_s: int | None = None
) -> tuple[int, bytes]:
    timeout_s = K8S_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    in_cluster = os.path.exists(SERVICE_ACCOUNT_TOKEN) and os.path.exists(SERVICE_ACCOUNT_CA)
    custom_api = os.environ.get("KUBERNETES_API_URL")
    context = ssl.create_default_context()

    if in_cluster:
        with open(SERVICE_ACCOUNT_TOKEN, encoding="utf-8") as token_file:
            token = token_file.read().strip()
        context = ssl.create_default_context(cafile=SERVICE_ACCOUNT_CA)
    elif custom_api:
        token = os.environ.get("KUBERNETES_BEARER_TOKEN", "")
        if _env_bool("KUBERNETES_INSECURE_SKIP_VERIFY"):
            context = ssl._create_unverified_context()
        _debug("Running outside the cluster with the configured API endpoint")
    else:
        raise RuntimeError(
            "Kubernetes credentials not found; run in a cluster or set KUBERNETES_API_URL"
        )

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"

    req = request.Request(
        _k8s_api_base() + path, data=body, headers=headers, method=method
    )
    try:
        with request.urlopen(req, context=context, timeout=timeout_s) as response:
            return response.status, response.read()
    except error.HTTPError as exc:
        _debug(f"Kubernetes API returned HTTP {exc.code} for {method} {path}")
        return exc.code, exc.read() if hasattr(exc, "read") else b""


def _pick_started_at(pod_obj: dict, event: dict) -> datetime.datetime | None:
    wanted_name = event.get("workload_service") or event.get("container_name")
    statuses = (pod_obj.get("status") or {}).get("containerStatuses") or []

    chosen = next((status for status in statuses if status.get("name") == wanted_name), None)
    chosen = chosen or (statuses[0] if statuses else None)
    started_at = None
    if chosen:
        state = chosen.get("state") or {}
        started_at = (state.get("running") or state.get("terminated") or {}).get("startedAt")

    started_at = started_at or (pod_obj.get("metadata") or {}).get("creationTimestamp")
    if not started_at:
        return None
    try:
        return datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def maybe_redeploy_pod(event: dict) -> str:
    """Delete the pod identified by an event when the safety checks pass."""
    namespace = next(
        (event.get(name) for name in ("workload_domain", "namespace", "ns") if event.get(name)),
        None,
    )
    pod = next(
        (event.get(name) for name in ("workload_name", "pod", "pod_name") if event.get(name)),
        None,
    )
    if not isinstance(namespace, str) or not isinstance(pod, str):
        return "SKIPPED: could not determine namespace/pod from webhook"

    namespace = namespace.strip()
    pod = pod.strip()
    if not namespace or not pod:
        return "SKIPPED: could not determine namespace/pod from webhook"
    if namespace in REDEPLOY_EXCLUDED_NAMESPACES:
        return f"SKIPPED: namespace excluded ({namespace})"
    if not ENABLE_POD_REDEPLOY:
        return "SKIPPED: redeploy disabled"
    if not REDEPLOY_ALLOWED_NAMESPACES:
        return "SKIPPED: no namespaces approved"
    if namespace not in REDEPLOY_ALLOWED_NAMESPACES:
        return f"SKIPPED: namespace not approved ({namespace})"
    if POD_NAME and POD_NAMESPACE and namespace == POD_NAMESPACE and pod == POD_NAME:
        return "SKIPPED: refusing to delete Destroyer pod"

    namespace_q = parse.quote(namespace, safe="")
    pod_q = parse.quote(pod, safe="")
    _debug(f"Evaluating pod {namespace}/{pod}")

    try:
        status, data = _k8s_request(
            "GET",
            f"/api/v1/namespaces/{namespace_q}/pods/{pod_q}",
            timeout_s=K8S_TIMEOUT_SECONDS,
        )
        if status != 200:
            return f"SKIPPED: could not fetch pod (HTTP {status})"
        pod_obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"SKIPPED: invalid pod response ({exc})"
    except Exception as exc:
        return f"SKIPPED: error fetching pod ({exc})"

    started_at = _pick_started_at(pod_obj, event)
    if started_at is None:
        return "SKIPPED: could not determine pod/container start time"

    age_seconds = (_parse_event_time(event) - started_at).total_seconds()
    if age_seconds < REDEPLOY_MIN_AGE_SECONDS:
        return f"SKIPPED: pod age {age_seconds:.1f}s < {REDEPLOY_MIN_AGE_SECONDS}s"
    if REDEPLOY_DRY_RUN:
        return f"DRY_RUN: would delete pod {namespace}/{pod}"

    delete_body = json.dumps(
        {"gracePeriodSeconds": 30, "propagationPolicy": "Background"}
    ).encode("utf-8")
    try:
        status, _ = _k8s_request(
            "DELETE",
            f"/api/v1/namespaces/{namespace_q}/pods/{pod_q}",
            body=delete_body,
        )
        if status in (200, 202):
            return f"TRIGGERED: deleted pod {namespace}/{pod}"
        if status == 404:
            return "SKIPPED: pod already gone"
        if status == 403:
            return "SKIPPED: forbidden (RBAC)"
        return f"SKIPPED: delete returned HTTP {status}"
    except Exception as exc:
        return f"SKIPPED: delete failed ({exc})"


def _event_value(event: dict, name: str, default: str = "-") -> str:
    value = event.get(name, default)
    return default if value is None or value == "" else str(value)


def _post_json(url: str, payload: dict) -> None:
    request_body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        _debug(f"Notification webhook failed: {exc}")


def send_to_teams(event: dict, redeploy_status: str) -> None:
    if not TEAMS_WEBHOOK_URL:
        return

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "size": "Large",
                "weight": "Bolder",
                "text": "Security Alert",
            },
            {
                "type": "TextBlock",
                "text": f"{_event_value(event, 'name', 'Security event')} ({_event_value(event, 'level', '-')})",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Cluster", "value": _event_value(event, "cluster_name")},
                    {"title": "Namespace", "value": _event_value(event, "workload_domain")},
                    {"title": "Pod", "value": _event_value(event, "workload_name")},
                    {"title": "Image", "value": _event_value(event, "workload_image")},
                    {"title": "Command", "value": _event_value(event, "proc_cmd")},
                    {"title": "Parent", "value": _event_value(event, "proc_parent_name")},
                    {"title": "User", "value": _event_value(event, "proc_effective_user")},
                    {"title": "Action", "value": _event_value(event, "action")},
                    {"title": "Redeploy", "value": redeploy_status},
                ],
            },
        ],
    }
    _post_json(TEAMS_WEBHOOK_URL, card)


def send_to_alert_webhook(event: dict, redeploy_status: str) -> None:
    """Forward the event to an optional generic notification webhook."""
    if ALERT_WEBHOOK_URL:
        _post_json(
            ALERT_WEBHOOK_URL,
            {"event": event, "redeploy_status": redeploy_status},
        )


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not WEBHOOK_TOKEN:
        return True
    authorization = handler.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        authorization = authorization[7:].strip()
    return hmac.compare_digest(authorization, WEBHOOK_TOKEN)


class Handler(BaseHTTPRequestHandler):
    """HTTP endpoints used by probes and the security webhook."""

    server_version = "Destroyer"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        _debug(f"HTTP: {format % args}")

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if parse.urlsplit(self.path).path not in {"/", "/healthz"}:
            self._respond(404, "not found\n")
            return
        self._respond(200, "ok\n")

    def do_POST(self) -> None:
        if not _authorized(self):
            self._respond(401, "unauthorized\n")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._respond(400, "invalid Content-Length\n")
            return
        if length < 1 or length > MAX_BODY_BYTES:
            self._respond(413 if length > MAX_BODY_BYTES else 400, "invalid request body\n")
            return

        try:
            event = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, "invalid JSON\n")
            return
        if not isinstance(event, dict):
            self._respond(400, "JSON object required\n")
            return

        _debug(f"Received webhook ({length} bytes)")
        try:
            redeploy_status = maybe_redeploy_pod(event)
            LOGGER.info("Webhook processed: %s", redeploy_status)
            send_to_alert_webhook(event, redeploy_status)
            send_to_teams(event, redeploy_status)
        except Exception as exc:
            LOGGER.exception("Webhook processing failed: %s", exc)
            self._respond(500, "processing failed\n")
            return
        self._respond(200, "ok\n")


def main() -> None:
    global DEBUG, REDEPLOY_DRY_RUN

    parser = argparse.ArgumentParser(
        description="Restart Kubernetes pods in response to security webhooks"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without deleting pods")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--k8s-api", help="Kubernetes API URL for local testing")
    args = parser.parse_args()

    if args.debug:
        DEBUG = True
    if args.dry_run:
        REDEPLOY_DRY_RUN = True
    if args.k8s_api:
        os.environ["KUBERNETES_API_URL"] = args.k8s_api

    logging.basicConfig(
        level=logging.INFO if DEBUG else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOGGER.info("Starting server on port %s", args.port)
    if REDEPLOY_DRY_RUN:
        LOGGER.warning("Dry-run mode enabled; pods will not be deleted")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
