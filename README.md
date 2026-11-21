# GitOps agent control plane

A Python/FastAPI agent-based control plane that adds release intelligence control layer to
existing GitLab CI, Harbor and Argo CD workflows. It uses PostgreSQL to retain
events and evidence, deterministic checks to identify issues, and a
language model to help explain results.

**This is a staging prototype, not a production-ready release orchestrator or
safety gate.** Validate the example contracts, integrations and security
controls against your own systems before relying on them.

[Architecture](#solution-architecture) |
[Capabilities](#current-application-capabilities) |
[Run locally](#run-locally) |
[Configuration](#configuration) |
[Features](#features)

## Solution architecture

![Solution architecture for the GitOps agent control plane](./docs/solution-architecture.svg)

The control plane wraps existing GitLab CI, Harbor and Argo CD processes; it
does not replace them or write directly to workload clusters.

| Component | Responsibility and boundary |
| --- | --- |
| API, poller and event worker | The API authenticates and persists incoming webhooks; the optional poller observes GitLab and enqueues events directly. The worker processes the inbox, records audit results and checkpoints outcomes with LangGraph. Use polling or GitLab webhooks, not both. |
| Routing agent | The event worker calls routing checks for MR events; findings are advisory, not GitLab merge gates. |
| Reconciliation agent | The event worker compares deployment CSV observations with Argo CD application state. |
| Preflight agent | A separate Helm-rendering CLI for an independently isolated CI runner; not wired into the supervisor. |
| Promotion agent | A policy helper, not an automated rollout and not wired into the supervisor. |
| Query agent and traceability | The opt-in query endpoint combines available index, GitLab and Argo CD evidence; the index verifies Harbor references at ingestion. **Azure OpenAI is required for queries**: the API sends only filtered generic keywords for intent classification, not the raw question or repository data. |
| Bundles and rollback | Optional approved bundles support drift comparison. Rollback requires a separately authorized approval and enabled writes, and stages only a draft GitLab MR; it does not merge or deploy. |
| Storage and integrations | PostgreSQL holds the inbox, ledger and checkpoints. Direct clients are the default; optional MCP servers expose read-only tools. Key Vault CSI supplies worker and poller secrets, while API credentials must be provisioned separately. |

## Current Application Capabilities

### End-to-end workflow

1. **Observe and queue.** GitLab webhooks are authenticated by the API, stored
   in the PostgreSQL inbox and acknowledged with HTTP 202. Alternatively, the
   GitLab poller reads merge requests and `REL/*` and `HOT/*` tags and enqueues
   observations directly. Optional job polling covers configured successful
   build and deployment jobs; polling starts from a baseline and can miss
   intermediate changes. Use one GitLab ingestion mode, not both.
2. **Check changes.** The worker reads queued events and calls the routing agent
   to check merge-request diffs against configured path and branch rules.
   Findings are advisory, not GitLab merge gates.
3. **Compare deployments.** For successful `deploy_release_*` jobs, the
   reconciliation agent reads a deployment CSV and compares its chart versions
   with Argo CD sync and health. It reports mismatches; it does not repair or
   deploy workloads.
4. **Record and explain.** The worker records results in the audit ledger and
   checkpoints outcomes with LangGraph. Failures are recorded for operator
   investigation and retry. When enabled, model advisories explain predefined
   finding categories; deterministic code, not the model, decides the result.
5. **Connect evidence.** Optional traceability indexes validated CI manifests,
   GitLab commits and Harbor chart digests, and can record deployment
   observations. Coverage depends on configuration and available artifacts:
   this is not a complete or independently verified deployment history.
6. **Investigate and act.** An authorized user can ask read-only questions in
   the console or API, inspect evidence and limitations, and, when configured,
   review bundles or propose a rollback. A rollback requires its own approval
   and can only create a draft GitLab merge request; it never merges or deploys.
   GitLab writes are disabled by default; enabling them can also allow
   automated MR comments, which do not require a separate per-comment approval.

### What this adds to a conventional GitOps workflow

GitLab, Harbor and Argo CD remain the systems of record and deployment
mechanisms. This control plane can reduce manual investigation by bringing
rule findings, build provenance and deployment observations together, making
inconsistencies easier to find and explaining the evidence behind an answer.
The inbox, audit records and checkpoints provide a place to investigate
processing outcomes. Optional bundle, drift and draft rollback workflows give
operators structured review points rather than silently changing the cluster.
These are aids to human diagnosis, not automatic fixes or replacements for
protected branches, CI checks, monitoring and independent validation.

## Run locally

Requires Python 3.11+ and PostgreSQL. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[test]"
$env:GITOPS_DATABASE_URL = "postgresql+psycopg://user:password@localhost/gitops"
$env:GITOPS_GITLAB_WEBHOOK_SECRET = "<set-locally>"
.\.venv\Scripts\gitops-migrate.exe
# Start this in a separate terminal with the same environment:
.\.venv\Scripts\gitops-worker.exe
# Start the API in another terminal:
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use environment variables or a protected `.env` file; never commit credentials.
Run `.\.venv\Scripts\python.exe -m pytest -q` for the local test suite.
To use polling instead of GitLab webhooks, configure `GITOPS_GITLAB_URL`,
`GITOPS_GITLAB_TOKEN` or `GITOPS_GITLAB_TOKEN_FILE`, and the exact
`GITOPS_GITLAB_POLL_PROJECT`; set `GITOPS_GITLAB_WEBHOOK_ENABLED=false` and
start `.\.venv\Scripts\gitops-gitlab-poller.exe` in another terminal. Its first
snapshot establishes a baseline rather than replaying existing MRs and tags.


## Configuration

All settings use the `GITOPS_` prefix. See `config/settings.py` for defaults.
Important variables:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL, for example `postgresql+psycopg://...` |
| `GITLAB_URL`, `GITLAB_TOKEN` | HTTPS GitLab URL and bot token |
| `GITLAB_TOKEN_FILE` | Optional mounted GitLab token file; overrides `GITLAB_TOKEN` |
| `GITLAB_WEBHOOK_SECRET` | GitLab webhook secret |
| `GITLAB_WEBHOOK_ENABLED` | Accept GitLab webhooks; default `true` |
| `GITLAB_POLL_PROJECT` | Exact GitLab project path, e.g. `group/project` |
| `GITLAB_POLL_SECONDS` | Minimum time between MR/tag poll starts; default `60` |
| `GITLAB_BUILD_POLL_ENABLED` | Poll configured successful build/deploy jobs; default `false`, inactive until traceability is enabled |
| `GITLAB_BUILD_POLL_SECONDS` | Build/deploy job polling interval; default `60`, minimum `30` |
| `TRACEABILITY_ENABLED` | Enable provenance indexing and deployment observations; default `false` |
| `TRACEABILITY_BUILD_JOBS` | Comma-separated exact successful CI build job names |
| `TRACEABILITY_DEPLOY_JOBS` | Optional exact deployment job names, each beginning `deploy_release_` |
| `TRACEABILITY_MANIFEST_PATH` | CI job artifact path; default `gitops-provenance.json` |
| `TRACEABILITY_HARBOR_PROJECT`, `TRACEABILITY_HARBOR_REPOSITORY` | Exact authorized chart registry coordinates |
| `TRACEABILITY_JIRA_PREFIXES` | Comma-separated Jira key prefixes, e.g. `GVH` |
| `TRACEABILITY_PATCH_DIRECTORY` | Repository directory containing patch files; default `chart-patch-files` |
| `ARGOCD_URL`, `ARGOCD_TOKEN`, `ARGOCD_CA_CERT_FILE`, `ARGOCD_WEBHOOK_SECRET` | Read-only Argo CD API, optional trusted CA PEM path, and separate notification secret |
| `DEPLOY_CSV_PATH` | Artifact path within a GitLab job, default `deploy_output.csv` |
| `HARBOR_URL`, `HARBOR_USERNAME`, `HARBOR_PASSWORD` | Harbor read-only integration credentials |
| `TEAMS_GATEWAY_SECRET` | Secret shared only with the trusted Teams gateway |
| `TEAMS_GATEWAY_URL` | HTTPS gateway notification endpoint for the notifier primitive |
| `TEAMS_APPROVERS` | Comma-separated immutable gateway identity allowlist |
| `STATUS_SECRET` | Separate secret for reading event status |
| `QUERY_ENABLED` | Enable read-only queries; default `false` |
| `QUERY_SECRET` | Dedicated secret for the query endpoint; do not reuse other service credentials |
| `QUERY_APPLICATIONS` | Comma-separated exact Argo CD application allowlist |
| `QUERY_PROJECTS` | Comma-separated exact GitLab project path allowlist |
| `BUNDLE_ENABLED` | Enable release-bundle endpoints; default `false` |
| `BUNDLE_GATEWAY_SECRET` | Dedicated secret shared only with the trusted identity gateway |
| `BUNDLE_CONFIG_PROJECTS` | Exact comma-separated GitLab configuration project allowlist |
| `BUNDLE_IMAGE_REPOSITORIES` | Exact comma-separated Harbor image repositories, `project/repository` without registry hostname |
| `ROLLBACK_ENABLED` | Enable rollback proposal, approval and execution endpoints; default `false` |
| `ROLLBACK_GATEWAY_SECRET` | Dedicated secret shared only with the trusted identity gateway |
| `ROLLBACK_TARGETS` | JSON mapping application names to exact target branches and file allowlists |
| `WRITE_ENABLED` | Explicit opt-in for GitLab write methods; default `false` |
| `CONFIG_DIR` | Directory containing the two YAML policy files |
| `WORKER_POLL_SECONDS` | Delay when no work is available; default `2` |
| `MCP_MODE` | Worker read transport: `direct` (default) or `mcp` |
| `MCP_BASE_URL` | HTTPS base URL of the trusted MCP proxy |
| `MCP_BEARER_TOKEN` | Shared secret for MCP clients and server |
| `MCP_ALLOWED_HOSTS` | Comma-separated MCP Host allowlist; defaults to loopback hosts |
| `MCP_ALLOWED_ORIGINS` | Optional comma-separated browser Origin allowlist; default empty |
| `MCP_ALLOW_INSECURE_LOOPBACK` | Allow HTTP only for local loopback development; default `false` |
| `LLM_ENABLED` | Opt into model advisories; default `false` |
| `LLM_PROVIDER` | `azure_openai` (default) or `self_hosted` |
| `LLM_MODEL` | Azure deployment name or self-hosted model identifier |
| `LLM_AZURE_ENDPOINT` | Azure OpenAI or Foundry resource root URL |
| `LLM_AZURE_AUTH` | `managed_identity` (default), `workload_identity`, or `api_key` |
| `LLM_AZURE_CLIENT_ID` | Optional user-assigned managed identity client ID |
| `LLM_AZURE_API_KEY` | Azure API key when using API-key authentication |
| `LLM_SELF_HOSTED_BASE_URL` | OpenAI-compatible API base URL, normally ending in `/v1` |
| `LLM_SELF_HOSTED_API_KEY` | API key for the self-hosted endpoint |
| `LLM_ALLOW_INSECURE_LOOPBACK` | Allow self-hosted HTTP only on local loopback; default `false` |
| `LLM_TIMEOUT_SECONDS` | Model request timeout; default `20`, maximum `60` |
| `UI_ENABLED`, `UI_PUBLIC_ORIGIN` | Enable the console and set its exact HTTPS public origin |
| `UI_ENTRA_TENANT_ID`, `UI_ENTRA_CLIENT_ID` | Microsoft Entra tenant and application IDs |
| `UI_ENTRA_CLIENT_SECRET`, `UI_ENTRA_CLIENT_SECRET_FILE` | Console application's Entra client secret or mounted secret file |
| `UI_ALLOWED_USERS` | Comma-separated Entra object IDs authorized to sign in |

For example, `DATABASE_URL` above is supplied as `GITOPS_DATABASE_URL`.
For Helm deployments, `uiAllowedUsersSecret` can name an externally managed
Kubernetes Secret in the release namespace containing the key
`GITOPS_UI_ALLOWED_USERS`. The staging overlay uses `gitops-console-allowlist`;
create it before deploying and restrict access to it. Do not also set that
variable in Helm `env`. Changing the Secret requires an API pod restart to
pick up the new value. The identifiers and credentials do not belong in Git.
GitLab and Argo CD API URLs must use HTTPS. Configure least-privilege GitLab
and Argo CD credentials and restrict the ingress to the intended sources.
Routing and rollout YAML are illustrative; adapt both and rebuild the image.

## Features

### Traceability index and build provenance

**Why:** Help operators investigate which build and candidate fixes are
associated with a chart, instead of manually searching separate CI and
registry records.

**How it works:** A worker validates successful build jobs and their
provenance manifests against GitLab and Harbor, then indexes the claims and
evidence. Optional deployment observations connect CSV claims to Argo CD
status. These are evidence-based observations, not proof of what is running.

**Enable:** Disabled by default. Run `gitops-migrate` on the application
database and configure the same traceability settings on the
worker, poller, and query API. Set `GITOPS_TRACEABILITY_ENABLED=true`, the
existing `GITOPS_GITLAB_POLL_PROJECT` to the exact chart project,
`GITOPS_TRACEABILITY_BUILD_JOBS` to exact build job names,
`GITOPS_TRACEABILITY_JIRA_PREFIXES` to the allowed prefixes, and
`GITOPS_TRACEABILITY_HARBOR_PROJECT` and
`GITOPS_TRACEABILITY_HARBOR_REPOSITORY` to authorized chart coordinates. The
worker needs read-only GitLab API/repository/artifact and Harbor access; the
API needs GitLab API access, database access and, for application queries,
read-only Argo CD.
The Harbor credentials belong in the externally managed secret, not Helm values.
Indexing always uses the direct GitLab and Harbor clients, even in MCP mode.
Configuration currently supports one GitLab chart project and one Harbor
project/repository per deployment.

The configured successful build job must publish the following JSON as a GitLab
job artifact at `GITOPS_TRACEABILITY_MANIFEST_PATH`. The example values are
illustrative, **not** a real deployment or evidence:

```json
{
  "schema_version": 1,
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "chart": {
    "name": "example",
    "version": "260801.5",
    "harbor_project": "charts",
    "harbor_repository": "repo/chart",
    "tag": "260801.5",
    "digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  },
  "fixes": [
    {
      "commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "patch_paths": ["chart-patch-files/GVH1797/patch.yaml"]
    }
  ]
}
```

Create this artifact in **your chart pipeline**; this project does not modify
that pipeline. Manifests are limited to 64 KiB, 50 fix commits, 20 patch paths
per fix and 50 paths overall. Each fix commit must be an ancestor of the build
commit, listed files must exist at that commit, and the Harbor tag must resolve
to the claimed digest at indexing time. Missing or conflicting evidence fails
the event. Manifest claims and ancestry do not prove a fix was packaged or
effective, and the index is not a signed attestation.

To observe jobs by polling, set `GITOPS_GITLAB_BUILD_POLL_ENABLED=true`.
Only the latest 100 successful jobs are considered, the first snapshot skips
existing jobs, and the interval must be at least 30 seconds. To enqueue known
historical build IDs, run `python -m app.traceability --job-id 42 --job-id 43`
with the same database and configuration as the worker. The worker must be
running; backfill does not discover jobs or recreate missing artifacts.
Deployment CSVs contain no digest, so they cannot prove which indexed package
was deployed. Candidate Jira and release-origin answers can be incomplete;
deployment origin remains unverified without stronger evidence.

### Natural-language queries

![Read-only GitOps query assistant in the console](./docs/query-console.png)

**Why:** Give operators a single place to investigate chart versions, branch
changes, candidate Jira fixes and release origins without manually collecting
every source link.

**How it works:** The API extracts allowlisted generic keywords from the
question and sends only those keywords to Azure OpenAI to classify intent.
Deterministic code then reads the relevant authorized GitLab, Argo CD or
traceability data and returns a result with an observation time, evidence
links and limitations. The model does not receive the raw question or execute
tools. For example, ask `Compare branches between main and release/VN1`.
Unsupported or ambiguous wording is rejected.

**Enable:** Queries are disabled by default. Configure:

1. `GITOPS_QUERY_ENABLED=true`, a dedicated `GITOPS_QUERY_SECRET`, and both
   nonempty allowlists `GITOPS_QUERY_APPLICATIONS` and `GITOPS_QUERY_PROJECTS`.
   Keep the query secret distinct from other service credentials.
2. `GITOPS_LLM_PROVIDER=azure_openai`, `GITOPS_LLM_MODEL`,
   `GITOPS_LLM_AZURE_ENDPOINT`, and the chosen `GITOPS_LLM_AZURE_AUTH`.
   The API identity needs access to the model. `GITOPS_LLM_ENABLED` controls
   worker advisories separately and is not required for queries.
3. HTTPS GitLab and Argo CD URLs and the API process's least-privilege
   read credentials; mounting a token for the worker does not make it available
   to the API. Jira and release-origin questions additionally need the
   configured traceability index and database.
4. For the browser, enable the console with `GITOPS_UI_ENABLED=true`, an exact
   HTTPS `GITOPS_UI_PUBLIC_ORIGIN`, Entra tenant and client IDs, a protected
   client secret, and `GITOPS_UI_ALLOWED_USERS`; run database migrations first.
   The console authenticates users and enforces origin and CSRF checks. An
   external caller can instead use `POST /queries` with `X-Query-Secret`.

**Limitations:** Chart-version resolution requires a single-source Argo CD
application with a reconciliation no more than five minutes old, `Synced`
status, and matching latest deployment-history revision and source. Missing,
stale or conflicting evidence
does not yield a version. GitLab comparisons provide changed paths and bounded
textual version hints, not a semantic diff; GitLab may omit changes without
signalling truncation. Argo CD status is not independent proof of the live
workload. Protect query results, evidence links and logs.

### Approved release bundles and drift

**Why:** Preserve a reviewable snapshot of intended chart, configuration and
image evidence, then compare it with later observations.

**How it works:** An authorized user creates a draft from an indexed build
whose provenance uses schema version 2 and includes its environments,
configuration commits and image digests. The service validates GitLab and
Harbor evidence and fingerprints the snapshot. Approval rechecks the
fingerprint and source evidence; it does not deploy. A separate drift check
compares an approved bundle with Argo CD observations and reports `match`,
`drift` or `unknown`.

**Enable:** Run migrations and configure traceability and queries first. Set
`GITOPS_BUNDLE_ENABLED=true`, a distinct `GITOPS_BUNDLE_GATEWAY_SECRET`,
`GITOPS_TEAMS_APPROVERS`, `GITOPS_BUNDLE_CONFIG_PROJECTS`, and
`GITOPS_BUNDLE_IMAGE_REPOSITORIES`. The query project allowlist must include
the chart and configuration projects, and the application allowlist must
include each environment. Give the API read-only database, GitLab, Harbor and
Argo CD access. In the CI artifact above, use `"schema_version": 2` and add an
`environments` array containing each `application`, `config_project`,
full-commit `config_revision`, and `images` with `repository` and SHA-256
`digest`. Version 1 artifacts can be indexed but cannot create a bundle.
For example, merge this field into the existing artifact:

```json
{
  "environments": [{
    "application": "viseca-prod",
    "config_project": "team/config",
    "config_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "images": [{
      "repository": "team/backend",
      "digest": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    }]
  }]
}
```

Version 2 supports 1-20 unique applications, 1-50 images per environment and
at most 100 images overall. The CI pipeline must supply the inventory.

Use the authenticated console, or a separately implemented trusted
identity-verifying gateway for bundle creation and approval. The gateway must
derive the actor itself, authorize it against the approver allowlist, and
protect its secret; possession of that secret can impersonate an approver.
There is no enforced two-person rule. Gateway callers create a draft with
`POST /release-bundles` and approve it with
`POST /release-bundles/<fingerprint>/approval`; authenticated query callers
can read it or request `/release-bundles/<fingerprint>/drift/<application>`.

**Limitations:** Drift uses Argo CD's observations, not live pod image IDs.
The deployed chart digest cannot be independently observed and is always
`unknown`, so overall drift cannot be `match`. CI claims and bundle approval
are not signed attestations or proof that the declared artifacts were deployed.

### Rollback merge requests

**Why:** Make recovery reviewable by proposing a limited Git change from a
previously approved bundle, rather than automatically changing a workload.

**How it works:** An authorized operator selects a prior approved bundle and
creates a plan. The service previews only configured files that differ,
fingerprints the plan, and requires explicit approval of that fingerprint.
After rechecking the bundle, registry digests, branch head and file hashes,
execution creates a branch, commit and **draft** GitLab merge request. It does
not merge or deploy. Approval of a bundle alone is not rollback approval.

**Enable:** Disabled by default and not validated against a real GitLab
instance. Configure the bundle prerequisites, migration `0005`, a distinct
`GITOPS_ROLLBACK_GATEWAY_SECRET`, and `GITOPS_ROLLBACK_ENABLED=true`. Use the
authenticated console or an independently implemented trusted gateway that
authenticates and authorizes users, supplies the actor itself and protects
the gateway secret. Actors must be in `GITOPS_TEAMS_APPROVERS`; there is no
enforced two-person rule. Set `GITOPS_ROLLBACK_TARGETS` to an explicit
per-application mapping, for example:

```json
{"viseca-prod":{"branch":"main","files":["env/values.yaml","Chart.yaml"]}}
```

Execution also requires `GITOPS_WRITE_ENABLED=true`, which enables other GitLab
write features, including MR comments. Restrict the bot to intended projects
and `agent-rollback/*` branches. The operator must choose the baseline bundle;
each application can restore only its own configuration project and at most
20 literal paths. Files must exist at both commits, be UTF-8 and at most 64 KiB.

**Limitations:** Restoring whole files can revert unrelated changes or security
fixes. Review the diff, use protected branches and enforce independent merge
checks; the target may change after validation. A GitLab pipeline may run when
the branch or MR is created. If execution times out or crashes, its status may
be `uncertain` or `executing` even if GitLab objects were created; inspect
GitLab and the audit ledger before taking further action. A draft MR and bundle
approval do not establish that the restored configuration is safe.

### Deployment reconciliation

**Why:** Highlight differences between a successful deployment job's declared
chart versions and Argo CD's observed sync and health.

**How it works:** The worker reads the job's CSV artifact. It considers an
application healthy only when `spec.source.targetRevision` and
`status.sync.revision` both match the CSV version, sync is `Synced`, and
health is `Healthy`. This is not independent proof of the live workload.

**Enable:** Ingest successful `deploy_release_*` job events, configure the
GitLab and Argo CD read credentials, and publish the CSV artifact at
`GITOPS_DEPLOY_CSV_PATH` (default `deploy_output.csv`). It requires headers
`application,chart_version,repository`. For job polling, also configure
traceability, exact `GITOPS_TRACEABILITY_DEPLOY_JOBS` names and
`GITOPS_GITLAB_BUILD_POLL_ENABLED=true`. Adapt the contract for Git-backed
or multi-source applications.

### MCP servers

**Why:** Offer a consistent, optional read-only tool interface to GitLab,
Harbor and Argo CD without giving the model deployment authority.

**How it works:** The worker can use a separate MCP server instead of direct
clients for supported reads. Tool errors fail the event; there is no silent
fallback. Traceability indexing still uses direct clients.

**Enable:** Direct clients are the default. Configure a protected MCP server
and set `GITOPS_MCP_MODE=mcp`, `GITOPS_MCP_BASE_URL`, and a separate
`GITOPS_MCP_BEARER_TOKEN` for the worker. For the Helm deployment, see the
chart settings below.

The `gitops-mcp` process serves three independent MCP Streamable HTTP endpoints:

| Endpoint | Registered read tools |
| --- | --- |
| `/gitlab/mcp` | `gitlab_mr_changes`, `gitlab_job_artifact` |
| `/harbor/mcp` | `harbor_artifact` |
| `/argocd/mcp` | `argocd_application` |

Tool responses use MCP structured content. The artifact tool returns base64
and limits the downloaded artifact to 8 MiB. An upstream error is returned as
an MCP tool error; the worker marks the event failed rather than falling back
to direct clients. The server requires `Authorization: Bearer <token>` on
every MCP request. It also checks allowed Host and Origin headers; empty
tokens and empty Host allowlists fail closed. `/health/live` is unauthenticated.
The shared token is not an MCP OAuth authorization server: external clients
must be configured to send the bearer token themselves.

The MCP application listens on plaintext HTTP on port 8001 and **does not
provide TLS**. In Kubernetes, put it behind a trusted TLS-terminating proxy or
service mesh with an encrypted, authenticated connection to the backend;
restrict access to the backend with network policy. Set `MCP_ALLOWED_HOSTS`
to the Host header forwarded by that proxy, including the port when present.
The worker's `MCP_BASE_URL` must be HTTPS with certificate validation enabled.
Use separate, read-only GitLab, Harbor and Argo CD credentials in the server
secret. Direct-mode workers and the existing GitLab comment outbox still use
their own integration credentials.

For local development only, set `GITOPS_MCP_BEARER_TOKEN` and the upstream
credentials in both relevant processes, start `gitops-mcp`, and set the worker's
`GITOPS_MCP_MODE=mcp`, `GITOPS_MCP_BASE_URL=http://127.0.0.1:8001`, and
`GITOPS_MCP_ALLOW_INSECURE_LOOPBACK=true`. The token values must match. Never
use the loopback HTTP exception for remote traffic.

The Helm chart keeps the MCP deployment disabled by default. To enable it,
set `mcp.enabled=true`, `mcp.existingSecret` to a separately managed Secret
containing the token and upstream read credentials, and `mcp.allowedHosts`
to the forwarded Host allowlist. Configure TLS proxying separately, then set
the worker's `GITOPS_MCP_MODE` and `GITOPS_MCP_BASE_URL` through chart `env`
and its bearer token through the worker's existing Secret. The chart's MCP
Service is plaintext and must not be exposed directly as a secure endpoint.

### Model advisories

**Why:** Help operators understand routing and reconciliation findings without
letting an AI model decide policy or take action.

**How it works:** The worker can request JSON advisories after it has evaluated
an MR or reconciled a deployment. They appear in the event result as
`advisory.status=generated` with a summary and recommendations. If the
provider or output validation fails, the deterministic result remains intact
and the event result contains `advisory.status=failed`; the worker logs the
error type. There is no model-driven tool calling, automatic posting, promotion,
or rollback. The model is not used when no routing findings exist.

The only data sent to either provider is a fixed system instruction and an
allowlisted task and finding category. The adapter does **not** send paths,
diffs, source code, job IDs, project IDs, tenant names, chart versions,
credentials, or raw errors.

**Enable:** Model advisories are disabled by default. To use Azure OpenAI,
configure the worker with `GITOPS_LLM_ENABLED=true`, `GITOPS_LLM_PROVIDER=azure_openai`,
`GITOPS_LLM_AZURE_ENDPOINT=https://<resource>.openai.azure.com`, and
`GITOPS_LLM_MODEL=<deployment-name>`. The deployed model must support Chat
Completions JSON mode and `max_completion_tokens`. Only HTTPS Azure OpenAI or
Foundry resource hostnames are accepted; the adapter appends `/openai/v1/`.

- For Azure-managed hosts, use `GITOPS_LLM_AZURE_AUTH=managed_identity` and
  optionally `GITOPS_LLM_AZURE_CLIENT_ID` for a user-assigned identity.
- For AKS workload identity, use `GITOPS_LLM_AZURE_AUTH=workload_identity`.
  Configure an AKS OIDC issuer, the federated identity credential and Azure
  RBAC, then set chart `azureWorkloadIdentity.enabled=true` and
  `azureWorkloadIdentity.clientId`. The worker is labeled for the AKS
  workload-identity webhook; Kubernetes API token automount remains disabled.
- For API-key authentication, set `GITOPS_LLM_AZURE_AUTH=api_key` and
  `GITOPS_LLM_AZURE_API_KEY` from the worker's externally managed Secret.
  Never place keys in Helm values or source control.

Grant the selected identity the **Cognitive Services OpenAI User** role on the
resource. Token authentication uses the `https://cognitiveservices.azure.com/.default`
audience for Azure OpenAI endpoints and `https://ai.azure.com/.default` for
Foundry endpoints. A configured Azure resource and model deployment are
prerequisites; this project does not create them. Mocked tests alone do not
verify Azure authentication or inference; validate the configured deployment.

For a future self-hosted model, select `GITOPS_LLM_PROVIDER=self_hosted`, set
`GITOPS_LLM_SELF_HOSTED_BASE_URL` to its OpenAI-compatible `/v1` endpoint,
`GITOPS_LLM_SELF_HOSTED_API_KEY` from a secret, and `GITOPS_LLM_MODEL` to its
model identifier. The server must support Chat Completions, JSON mode and
`max_completion_tokens`. HTTPS is mandatory except for explicitly enabled
loopback-only development using `GITOPS_LLM_ALLOW_INSECURE_LOOPBACK=true`.

### Preflight in your release pipeline

**Why:** Catch missing tenant ConfigMap keys before building a chart.

**How it works:** A standalone CLI renders the baseline and tenant charts,
then compares ConfigMap keys. It is not automatically invoked by the worker.

**Enable:** Install the package and Helm in a **network-isolated** CI runner,
mount the chart and values read-only, and run (paths below are examples):

```sh
gitops-preflight --chart ./chart --baseline ./values/vn_base.yaml \
  --tenant ./values/clientdemo.yaml --tenant ./values/vn_base.yaml \
  --tenant ./values/vbl.yaml --tenant ./values/payfare.yaml \
  --tenant ./values/viseca.yaml
```

Require this command to exit successfully before `build_and_push_chart`.
The isolation is a responsibility of the runner platform, not the application.
The API service does not invoke Helm or have Kubernetes API access.
