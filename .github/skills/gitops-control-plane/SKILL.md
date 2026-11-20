---
name: gitops-control-plane
description: Work on the GitOps agent control plane (FastAPI, worker, routing/reconciliation/query agents, console UI, Helm and Terraform for AKS). Use when changing agents, event handling, config, the console, deployment, or when explaining or operating the system. Not for generic GitOps advice.
---

# GitOps agent control plane

A Python/FastAPI control layer around existing GitLab CI, Harbor and Argo CD. It
ingests GitLab events, evaluates them with deterministic agents, records an audit
ledger in PostgreSQL, and optionally explains results with Azure OpenAI. It does
**not** replace those tools and never writes to a workload cluster. Changes go
through a GitLab merge request or an authorized CI job.

It is a **staging prototype, not a production safety gate**. Say so when relevant.

## Architecture

| Area | Location | Role |
| --- | --- | --- |
| API | `app/main.py`, `app/ingestion/` | Authenticated webhooks, health, query/bundle/rollback gateways. Persists events and returns 202. |
| Worker | `app/worker.py` | Processes the inbox, dispatches by event `kind`, writes audit rows, checkpoints with LangGraph. |
| Poller | `app/gitlab_poller.py` | Alternative to webhooks: observes MRs, `REL/*` and `HOT/*` tags, builds. Do not run both modes. |
| Agents | `app/agents/` | `routing_agent`, `reconciliation_agent`, `preflight_agent`, `promotion_agent`, `query_agent`. |
| Tools | `app/tools/` | GitLab, Harbor, Argo CD, Helm sandbox and notifier clients. |
| Supervisor | `app/supervisor/` | LangGraph state and guardrails. Checkpoints outcomes; it does not orchestrate agents as graph nodes. |
| Model advisories | `app/llm/` | Azure OpenAI or self-hosted adapter. Fixed prompts, allowlisted categories only. |
| MCP | `app/mcp/` | Optional read-only MCP server and client. Direct clients are the default. |
| Traceability, bundles, rollback | `app/traceability.py`, `app/release_bundles.py`, `app/rollback.py` | Provenance index, approved snapshots with drift, draft rollback MRs. |
| Console UI | `app/ui.py`, `app/ui_static/` | Entra sign-in (MSAL code flow, PKCE), query chat, bundle, rollback and wave approvals. |
| Storage | `app/storage/` | SQLAlchemy models and Alembic migrations `0001`–`0006`. |
| Config | `config/settings.py`, `rules_catalog.yaml`, `rollout_waves.yaml` | `GITOPS_*` settings and declarative rules. |
| Deploy | `deploy/helm/helix-gitops-agents/`, `deploy/staging/`, `infra/` | Helm chart, staging overlays and Gateway manifest, Terraform for AKS. |

## What each agent really does

- **Routing**: checks an MR diff against `rules_catalog.yaml`. Findings are advisory, not merge gates. Comments need `GITOPS_WRITE_ENABLED=true`.
- **Reconciliation**: parses the `deploy_release_*` CSV (`application,chart_version,repository`) and compares each app with Argo CD. Healthy means `targetRevision` and `status.sync.revision` match the CSV version, sync is `Synced` and health is `Healthy`.
- **Preflight**: `gitops-preflight` CLI renders `helm template` per tenant and diffs ConfigMap keys. CI-only, not wired into the worker.
- **Promotion**: wave policy helper. It records approvals and never triggers rollouts.
- **Query**: Azure OpenAI classifies intent from fixed keywords. Deterministic code then gathers evidence and limitations. Read-only.
- **Bundles and rollback**: approved release snapshots, drift against Argo CD, and a **draft** rollback MR. Never merges or deploys.

## Rules to follow

1. **Never expose secrets.** Do not print, log, paste or pass secret values in command arguments, uploaded scripts, Helm values or source. Secrets come from Key Vault CSI mounts or Kubernetes Secrets. Treat `*_SECRET`, tokens and passwords as write-only.
2. **Keep writes off unless asked.** `GITOPS_WRITE_ENABLED` and the query, bundle and rollback features are disabled by default. Enable them only on explicit request and with their prerequisites.
3. **The model is advisory.** It never decides policy, approvals or deployments, and receives only fixed task and category values, never diffs, paths, versions, tenants or credentials. Do not add model tool-calling.
4. **Approver identity comes from the verified Entra actor**, never from a client-supplied field. Mutating console routes require an exact origin and a session-bound CSRF token.
5. **Distinct secrets.** Query, bundle, rollback, status, webhook and Teams secrets must differ from each other and from service credentials.
6. **Be accurate about status.** Argo CD and Harbor are not connected in staging, Teams is only a gateway contract (no bot), and preflight and promotion are not automated. Do not claim otherwise.
7. **Preserve unrelated changes.** The workspace may not be a Git repository. Make surgical edits.

## Configuration essentials

All settings are `GITOPS_*` variables defined in `config/settings.py`. File variants (`*_TOKEN_FILE`, `GITOPS_UI_ENTRA_CLIENT_SECRET_FILE`) read mounted secret files.

- **Console**: `UI_ENABLED`, `UI_PUBLIC_ORIGIN` (HTTPS), `UI_ENTRA_TENANT_ID`, `UI_ENTRA_CLIENT_ID`, the client secret, `UI_ALLOWED_USERS` (Entra object IDs), and `TEAMS_APPROVERS` for approvals. Requires migration `0006`.
- **Query**: `QUERY_ENABLED`, `QUERY_SECRET`, **non-empty** `QUERY_APPLICATIONS` and `QUERY_PROJECTS`, Azure OpenAI (`LLM_*`), and GitLab and Argo CD read access in the API process.
- **Bundles**: query prerequisites plus `BUNDLE_ENABLED`, `BUNDLE_GATEWAY_SECRET`, `BUNDLE_IMAGE_REPOSITORIES`, `BUNDLE_CONFIG_PROJECTS`, Harbor and Argo CD.
- **Rollback**: bundle prerequisites plus `ROLLBACK_ENABLED`, `ROLLBACK_GATEWAY_SECRET`, `ROLLBACK_TARGETS` (JSON), migration `0005`, and writes enabled.
- **Polling**: `GITLAB_POLL_PROJECT`, `GITLAB_POLL_SECONDS`, `GITLAB_BUILD_POLL_*`; keep `GITLAB_WEBHOOK_ENABLED=false`.

## Common tasks

Use the project virtual environment, not a bare system `python`.

```powershell
pip install -e ".[test]"                                         # install with test extras
python -m pytest -q                                              # full suite (PostgreSQL tests skip without a database)
python -m pytest tests\test_ui.py tests\test_ui_gateway.py -q    # console and gateway
gitops-migrate                                                   # apply Alembic migrations (needs the database URL)
gitops-worker                                                    # event worker
gitops-gitlab-poller                                             # GitLab poller
gitops-preflight --chart ./chart --baseline ./values/vn_base.yaml --tenant ./values/viseca.yaml
node --check app\ui_static\app.js                                # syntax-check the UI script
```

Infrastructure and chart validation:

```powershell
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
terraform -chdir=infra test                                      # mocked assertions
helm lint deploy\helm\helix-gitops-agents
helm template helix deploy\helm\helix-gitops-agents -f deploy\staging\ui-values.yaml
```

### Adding or changing behaviour

- **New event kind**: add a branch in `handle_event` (`app/worker.py`), an audit row and a test. Unknown kinds must raise.
- **New routing rule**: edit `config/rules_catalog.yaml`, then extend `tests/test_routing_rules.py`.
- **New setting**: add it to `config/settings.py` with a safe default (disabled or empty), document it in the README, and cover validation with a test.
- **Schema change**: add a new Alembic revision under `app/storage/migrations/versions/`. Never edit past revisions, and downgrades must not silently destroy data.
- **UI change**: keep the strict CSP (`script-src 'self'; style-src 'self'`), so no inline scripts or styles. Run `node --check` and the UI tests.

## Staging deployment (AKS)

- Target: resource group `rg-agentic-gitops-test`, cluster `agenticGitOpsTest`, namespace `gitops-staging`, Helm release `helix` (API service `helix-agents`).
- The public console is `https://agegitops.giddyitsolutions.com/console` through an AKS-managed Gateway API with cert-manager. Only `/console` is routed publicly. Webhook, health and other API paths must stay unreachable from outside.
- Identities are separate: the worker identity, and a dedicated console identity with Key Vault access scoped to one secret.
- Direct Kubernetes API access from the dev machine times out during TLS setup. Use `az aks command invoke` or the Run Command REST API (it needs an Entra cluster token). Never send secrets through it.
- The cluster is **stopped between sessions** to control cost. Start it with `az aks start`, verify, and stop it again unless told otherwise. While stopped, the UI and certificate renewal are unavailable.
- Build the image with Podman. On the corporate network the build needs a build-only trusted CA in a separate stage that must not appear in the final image. Publish to the existing ACR under a new unique tag, record the digest, and deploy pinned by digest.
- For Helm upgrades preserve the polling overlay (`deploy/staging/gitlab-polling-values.yaml`), and use `--reuse-values` when changing only the image.
- Record real deployment results in `.azure/deployment-plan.md`. Only the Azure validate workflow may set its status to `Validated`.

## Known limits to state honestly

- The gateway has no ingress rate or body-size limits, and the in-cluster PostgreSQL has no backups or hardening.
- The Entra client secret expires on **18 December 2026**; rotation needs an API restart because CSI rotation is off.
- Query, bundles, rollback and approvals are disabled in staging, and the query service needs extra identity access (GitLab token and Azure OpenAI) before it can work.
