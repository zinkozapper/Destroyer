# Publish Destroyer on GitHub

This guide covers the first public release. The repository is not actively
maintained; publishing it does not imply ongoing support.

## What GitHub will build

This repository already contains two GitHub Actions workflows:

- **CI** runs on pull requests and pushes to `main`. It tests the Python app,
  builds the container as a check, and validates/packages the Helm chart. The
  CI build does not publish an image.
- **Release** runs when a version tag like `v0.1.0` is pushed. It tests the
  app, builds and publishes the container image, packages and publishes the
  Helm chart, and creates a GitHub Release with install files attached.

The container image is published to `ghcr.io/zinkozapper/destroyer`. The Helm
chart is published as an OCI package at
`oci://ghcr.io/zinkozapper/charts/destroyer`. OCI is the registry format Helm
uses here; you do not need to run a separate Helm chart server or use `helm repo add`.

## 1. Prepare the GitHub repository

The local repository already has this remote configured:

```text
git@github.com:zinkozapper/Destroyer.git
```

That remote uses SSH. If you have not added an SSH key to your GitHub account,
either set one up under **Settings → SSH and GPG keys**, or switch the remote to
HTTPS and authenticate with GitHub CLI (install `gh` first):

```bash
git remote set-url origin https://github.com/zinkozapper/Destroyer.git
gh auth login
```

Open `https://github.com/zinkozapper/Destroyer`. If the repository does not
exist yet, create a repository named `Destroyer` under `zinkozapper`. Leave it
empty—do not initialize it with a README, license, or `.gitignore`.

For an open-source release, make the repository public in **Settings → General
→ Danger Zone → Change repository visibility**.

Enable GitHub Actions in **Settings → Actions → General**. The release workflow
needs permission to create releases and publish packages. Its workflow file
requests `contents: write` and `packages: write`; set the repository's
**Workflow permissions** to **Read and write permissions** if GitHub or the
organization policy requires it. The workflow uses GitHub's built-in
`GITHUB_TOKEN`; no personal access token or added Actions secret is needed.

## 2. Make the first commit and push it

There are no commits yet. Before adding files, check that local settings and
credentials are not included:

```bash
git status --short
git status --short --ignored
```

Local Helm values such as `values-dev.yaml` and
`values-rancher-dev.local.yaml` are ignored. The tracked example
`values-dev.example.yaml` is safe to publish; do not force-add local values or
files containing credentials.

When the source is ready, stage the project files and review the staged list:

```bash
git add .dockerignore .gitignore .github charts Destroyer.py Dockerfile \
  LICENSE README.md PUBLISHING.md generate_deployment.py \
  test_destroyer_integration.py values-dev.example.yaml
git status --short
```

If the staged list is correct, create and push the initial commit:

```bash
git commit -m "Prepare Destroyer for open-source release"
git push -u origin main
```

Open the repository's **Actions** tab and wait for the **CI** workflow to
finish successfully. A green CI run does not publish packages; it only checks
that the project builds and tests.

If `git push` says the remote already has commits, stop and reconcile the
histories rather than force-pushing.

## 3. Publish the first version

After CI passes, create a version tag and push it:

```bash
git tag -a v0.1.0 -m "Destroyer 0.1.0"
git push origin v0.1.0
```

The `v` prefix is required by the workflow. Pushing the tag starts **Release**.
You do not need to build or upload anything from your computer. GitHub Actions
will:

1. Run the tests.
2. Build and push the image to GHCR with tags `0.1.0`, `0.1`, and `latest`.
3. Package and push chart version `0.1.0` to GHCR.
4. Create a GitHub Release named `v0.1.0`, attaching the chart archive and a
   generated Kubernetes manifest.

Watch the **Actions** tab until the **Release** run succeeds. The chart's
`Chart.yaml` version is set by the release workflow from the tag; for later
releases, use a new version tag such as `v0.1.1`.

## 4. Make the GHCR packages public

GitHub repository visibility and package visibility are separate. After the
first successful Release run, open the repository's **Packages** section (or
the owner's Packages page) and check both packages:

- `destroyer` — the container image
- `charts/destroyer` — the Helm chart

For anonymous installs, open each package's **Package settings**, choose
**Change visibility**, and set it to **Public**. If either package stays
private, Helm and the Rancher nodes will need credentials to pull it.

## 5. Install it on Rancher with Helm

Make sure `kubectl` is using the intended Rancher cluster and that Helm 3.8 or
newer is installed:

```bash
kubectl config current-context
helm version
```

For the first release, install the published chart with safe defaults:

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace
```

The chart defaults to the matching GHCR image. Pod deletion is disabled by
default. To test webhook decisions in dry-run mode, copy
`values-dev.example.yaml` to the ignored `values-rancher-dev.local.yaml`, set
the image repository and tag to `ghcr.io/zinkozapper/destroyer` and `0.1.0`,
then install with:

```bash
helm upgrade --install destroyer \
  oci://ghcr.io/zinkozapper/charts/destroyer \
  --version 0.1.0 \
  --namespace destroyer-system \
  --create-namespace \
  -f values-rancher-dev.local.yaml
```

The example allowlists only `destroyer-test` and keeps dry-run on. Enabling
redeploy creates a cluster-scoped role that can get and delete pods; use an
isolated test cluster and keep the namespace allowlist narrow. Do not expose
the webhook publicly.

## Later releases

Push changes to `main` and wait for CI. When ready to publish, tag the desired
commit with the next version (for example, `v0.1.1`) and push the tag. GitHub
Actions will publish the image and chart and create the release automatically.
