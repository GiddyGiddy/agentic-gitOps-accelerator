# GitOps agent control plane

A Python/FastAPI starting point for the requested event-driven supervisor and
four GitOps workers. It integrates with GitLab, Argo CD, and PostgreSQL using
configurable example contracts. **This is not a production-ready release
orchestrator.** No deployment should rely on it as a safety gate without
completing and testing the items below against your actual systems.

## What works

- GitLab, Argo CD and gateway endpoints authenticate using separate shared
  secrets. Empty secrets always reject requests.
- Authenticated requests are persisted to a PostgreSQL inbox and return HTTP
  202. A separate worker processes them and checkpoints LangGraph state.
  SQL tables persist releases, approvals and audit results.
- GitLab MR hooks fetch a diff and apply the example routing rules. Optional
  comments require `GITOPS_WRITE_ENABLED=true`.
- An optional GitLab poller observes merge requests and `REL/*` tags and
  submits changes to the same worker inbox, without requiring inbound webhooks.
- Successful GitLab deployment job hooks fetch a CSV artifact and check the
  current Argo CD Application health, sync state and chart revision.
- The preflight CLI renders tenant values and compares ConfigMap keys against a
  baseline. The operator must run it in an independently isolated CI runner.
- The GitLab, Harbor and Argo CD API clients and promotion policy helper provide
  integration building blocks. GitLab writes are disabled by default.
- Optional read-only MCP servers expose GitLab, Harbor, and Argo CD tools using
  Streamable HTTP. The worker can use MCP for its existing GitLab and Argo CD
  reads; direct clients remain the default. GitLab comment writes still go
  through the existing outbox and direct client, not MCP.
- Optional Azure OpenAI or OpenAI-compatible self-hosted model advisories
  explain routing findings and reconciliation health. They are disabled until
  configured and do not control policy, approvals, or deployment actions.
- The Helm chart uses a non-root service account with no Kubernetes permissions,
  no mounted API token, a read-only filesystem and an externally managed secret.

## Important limitations

- Checkpointed graphs record event outcomes per thread, but the multi-step
  promotion state machine, human-in-the-loop interrupt and job-to-release
  correlation are not implemented. Job and Argo CD events have individual
  threads; only release tags and approvals share a release thread. Checkpoints
  and application transactions use separate connections: the inbox and ledger
  are authoritative if a checkpoint is ahead after a crash. Never initiate an
  external action solely from checkpoint state.
- The model is advisory only and is not an autonomous supervisor. Preflight,
  promotion, rollback, and ChatOps have no model integration. Model output is
  untrusted, may be inaccurate, and requires human review.
- **No automatic promotion, job play, branch creation, corrected MR, hotfix,
  rollback, health polling, or Teams notification is wired into the supervisor.**
  An approval is recorded but does not advance a release. The API client
  methods are primitives, not authorization for automation. A signed-in Teams
  gateway, application-specific policy, transaction/outbox design and a
  tested per-release state machine are required before enabling those actions.
- The MCP servers expose no GitLab writes, Helm rendering, or Teams tools.
  Their shared bearer token is a deployment-level credential, not OAuth or
  per-user authorization. Anyone holding it can invoke all registered reads
  permitted by the upstream service credentials. Use least-privilege upstream
  accounts and restrict access to the MCP proxy and its backend.
- MR violations are advisory comments only: they do not block merges. The diff
  check uses path/name and added-line heuristics; it is not a YAML-aware
  enforcement engine. Enforce required checks separately in GitLab. An
  incomplete GitLab diff is rejected.
- Preflight is not inserted into your existing pipeline. The CLI itself does
  **not** create a sandbox or disable network; run it in an ephemeral CI
  environment with network disabled and a read-only, untrusted checkout, and
  require its success before your actual chart-build job. It compares ConfigMap
  key names only, not schemas or Kubernetes runtime behavior.
- CSV columns and Argo CD source/revision checks are examples. They must match
  your chart deployment and artifact format. Argo CD health is an Application
  health summary, not an independent pod-readiness check. No real GitLab,
  Harbor, Argo CD, Teams, Kubernetes, Helm or PostgreSQL service was exercised
  by the included unit tests.
- Delivery IDs deduplicate retries but do not prevent an authorized sender from
  inventing new IDs. Put these
  endpoints behind TLS, restrict ingress, and use an authenticated trusted
  gateway. The gateway must validate Teams identities and set `actor` to the
  immutable identity in the allowlist; never accept an end-user-supplied actor.
  Protect the gateway secret and separate it from GitLab and Argo CD secrets.
- Run the explicit migration command before starting the service; use a
  dedicated database role and protect both inbox and checkpoint contents. API
  access, audit retention, webhook limits, secret rotation, egress policy,
  monitoring, and disaster recovery require deployment-specific design.

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
| `GITLAB_POLL_SECONDS` | Minimum time between poll starts; default `60` |
| `ARGOCD_URL`, `ARGOCD_TOKEN`, `ARGOCD_WEBHOOK_SECRET` | Read-only Argo CD API and separate notification secret |
| `DEPLOY_CSV_PATH` | Artifact path within a GitLab job, default `deploy_output.csv` |
| `HARBOR_URL`, `HARBOR_USERNAME`, `HARBOR_PASSWORD` | Harbor read-only integration credentials |
| `TEAMS_GATEWAY_SECRET` | Secret shared only with the trusted Teams gateway |
| `TEAMS_GATEWAY_URL` | HTTPS gateway notification endpoint for the notifier primitive |
| `TEAMS_APPROVERS` | Comma-separated immutable gateway identity allowlist |
| `STATUS_SECRET` | Separate secret for reading event status |
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

For example, `DATABASE_URL` above is supplied as `GITOPS_DATABASE_URL`.
GitLab and Argo CD API URLs must use HTTPS. Configure least-privilege GitLab
and Argo CD credentials and restrict the ingress to the intended sources.
Routing and rollout YAML are illustrative; adapt both and rebuild the image.

## Event contracts

`POST /webhooks/gitlab` expects `X-Gitlab-Token`, `X-Gitlab-Event` and
`X-Gitlab-Event-UUID` headers. The UUID must be stable across retries.
Supported event names are `Merge Request Hook`, `Tag Push Hook` and `Job Hook`.
An MR event uses `project.id`, `object_attributes.iid` and `target_branch`.
A release tag event uses `project_id`, `checkout_sha` and
`ref=refs/tags/REL/<version>`. A successful job event uses `project_id`,
`build_id`, `build_name` and `build_status`. An example MR and CSV are in
`tests/fixtures`. GitLab must retain the CSV as a job artifact.

`POST /webhooks/argocd` requires `X-Webhook-Secret`, a retry-stable `X-Event-Id`,
and a JSON object with an `application` string; it records an audit event.

`POST /chatops/approvals` requires `X-Gateway-Secret` and JSON fields `release`,
`project_id`, `wave`, `actor` and `request_id`. The request ID must be stable
across retries. Only listed gateway identities can submit; the worker checks
the release belongs to the project and allows one approval per release/wave. This
endpoint is **not** a Teams Bot Framework endpoint; a separately implemented
authenticated gateway is mandatory.

All three endpoints return `202` after the inbox transaction commits, not
after processing. The response includes `event_id`, `status`, and `duplicate`.
Reusing a delivery ID with different content returns `422`. Query
`GET /events/{event_id}` with `X-Status-Secret` to retrieve completion or
failure status. A failed operation is not a successful release or approval.
Inbound payloads, results and graph checkpoints may contain sensitive
information; restrict database access and define retention.

## GitLab polling

The optional `gitops-gitlab-poller` process requires PostgreSQL, a read-only
GitLab project token with API and repository access, and an exact project path.
Run `gitops-migrate` before starting it. Set `GITOPS_GITLAB_URL`,
`GITOPS_GITLAB_TOKEN`, `GITOPS_GITLAB_POLL_PROJECT`, and
`GITOPS_GITLAB_WEBHOOK_ENABLED=false`. The poller refuses to start if GitLab
webhook ingestion is enabled. Other webhook endpoints are unaffected. In Helm,
set `gitlabPoller.enabled=true` and the non-secret settings in `env`; put the
database URL and, unless using the Key Vault option below, the token in the
externally managed Secret. Do not put credentials in Helm values, command
arguments, CI logs, or chat.

On AKS, the optional `gitlabKeyVault` chart configuration mounts the token
directly from Azure Key Vault into the poller and worker using the Secrets
Store CSI driver and workload identity. Enable the AKS Key Vault provider,
configure `azureWorkloadIdentity`, and grant that identity `Key Vault Secrets
User` at the specific secret scope. Set the vault, secret name and tenant ID
in the chart values; these are identifiers, not token values. The chart does
not sync the token into a Kubernetes Secret. The database URL still comes from
the existing application Secret. The processes read the token once at startup:
restart both worker and poller after rotating it. This staging configuration
does not enable automatic CSI rotation.

The first successful snapshot becomes the baseline and does **not** enqueue
existing MRs or tags. Later snapshots enqueue new or changed MRs and new,
recreated or changed `REL/*` tags. The worker fetches the current MR diff and
applies the same advisory routing rules; tag events record a release, not a
deployment. GitLab writes remain disabled unless separately enabled. Snapshot
updates and inbox inserts commit together, and a PostgreSQL advisory lock
prevents concurrent pollers for the same project. Failures are logged and do
not advance the baseline; investigate worker failures separately. A project ID
change at the configured path also stops baseline advancement.

Polling is not an event log: changes that occur and disappear between
snapshots can be missed, and multiple changes may collapse into one observation.
Job events and deployment CSV reconciliation are **not** polled. Each poll
reads the entire project MR and matching tag lists, with at least three API
requests and additional requests for every non-empty page. The five-second
staging interval can impose substantial load or hit rate limits; coordinate
with the GitLab administrator and monitor errors. A malformed release tag or
an incomplete/inconsistent API snapshot can prevent the poll from advancing.

The sample `deploy/staging/gitlab-polling-values.yaml` enables polling for the
selected staging project every five seconds, disables GitLab webhook ingestion,
and leaves writes off. Applying the values requires a separate deployment. Do not configure
GitLab to send to the internal ClusterIP: it is not reachable outside the
cluster. Disable any webhook already configured in GitLab until an approved
private HTTPS route and webhook secret are ready. When transitioning to
webhooks, disable the poller and enable webhook ingestion only after verifying
the gateway and credentials. Concurrent webhook/poller ingestion and reliable
cross-channel deduplication are not implemented; switching can miss or repeat
observations and needs an operator-controlled cutover and reconciliation.

## Worker operation and recovery

Use PostgreSQL, run `gitops-migrate` once per deployment before starting API or
workers, and start `gitops-worker` as a separate process. The chart includes a
worker deployment but does not run migrations. Migration adopts the existing
baseline tables by name; back up and verify their schema before upgrading an
existing installation. The migration role needs DDL privileges; runtime roles
need access to the application and LangGraph checkpoint tables. Use an
appropriately scoped database connection and secure transport.

The worker locks inbox rows and serializes release threads with PostgreSQL
transaction advisory locks. Later events for a release wait behind a failed
earlier event; unrelated threads can continue. Processing failures are
recorded as `failed` and logged; there is no automatic retry. After diagnosis,
an operator can run `gitops-worker --retry-event <id>`. To reject an
irrecoverably invalid event and unblock its thread, run
`gitops-worker --reject-event <id> --reason "<reason>"`; the rejection is
audited. For example, an approval received before its tag must be rejected
and re-submitted after the tag is processed.

MR comments are committed to the outbox with the event transaction. With
`WRITE_ENABLED=true`, the worker records `sending` before posting. Any error
is `uncertain`; a worker crash can leave `sending`. Neither is automatically
retried, since the remote comment might already exist. Operators must inspect
GitLab and reconcile those rows manually. Do not copy this comment dispatcher
as an exactly-once protocol for job plays or rollback MRs.

The baseline tests use SQLite and an in-memory checkpointer. The optional
PostgreSQL tests require a **dedicated, empty disposable database**, with
`GITOPS_TEST_DATABASE_URL` set to its SQLAlchemy connection string:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_postgres_workflow.py
```

## Read-only MCP servers

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

## Model advisories

The worker can request JSON advisories after it has evaluated an MR or
reconciled a deployment. They appear in the event result as
`advisory.status=generated` with a summary and recommendations. If the
provider or output validation fails, the deterministic result remains intact
and the event result contains `advisory.status=failed`; the worker logs the
error type. There is no model-driven tool calling, automatic posting, promotion,
or rollback. The model is not used when no routing findings exist.

The only data sent to either provider is a fixed system instruction and an
allowlisted task and finding category. The adapter does **not** send paths,
diffs, source code, job IDs, project IDs, tenant names, chart versions,
credentials, or raw errors. Model output is untrusted; do not treat it as an
approval or execute commands it suggests. A crash or operator retry may repeat
a model request and incur additional charges. The model call is made while the
worker holds its event transaction, so size the timeout accordingly. Ensure
your organization has approved the configured endpoint and egress.

To enable Azure OpenAI, install the updated dependencies and configure the
worker with `GITOPS_LLM_ENABLED=true`, `GITOPS_LLM_PROVIDER=azure_openai`,
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
resource. A configured Azure resource and model deployment are prerequisites;
this project does not create them. Actual Azure authentication and inference
have not been exercised by the included mocked tests.

For a future self-hosted model, select `GITOPS_LLM_PROVIDER=self_hosted`, set
`GITOPS_LLM_SELF_HOSTED_BASE_URL` to its OpenAI-compatible `/v1` endpoint,
`GITOPS_LLM_SELF_HOSTED_API_KEY` from a secret, and `GITOPS_LLM_MODEL` to its
model identifier. The server must support Chat Completions, JSON mode and
`max_completion_tokens`. HTTPS is mandatory except for explicitly enabled
loopback-only development using `GITOPS_LLM_ALLOW_INSECURE_LOOPBACK=true`.

The CSV requires headers `application,chart_version,repository`. A chart
application is treated as healthy only when `spec.source.targetRevision` and
`status.sync.revision` both match the CSV version, sync is `Synced`, and
health is `Healthy`. Adapt this for Git-backed or multi-source Applications.

## Preflight in your release pipeline

Install the package and Helm in a **network-isolated** runner, mount the chart
and values read-only, and run (paths below are examples):

```sh
gitops-preflight --chart ./chart --baseline ./values/vn_base.yaml \
  --tenant ./values/clientdemo.yaml --tenant ./values/vn_base.yaml \
  --tenant ./values/vbl.yaml --tenant ./values/payfare.yaml \
  --tenant ./values/viseca.yaml
```

Require this command to exit successfully before `build_and_push_chart`.
The isolation is a responsibility of the runner platform, not the application.
The API service does not invoke Helm or have Kubernetes API access.

## Deployment

Create an externally managed Kubernetes Secret containing the configured
environment variables, set `existingSecret`, set the image and deploy the
chart in `deploy/helm/helix-gitops-agents`. The chart intentionally grants no
Kubernetes read or write permissions because this implementation uses Argo CD
over HTTPS. It does not configure ingress, TLS, database provisioning or network
policy. Do not deploy it until those controls and the limitations above are
addressed. The sample GitLab pipeline tests and builds an image but does not
publish it.

### AKS staging infrastructure

The Terraform in `infra/` prepares a single-node AKS Free-tier cluster, three
managed identities, the worker's federated credential and scoped role
assignments for the existing Azure AI resource and ACR. It does not create
the application, namespace, secrets, database, ingress, image or webhook
gateway. The Terraform execution identity needs permission to create
resources and role assignments at the relevant scopes. It must also have
access to the selected subscription. Required Azure resource providers must
already be registered; this configuration does not register them.

Before provisioning:

1. Confirm Azure subscription 1, Sweden Central, SKU availability, quota, and
   the complete monthly cost. The single VM was priced at approximately
   EUR 1.63 for 10 hours, but disks, the AKS standard load balancer and public
   IP, registry, model requests and other charges continue separately. The
   EUR 25 budget is not enforced automatically and may be exceeded.
2. Set `ARM_SUBSCRIPTION_ID` in the local environment to the subscription ID
   selected by `az account show --query id -o tsv`. Do not commit credentials.
   Supply `authorized_ip_ranges` as a nonempty set of approved public IPv4
   CIDRs through a protected `.tfvars` file or environment variable. The
   cluster API rejects an unrestricted `0.0.0.0/0` input. Supply the approved
   operator's Microsoft Entra user object ID as `operator_object_id` in the
   same protected file. When the signed-in user is the approved operator, obtain
   it with `az ad signed-in-user show --query id -o tsv`. This explicit value
   must not silently change when Terraform runs under another identity.
3. Run `terraform -chdir=infra init`, `terraform -chdir=infra validate`, and
   `terraform -chdir=infra plan` with the approved CIDRs. Review the real plan,
   roles, and cost before separately approving an apply. Terraform state can
   contain sensitive data: protect and back it up, and use an encrypted remote
   backend with access control and locking for shared use. The default local
   backend is for isolated testing only.
4. Local AKS accounts are disabled. Terraform plans a cluster-scoped Azure
   Kubernetes Service RBAC Cluster Admin role for the specified operator.
   The operator also needs permission to retrieve cluster user credentials
   (for example Azure Kubernetes Service Cluster User); this is not assigned
   by Terraform. Verify existing access before use. Use a separate kubeconfig
   so shared contexts are not changed.

The optional `deploy/staging/postgres.yaml` is a **test-only**, single-instance
PostgreSQL StatefulSet with a persistent Azure disk and internal ClusterIP.
Create the `gitops-staging` namespace and an externally managed
`gitops-postgres-credentials` Secret with a `password` key before applying it.
Create a separate `gitops-agent-credentials` Secret with a matching
`GITOPS_DATABASE_URL` and the required webhook and integration credentials.
Use synthetic data: this sample has no high availability, backup, database
network policy, or TLS between pods. Namespace membership alone does not
isolate the database. Configure and verify those controls before real data.
Persistent disks retain charges while AKS is stopped.

For an idle staging cluster, stop and later restart it with `az aks stop` and
`az aks start`, specifying the configured resource group and cluster name.
Stopping compute does not eliminate charges for retained disks, networking,
or other Azure resources. Check the cluster power state before use.

Publish the application image to ACR, run `gitops-migrate` once with the
database connection, and then install the existing Helm chart. Set its
`image.repository` and tag, `azureWorkloadIdentity.enabled=true`,
`azureWorkloadIdentity.clientId` to the Terraform worker identity output,
and `env.GITOPS_LLM_AZURE_AUTH=workload_identity`. The API and worker use the
same external application Secret. Configure a trusted private ingress or
tunnel separately; neither Terraform nor the chart exposes a webhook endpoint.
Verify that the worker can reach Azure OpenAI and process a test event before
relying on it.

Stop and start the cluster with `az aks stop` and `az aks start` when not
testing. Stopping does not remove disks, public networking or other charges,
and webhooks cannot arrive during downtime. No scheduling or spending cap is
included. The sample is not a production or disaster-recovery architecture.
