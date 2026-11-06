# Destroyer

Destroyer is a lightweight Kubernetes webhook listener for responding to container security alerts. It receives NeuVector events, checks the target pod against safety and namespace policy, and either deletes the pod or skips deletion while forwarding the alert to configured notification systems.

Deleting a pod is destructive. Start in dry-run mode, restrict access to the webhook, and explicitly configure the namespaces approved for deletion before enabling it in production.

Note: This repository is not actively maintained.

See [PUBLISHING.md](PUBLISHING.md) for beginner-friendly steps to publish a
GitHub release and install the Helm chart from GitHub Container Registry.

## How it works

1. NeuVector detects suspicious container behavior and sends a JSON webhook.
2. Destroyer extracts the namespace, pod, and optional container and event timestamp.
3. Destroyer applies namespace, enablement, dry-run, and minimum-age checks.
4. An eligible pod is deleted so its Kubernetes controller can recreate it.
5. The event and resulting action can be sent to Microsoft Teams and an optional generic alert webhook.

NeuVector policy configuration is outside this project. This repository covers Destroyer deployment, configuration, and testing.

## Requirements

- Python 3.10 or newer for local use
- Helm 3.8 or newer for OCI chart installation
- A Kubernetes cluster with RBAC enabled for cluster deployment
- NeuVector configured to send JSON security-event webhooks

Destroyer uses only the Python standard library.

## Install locally

```bash
git clone https://github.com/zinkozapper/Destroyer.git
cd Destroyer
python3 -m venv .venv
. .venv/bin/activate
```

There are no packages to install. Run the service against a local or test Kubernetes API:

```bash
python Destroyer.py \
  --debug \
  --dry-run \
  --k8s-api http://127.0.0.1:9000
```

For an in-cluster deployment, omit `--k8s-api`. Destroyer then uses the Kubernetes service-account token and CA mounted by Kubernetes.

## Deploy with Helm

The Helm chart and matching container image are published together for each
`vX.Y.Z` release tag. Install the first release with safe defaults (pod deletion
is disabled):

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace
```

After the first release, set both the `destroyer` image and `charts/destroyer`
GitHub Container Registry packages to public for unauthenticated installs and
image pulls.

For a Rancher dev cluster before the first release, build and push the image to
a registry the cluster can pull from, then copy and edit the dev values file:

```bash
docker login registry.example.com
docker buildx build --platform linux/amd64 \
  --tag registry.example.com/team/destroyer:dev \
  --push .

cp values-dev.example.yaml values-rancher-dev.local.yaml
# Set image.repository and image.tag for the pushed image.
kubectl create namespace destroyer-test --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install destroyer ./charts/destroyer \
  --namespace destroyer-system \
  --create-namespace \
  -f values-rancher-dev.local.yaml
```

The sample keeps dry-run enabled and only approves `destroyer-test`. Enabling
redeploy still creates cluster-scoped pod `get`/`delete` RBAC; use an isolated
dev cluster and keep the allowlist narrow.

To evaluate pod selection in dry-run mode, explicitly enable redeploy and set
the namespace allowlist:

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace \
  --set redeploy.enabled=true \
  --set 'redeploy.allowedNamespaces[0]=app-dev'
```

The chart rejects an enabled redeploy policy without an allowlist. Configure
NeuVector to send JSON webhooks to
`http://destroyer.destroyer-system.svc.cluster.local:8080/`. The Service is
internal; configure `WEBHOOK_TOKEN` through `webhook.existingSecret` or restrict
access with a NetworkPolicy. After validating dry-run decisions and
notifications, set `redeploy.dryRun=false` to allow pod deletion.

## Deploy from a generated manifest

The manifest generator embeds the application in a standard Python image, so a
registry is not required for this deployment option.

1. Create a namespace:

   ```bash
   kubectl create namespace destroyer-system
   ```

2. Generate the manifest:

   ```bash
   python generate_deployment.py \
     --namespace destroyer-system \
     --enable-redeploy \
     --allowed-namespaces approved-a,approved-b \
     --output destroyer-deployment.yaml
   ```

   This opts into dry-run redeploy for only the listed namespaces. Omit
   `--enable-redeploy` and `--allowed-namespaces` to generate a deployment with
   no pod-deletion RBAC.

3. Review the generated environment variables. Keep `REDEPLOY_DRY_RUN=true`
   until the webhook and namespace policy are verified. The generated
   deployment reads optional webhook URLs and `WEBHOOK_TOKEN` from the
   `destroyer-webhooks` Secret; provision it through your secret manager rather
   than committing credentials.

4. Apply the manifest:

   ```bash
   kubectl apply -f destroyer-deployment.yaml
   kubectl rollout status deployment/destroyer -n destroyer-system
   ```

5. Configure the NeuVector webhook with:

   ```text
   Name: Destroyer
   URL: http://destroyer.destroyer-system.svc.cluster.local:8080/
   Type: JSON
   ```

6. Verify the deployment:

   ```bash
   kubectl get pods -n destroyer-system
   kubectl logs -n destroyer-system deployment/destroyer
   kubectl get --raw \
     "/api/v1/namespaces/destroyer-system/services/destroyer:8080/proxy/healthz"
   ```

7. After confirming dry-run decisions and notifications, set `REDEPLOY_DRY_RUN=false` and apply the manifest again.

When generated with `--enable-redeploy`, the deployment gets `get` and `delete` access to pods; otherwise it has no pod-deletion RBAC. It runs as a non-root user with a read-only filesystem.

For managed clusters, keep the reviewed manifest in the organization's normal CI/CD or GitOps repository. Manage environment-specific values and notification credentials through the deployment system's secret/configuration facilities.

### Build an image for Helm or a custom deployment

Build and publish the included image to a registry available to the cluster:

```bash
docker build -t <registry>/<repository>/destroyer:latest .
docker push <registry>/<repository>/destroyer:latest
```

For Helm, set `image.repository` and `image.tag` to the image you pushed. The
self-contained generated-manifest option embeds the Python source and uses a
standard Python image instead; it does not use this built image.

## NeuVector setup

Configure NeuVector to send security events to the Destroyer service. The exact console labels vary by NeuVector version, but the webhook should be a JSON security-event webhook pointing to the in-cluster URL above.

For policy rules, create groups that match the workload namespaces approved for deletion and attach response rules appropriate to your environment. Common examples include:

| Event | Action |
| --- | --- |
| `Process.Profile.Violation` | `webhook` |
| `Container.Tunnel.Detected` | `webhook`, optionally `quarantine` |
| `Container.Privilege.Escalation` | `webhook`, optionally `quarantine` |

Start with the least disruptive rule set, validate the generated alerts, and add rules only after confirming that legitimate workload behavior is not being treated as a compromise.

## Namespace policy

`REDEPLOY_ALLOWED_NAMESPACES` is the positive approval list. When it is set, a pod is deleted only if its namespace appears in that list. This is the preferred production setting.

`REDEPLOY_EXCLUDED_NAMESPACES` is a deny list that always wins. It defaults to:

```text
kube-system,kube-public,kube-node-lease,neuvector
```

Example policy for two approved application namespaces:

```text
REDEPLOY_ALLOWED_NAMESPACES=approved-a,approved-b
REDEPLOY_EXCLUDED_NAMESPACES=kube-system,kube-public,kube-node-lease,neuvector
```

Keep system, security-controller, and other high-impact namespaces excluded even when an allowlist is used. Expand the allowlist only after testing the workload's startup and normal runtime behavior.

## Configuration

Environment variables are read when the process starts.

| Variable | Default | Description |
| --- | --- | --- |
| `ENABLE_POD_REDEPLOY` | `false` | Master switch for pod deletion. |
| `REDEPLOY_DRY_RUN` | `true` | Reports what would be deleted without deleting pods. |
| `REDEPLOY_ALLOWED_NAMESPACES` | unset | Optional comma-separated positive approval list. |
| `REDEPLOY_EXCLUDED_NAMESPACES` | `kube-system,kube-public,kube-node-lease,neuvector` | Comma-separated namespaces that are never deleted. |
| `REDEPLOY_MIN_AGE_SECONDS` | `15` | Minimum pod age before deletion; helps prevent restart loops. |
| `K8S_TIMEOUT_SECONDS` | `5` | Kubernetes API request timeout. |
| `MAX_BODY_BYTES` | `1048576` | Maximum accepted webhook body size. |
| `WEBHOOK_TOKEN` | unset | Optional bearer token required on POST requests. |
| `TEAMS_WEBHOOK_URL` | unset | Optional Microsoft Teams Adaptive Card webhook. |
| `ALERT_WEBHOOK_URL` | unset | Optional generic alerting webhook. |
| `DEBUG` | `false` | Enables verbose service logging. |
| `KUBERNETES_API_URL` | unset | Kubernetes API URL for local testing. |
| `KUBERNETES_BEARER_TOKEN` | unset | Optional bearer token for a local Kubernetes API. |
| `KUBERNETES_INSECURE_SKIP_VERIFY` | `false` | Disables TLS verification for local testing only. |

If `WEBHOOK_TOKEN` is set, clients must send `Authorization: Bearer <token>` or an equivalent `Authorization` header.

### Minimum pod age

Tune `REDEPLOY_MIN_AGE_SECONDS` per workload. A low value can be appropriate for workloads that never execute shell commands during startup, but every new namespace or value should be validated in dry-run mode first. Increase it if alerts cause restart loops.

### Notifications

When configured, `TEAMS_WEBHOOK_URL` receives an Adaptive Card containing event details and the redeploy decision. `ALERT_WEBHOOK_URL` receives this JSON shape:

```json
{
  "event": {"workload_domain": "default", "workload_name": "example-pod"},
  "redeploy_status": "SKIPPED: namespace not approved (default)"
}
```

Notification failures are only logged when `DEBUG=true` and do not prevent the
webhook request from being acknowledged. Treat notification URLs as credentials
and store them in Kubernetes Secrets.

## Webhook contract

The request body must be a JSON object. Destroyer accepts these common field names:

- Namespace: `workload_domain`, `namespace`, or `ns`
- Pod: `workload_name`, `pod`, or `pod_name`
- Container: `workload_service` or `container_name` (optional)
- Timestamp: `reported_timestamp`, `timestamp`, or `time` (optional)

Example:

```json
{
  "workload_domain": "approved-a",
  "workload_name": "example-pod",
  "container_name": "app",
  "reported_timestamp": 1737361220
}
```

`GET /` and `GET /healthz` return a health response. Send events with `POST /`.

## Permissions and security

By default, the generated manifest has no pod-deletion RBAC. With redeploy enabled, Helm and generated-manifest installs grant `get` and `delete` on pods cluster-wide; Destroyer enforces the configured namespace allowlist in the application. For a tighter Kubernetes permission boundary, replace the `ClusterRole` and `ClusterRoleBinding` with namespace-scoped `Role` and `RoleBinding` resources for each approved namespace.

Keep the service internal to the cluster. A ClusterIP Service keeps it off the
public internet, but other workloads in the cluster can still reach it unless
you restrict access with a NetworkPolicy or `WEBHOOK_TOKEN`. Do not expose the
webhook directly to the public internet.

## Testing

The integration test starts a mock Kubernetes API, runs Destroyer against it, and exercises deletion, namespace policy, dry-run, minimum-age, missing-pod, and container-selection behavior:

```bash
python test_destroyer_integration.py
```

The default ports are `9000` for the mock Kubernetes API and `8080` for Destroyer. If either port is already in use, choose free ports:

```bash
MOCK_K8S_PORT=19000 DESTROYER_PORT=18080 python test_destroyer_integration.py
```

## License

This project is released into the public domain under the [Unlicense](LICENSE).
