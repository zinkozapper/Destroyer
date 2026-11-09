# Destroyer Helm chart

This chart installs Destroyer, its internal ClusterIP Service, and service
account. Pod-deletion RBAC is added only when redeploy is enabled. The published
chart and matching container image are released together for each `vX.Y.Z` Git
tag.

## Install

Requires Helm 3.8 or newer for OCI chart support. Install with safe defaults:

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace
```

The defaults use the published image, keep the Service internal, and disable
pod deletion. To enable dry-run evaluation for an approved workload namespace:

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace \
  --set redeploy.enabled=true \
  --set 'redeploy.allowedNamespaces[0]=app-dev'
```

The chart rejects `redeploy.enabled=true` without an allowlist. After validating
the dry-run decisions, set `redeploy.dryRun=false` to allow deletion.

## Install or manage it in Rancher

Rancher v2.9 or newer supports OCI Helm chart repositories. In the target
cluster, add an OCI repository at
`oci://ghcr.io/zinkozapper/charts` under **Apps → Repositories**. Once it is
active, find **Destroyer** under **Apps → Charts** and install or upgrade it
from Rancher. Choose the `destroyer-system` namespace and a release name of
`destroyer` to use the default service URL. The Rancher form exposes the
redeploy policy, debug logging, and an optional existing webhook Secret.

Create the namespace and any webhook Secret before installing if you want to
select one in the form. The Secret must be in the release namespace. Keep
`redeploy.enabled` off until you configure the approved namespace list; when
you enable it, dry-run remains on by default. Rancher shows the Secret name,
not its contents. For private GHCR packages, configure registry credentials in
Rancher and image pull credentials for the workload.

The form is included in chart releases published after this change; the already
published `0.1.0` chart does not include it. Publish and select a newer chart
version (for example, `0.1.1`) to use the Rancher form. An existing release
installed with standalone Helm may not be managed by Rancher's Apps UI; avoid
installing a second copy with the same release name until you have planned how
to migrate or manage that release.

After the first release, make both the `destroyer` image and `charts/destroyer`
packages public in GitHub Container Registry for unauthenticated Helm installs
and image pulls. Otherwise, configure registry credentials and `imagePullSecrets`.

Configure NeuVector to send JSON webhooks to:

```bash
http://destroyer.destroyer-system.svc.cluster.local:8080/
```

For generic instructions on creating NeuVector groups and response rules, see
the [NeuVector setup guide](../../README.md#neuvector-setup).

When redeploy is enabled, the chart uses a ClusterRole and ClusterRoleBinding
with `get` and `delete` access to pods cluster-wide. Destroyer enforces
`redeploy.allowedNamespaces` in the application, so keep that allowlist
restricted to intended workload namespaces.

## Webhook Secret

Create a Secret in the release namespace and set
`webhook.existingSecret=destroyer-webhooks`. Use your secret manager for
production credentials; do not commit them in Helm values.

```bash
kubectl -n destroyer-system create secret generic destroyer-webhooks \
  --from-literal=WEBHOOK_TOKEN='replace-me' \
  --from-literal=TEAMS_WEBHOOK_URL='https://example.invalid/teams'
```

The Service is internal. Use a NetworkPolicy or `WEBHOOK_TOKEN` before allowing
other workloads to reach it.
