# GitOps agent control plane

A Python/FastAPI multi-Agent system that add an intelligent control layer around existing GitLab CI, Harbor and Argo CD. It integrates with GitLab, Argo CD, and PostgreSQL using
configurable example contracts. **This is not a production-ready release
orchestrator.** No deployment should rely on it as a safety gate without
completing and testing the items below against your actual systems.

## Solution Architecture

![Solution architecture for the GitOps agent control plane](./docs/solution-architecture.svg)

The control plane wraps existing GitLab CI, Harbor and Argo CD processes; it
does not replace them or write directly to workload clusters.

| Component | Responsibility and boundary |
| --- | --- |
| API and event worker | Authenticate and persist incoming events, process the inbox, record audit results and checkpoint event outcomes with LangGraph. GitLab polling and GitLab webhook ingestion are alternative modes, not concurrent sources. |
| Routing agent | The event worker calls routing checks for MR events; findings are advisory, not GitLab merge gates. |
| Reconciliation agent | The event worker compares deployment CSV observations with Argo CD application state. |
| Preflight agent | A separate Helm-rendering CLI for an independently isolated CI runner; not wired into the supervisor. |
| Promotion agent | A policy helper, not an automated rollout and not wired into the supervisor. |
| Query agent and traceability | The opt-in read-only query endpoint correlates indexed provenance, GitLab, Harbor and Argo CD evidence. **Azure OpenAI is required when the query endpoint is enabled**: the API extracts fixed generic keywords and sends those to the model for intent classification, not the raw question or repository data. |
| Bundles and rollback | Optional approved bundles support drift comparison. Rollback requires a separately authorized approval and enabled writes, and stages only a draft GitLab MR; it does not merge or deploy. |
| Storage and integrations | PostgreSQL holds the inbox, ledger and checkpoints. Direct clients are the default; optional MCP servers expose read-only tools. Key Vault CSI supplies worker and poller secrets, while API credentials must be provisioned separately. |



## What works

- GitLab, Argo CD and gateway endpoints authenticate using separate shared
  secrets. Empty secrets always reject requests.
- Authenticated requests are persisted to a PostgreSQL inbox and return HTTP
  202. A separate worker processes them and checkpoints LangGraph state.
  SQL tables persist releases, approvals and audit results.
- GitLab MR hooks fetch a diff and apply the example routing rules. Optional
  comments require `GITOPS_WRITE_ENABLED=true`.
- An optional GitLab poller observes merge requests, `REL/*` and `HOT/*` tags and
  submits changes to the same worker inbox, without requiring inbound webhooks.
- An opt-in traceability index verifies CI provenance manifests, Git ancestry
  and Harbor digests; it can also record deployment CSV and Argo CD observations.
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
- An optional, authenticated read-only query endpoint supports chart-version,
  GitLab ref-comparison, Jira fix and release-origin candidate questions using
  restricted Azure model routing.
- Opt-in release bundles snapshot indexed CI provenance, require explicit
  gateway-mediated approval, and compare approved snapshots with Argo CD.
- A separately gated rollback API can propose and, after explicit approval,
  stage a draft GitLab merge request for allowlisted configuration files.
- The Helm chart uses a non-root service account with no Kubernetes permissions,
  no mounted API token, a read-only filesystem and an externally managed secret.

## Important limitations

- Checkpointed graphs record event outcomes per thread, but the multi-step
  promotion state machine, human-in-the-loop interrupt and job-to-release
  correlation are not implemented. Poll/backfill events for the same build
  share a build thread; release tags and approvals share a release thread. Checkpoints
  and application transactions use separate connections: the inbox and ledger
  are authoritative if a checkpoint is ahead after a crash. Never initiate an
  external action solely from checkpoint state.
- The model is advisory only and is not an autonomous supervisor. Preflight,
  promotion, rollback, and ChatOps have no model integration. Model output is
  untrusted, may be inaccurate, and requires human review.
- The query endpoint has a shared deployment-level secret, not individual
  identities or per-user authorization. Everyone with the secret can query all
  configured resources using the service's upstream credentials. It has no
  per-user audit trail, rate limiting, Jira API integration, rollback, or
  mutation capabilities. Release bundles use separate endpoints and controls.
- Bundle approval depends on a separately implemented trusted identity gateway
  and trusted CI provenance. It is not a promotion authorization or proof of
  live pod image IDs, deployed chart digest, or package contents.
- Rollback stages GitLab changes only when explicitly enabled and authorized;
  it does not merge or deploy. It is not integrated into the natural-language
  query endpoint or supervisor.
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
| `ARGOCD_URL`, `ARGOCD_TOKEN`, `ARGOCD_WEBHOOK_SECRET` | Read-only Argo CD API and separate notification secret |
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

For example, `DATABASE_URL` above is supplied as `GITOPS_DATABASE_URL`.
GitLab and Argo CD API URLs must use HTTPS. Configure least-privilege GitLab
and Argo CD credentials and restrict the ingress to the intended sources.
Routing and rollout YAML are illustrative; adapt both and rebuild the image.

## Read-only natural-language queries

The query service is disabled by default. Configure `GITOPS_QUERY_ENABLED=true`,
a dedicated `GITOPS_QUERY_SECRET`, and **both** nonempty allowlists
`GITOPS_QUERY_APPLICATIONS` and `GITOPS_QUERY_PROJECTS`. Provision the secret
separately from all other credentials. Use the existing Azure model settings
(`LLM_PROVIDER=azure_openai`, `LLM_MODEL`, `LLM_AZURE_ENDPOINT`, and the chosen
`LLM_AZURE_AUTH`). `LLM_ENABLED` controls worker advisories separately; enabling
queries independently authorizes model requests. Configure read-only Argo CD
and GitLab credentials for the API process. Both service URLs must be HTTPS.

Put this endpoint behind a trusted TLS gateway with restricted ingress; the
shared secret is not user authentication. Use least-privilege upstream service
accounts and rotate the query secret securely. Do not send secrets as query
fields, command-line arguments, or in logs. The chart's existing GitLab Key
Vault mount is for the worker and poller, **not** the API; API credentials and
Azure identity access must be provisioned and validated separately before any
deployment. This functionality is not enabled or deployed by default.

`POST /queries` requires `X-Query-Secret`. Example JSON bodies:

```json
{"question":"Which chart version is deployed in viseca-prod?","application":"viseca-prod"}
```

```json
{"question":"What changed between master and release/VN2608-1A?","project":"team/chart","from_ref":"master","to_ref":"release/VN2608-1A"}
```

With the traceability index configured, two additional query examples are:

```json
{"question":"Which chart package contained Jira fix GVH1797?","project":"team/chart","jira_id":"GVH1797"}
```

```json
{"question":"Was this a planned release or emergency hotfix?","project":"team/chart","chart_version":"260801.5"}
```

The application can be inferred when exactly one authorized name appears in the
question. The project can be omitted only if there is exactly one authorized
project; refs can be omitted when the question includes `between <ref> and
<ref>`. Explicit fields are recommended. Refs need not be branches: GitLab
compares the specified refs in the `from_ref` to `to_ref` direction. Unsupported,
ambiguous, write-oriented, or unauthorized requests fail rather than triggering
actions. The Azure model receives only a fixed vocabulary of generic keywords,
never the raw question, resource names, refs, repository content, diffs, or
service credentials. It selects a strictly validated intent; local code
performs all resource authorization and read-only operations.

Responses provide an observation time, evidence links and limitations.
Chart-version resolution requires a single-source Argo CD Application, a
reconciliation timestamp no more than five minutes old, `Synced` status, and a
matching latest successful deployment-history revision and source. For a Helm
repository, its revision is interpreted as the chart version; for a Git-backed
chart, the service reads `Chart.yaml` at the recorded commit from the same
configured GitLab host and an allowlisted project. Missing, stale, unsynced,
conflicting, multi-source, or unauthorized evidence produces no chart version.
Argo CD sync/history is not independent proof of live workloads; health and
timestamps are returned for review. GitLab comparisons return changed paths and
bounded textual version/tag hints, not a YAML-aware diff. Truncated or missing
diffs and timeouts are marked incomplete; GitLab limits can still omit changes
without signalling truncation. Source data and results may be sensitive:
protect the gateway, transport and client logs.

## Traceability index and build provenance

This feature is off by default. Before enabling it, run `gitops-migrate` on the
application database and configure the same traceability settings on the
worker, poller, and query API. Set `GITOPS_TRACEABILITY_ENABLED=true`, the
existing `GITOPS_GITLAB_POLL_PROJECT` to the exact chart project, exact build
job names, Jira prefixes, and authorized Harbor coordinates. The worker needs
read-only GitLab API/repository/artifact and Harbor access; the API needs GitLab
API access, database access and, for application queries, read-only Argo CD.
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

Create and publish this manifest in **your chart pipeline**; this project does
not modify or deploy that pipeline. The manifest is limited to 64 KiB, 50 fix
commits, 20 patch files per fix and 50 patch files overall. Every fix commit must be an ancestor of the
job's build commit. Jira references are extracted from those commit messages and
the paths of explicitly listed files under the configured patch directory; the
files must exist at the build commit. Other commits and patch files are not
scanned. The job's successful status, configured name, project, commit and
pipeline are checked independently; the Harbor tag must resolve to the claimed
SHA-256 digest at indexing time. Missing artifacts, ancestry conflicts, moved
tags, mismatched registry coordinates, or malformed manifests fail the inbox
event; they do not produce success-shaped records. CI authors and their build
system must be trusted: a manifest and ancestry do not independently prove a
fix was included in a chart or is effective, and a point-in-time tag check does
not make a Harbor tag immutable. Results contain the digest, manifest hash,
observation time and source links. Do not treat them as a signed attestation.

Enable the optional bounded job poller with
`GITOPS_GITLAB_BUILD_POLL_ENABLED=true`. It polls the latest 100 successful
project jobs on a separate interval (at least 30 seconds), selecting only the
configured exact build and optional deploy job names. Its first snapshot
establishes a separate baseline and skips existing jobs; changing the job-name
configuration creates a new baseline. A full window is logged as potentially
incomplete. Deployment jobs use the existing CSV contract and record its chart
claims alongside Argo CD sync/health observations; the CSV has no digest, so
these do not prove which indexed package was deployed. The staging values
request job polling, but it stays inactive until traceability is enabled and
configured. No staging deployment is performed by editing those values.

For explicit history backfill, identify retained successful build job IDs in
GitLab and run `python -m app.traceability --job-id 42 --job-id 43` with the
same configuration and database as the worker. This enqueues those IDs only;
the worker must be running to validate and index them. Backfill is idempotent
for repeated requests; inspect failures via the existing event-status and worker
recovery procedures. It does not discover historical jobs, reconstruct missing
artifacts, backfill old tags or automatically replay old deployment CSVs.
Polling and backfill may therefore leave gaps. Jira answers are candidates,
not proof that no other package contains the fix. Release/hotfix answers
classify candidate builds using their recorded ref and current same-commit
`REL/*`/`HOT/*` tags. Conflicting signals are reported as ambiguous; the
deployment origin remains **unverified** without digest-bearing deployment
evidence. Indexed data can be sensitive; restrict database access and retention.

## Approved release bundles and drift

This feature is disabled by default and has not been deployed. Run
`gitops-migrate`, configure the traceability index above, and enable the query
service and `GITOPS_BUNDLE_ENABLED`. Set the dedicated
`GITOPS_BUNDLE_GATEWAY_SECRET`, `GITOPS_TEAMS_APPROVERS`, and the two bundle
allowlists. `QUERY_PROJECTS` must include the chart project and every permitted
configuration project; `QUERY_APPLICATIONS` must include every application in
the bundle. The API process also needs read-only access to the database, GitLab
jobs, artifacts and configuration commits, Harbor artifacts, and Argo CD.
Use HTTPS, least-privilege credentials, restricted network access, and externally
managed secrets. Bundle and query secrets must be distinct from each other and
the other configured service credentials.

The existing Teams gateway is only a contract, not an identity provider. Before
enabling bundle creation or approval, independently implement a trusted TLS
gateway that authenticates each user, checks their authorization, derives the
immutable actor identity itself (never trust a client-supplied actor), and
protects the gateway secret. The actor must exactly match `TEAMS_APPROVERS`.
Anyone holding the gateway secret can impersonate any allowlisted actor; the
API itself does not verify user identity. The query secret separately grants
access to all configured bundle reads and drift results.

For bundle creation, extend the retained CI artifact shown above to schema
version 2 by setting `"schema_version": 2` and adding, for example:

```json
"environments": [
  {
    "application": "viseca-prod",
    "config_project": "team/config",
    "config_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "images": [
      {
        "repository": "team/backend",
        "digest": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
      }
    ]
  }
]
```

The snippet is a field in the preceding JSON object, not a standalone artifact.
Version 1 remains valid for indexing but cannot create a bundle. Version 2
requires 1–20 unique applications, 1–50 images per environment and at most
100 images overall. Config revisions must be full commit IDs; image digests
must be SHA-256. The CI pipeline must enumerate the intended image inventory;
this project does not generate or deploy that pipeline or its manifest.

After the successful build has been indexed, the trusted gateway calls:

```text
POST /release-bundles
X-Bundle-Gateway-Secret: <gateway-secret>
{"project":"team/chart","job_id":42,"actor":"immutable-manager-id"}
```

The response contains a draft fingerprint. A separately authorized gateway
request explicitly approves that exact fingerprint:

```text
POST /release-bundles/<fingerprint>/approval
X-Bundle-Gateway-Secret: <gateway-secret>
{"actor":"immutable-reviewer-id","request_id":"unique-approval-request-id"}
```

Creation and approval are separate operations; they do not enforce a two-person
rule. Approval rechecks the job, retained artifact hash, GitLab project IDs and
commits, and Harbor chart and image digests. The SHA-256 fingerprint covers the
canonical snapshot including environment mappings and artifact metadata.
Creation and approval are recorded in the audit ledger. Retrying approval with
the same actor and request ID is idempotent; another approval or reuse of the
request ID for a different bundle conflicts. A later scope change can make an
existing bundle inaccessible. An approval does not trigger a deployment.

Read and compare using `X-Query-Secret`:

```text
GET /release-bundles/<fingerprint>
GET /release-bundles/<fingerprint>/drift/viseca-prod
```

Only approved bundles can be compared. The comparison reports `match`, `drift`,
or `unknown` for chart version, Git configuration revision and digest-bearing
Argo CD image summaries. Stale, unsynced, multi-source, tag-only, missing, or
conflicting deployment history and other unverifiable evidence are not treated
as compliant. Argo CD's summary
is not an independent observation of live pod image IDs, and this implementation
cannot observe a verified deployed chart digest: that field is always `unknown`,
so overall status cannot be `match`. CI claims and independently existing
artifacts do not prove that the declared images or configuration were actually
packaged or deployed; approval is not a signed build attestation. Validate the
contract against your systems before relying on it as a release safety gate.

## Rollback merge requests

This feature is off by default and has not been deployed or validated against a
real GitLab instance. It requires the bundle prerequisites above, migration
`0005`, a separately provisioned identity-verifying gateway, and a distinct
`GITOPS_ROLLBACK_GATEWAY_SECRET`. The gateway must authenticate the user,
enforce authorization, supply the actor itself rather than trusting client input,
and protect the secret. All actors must appear in `GITOPS_TEAMS_APPROVERS`.
Anyone holding the gateway secret can impersonate an allowlisted actor. The
requester may also approve; there is no two-person rule.

Configure explicit per-application targets, for example:

```json
{"viseca-prod":{"branch":"main","files":["env/values.yaml","Chart.yaml"]}}
```

Set that JSON as `GITOPS_ROLLBACK_TARGETS`, and set `GITOPS_ROLLBACK_ENABLED=true`.
The target project and baseline commit come from the selected approved bundle.
An application can restore only its own bundle configuration project; there
is no automatic search for a prior master baseline. The operator must select
and verify the desired prior bundle. Each target has at most 20 unique literal
paths; files must exist at both commits, be UTF-8 text and fit the existing
64 KiB repository-file limit. Only files whose content differs are included.
Restoring an entire file may revert unrelated changes or security fixes: choose
the allowlist carefully and review every resulting diff.

The trusted gateway calls the following endpoints with
`X-Rollback-Gateway-Secret`. These examples show request bodies, not credentials:

```text
POST /rollbacks
{"bundle_fingerprint":"<approved-bundle-fingerprint>","application":"viseca-prod","actor":"manager-id","request_id":"unique-request-id"}

GET /rollbacks/<plan-id>?actor=manager-id
GET /rollbacks/<plan-id>/preview?actor=manager-id

POST /rollbacks/<plan-id>/approval
{"actor":"manager-id","fingerprint":"<rollback-plan-fingerprint>"}

POST /rollbacks/<plan-id>/execute
{"actor":"manager-id"}
```

The draft plan records the target project ID, branch head, bundle, baseline
commit, paths and before/after content hashes. Preview returns the actual
textual diffs; protect gateway access, transport and logs, because file
contents may be sensitive. Approval binds to the displayed plan fingerprint;
approving the bundle alone does not approve a rollback. A repeated proposal
request ID returns the same plan for the same actor and inputs. A repeated
approval by the same actor is idempotent.

Execution additionally requires `GITOPS_WRITE_ENABLED=true`, which also enables
other existing GitLab write features such as MR comments. Use a least-privilege
bot limited to the intended projects and `agent-rollback/*` branches. Before
writing, the service revalidates the approved bundle, Harbor digests, GitLab
project identity, target branch head and file hashes. It creates a new branch
from the pinned target commit, updates only the approved files and opens a
**draft** merge request; it never merges, runs a job or writes to Kubernetes.
GitLab may independently run pipelines when a branch or MR is created, so
ensure those pipelines cannot deploy without their own controls.

GitLab and the database cannot share an atomic transaction. Execution is claimed
once before the first write. A timeout or crash can leave `uncertain` or
`executing` status even when a branch, commit or MR was created; the service
does not retry these states. An operator must inspect GitLab and the audit
ledger before deciding what to do. A completed execution can be read again
without another write. Failed checks before the claim do not write; create and
approve a fresh plan when the target changes.

The target can still advance after the last check or after MR creation. Enforce
protected target branches and GitLab-side merge checks that revalidate the
target revision, source branch, complete diff, approvals and pipelines at merge
time; block automatic merges and invalidate stale approvals. A draft MR alone
is not a security boundary. Bundle evidence does not establish that the restored
file contents actually deploy its chart or images, nor that the baseline is
healthy or appropriate. Perform environment-specific human review and testing.

## Event contracts

`POST /webhooks/gitlab` expects `X-Gitlab-Token`, `X-Gitlab-Event` and
`X-Gitlab-Event-UUID` headers. The UUID must be stable across retries.
Supported event names are `Merge Request Hook`, `Tag Push Hook` and `Job Hook`.
An MR event uses `project.id`, `object_attributes.iid` and `target_branch`.
A release tag event uses `project_id`, `checkout_sha` and
`ref=refs/tags/REL/<version>` or `ref=refs/tags/HOT/<version>`. A successful job event uses `project_id`,
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
recreated or changed `REL/*` and `HOT/*` tags. The worker fetches the current MR diff and
applies the same advisory routing rules; tag events record a tag or release, not a
deployment. GitLab writes remain disabled unless separately enabled. Snapshot
updates and inbox inserts commit together, and a PostgreSQL advisory lock
prevents concurrent pollers for the same project. Failures are logged and do
not advance the baseline; investigate worker failures separately. A project ID
change at the configured path also stops baseline advancement.

Polling is not an event log: changes that occur and disappear between
snapshots can be missed, and multiple changes may collapse into one observation.
Configured build and deployment jobs can be polled separately as described
above; other jobs remain unobserved. Each MR/tag poll
reads the entire project MR and matching tag lists, with at least four API
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
