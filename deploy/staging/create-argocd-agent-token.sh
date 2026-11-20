#!/bin/sh
set -eu
umask 077

trap 'rm -f /tmp/argocd-ca.crt /tmp/argocd-session.json /tmp/argocd-admin-auth.conf /tmp/argocd-token-response.json /tmp/argocd-agent-auth.conf /tmp/argocd-token-patch.json /tmp/argocd-read-check.json /tmp/argocd-other-check.json /tmp/argocd-sync-check.json' 0 HUP INT TERM

argo_url=https://argocd-server.argocd.svc.cluster.local
kubectl get configmap argocd-ca-bundle -n gitops-staging \
  -o jsonpath='{.data.ca\.crt}' > /tmp/argocd-ca.crt

admin_password=$(kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath='{.data.password}' | base64 -d)
printf '%s' "$admin_password" |
  jq -Rs --arg username admin '{username:$username,password:.}' |
  curl --fail --silent --show-error --cacert /tmp/argocd-ca.crt \
    -H 'Content-Type: application/json' --data-binary @- \
    "$argo_url/api/v1/session" > /tmp/argocd-session.json
admin_token=$(jq -er '.token' /tmp/argocd-session.json)
printf 'header = "Authorization: Bearer %s"\n' "$admin_token" \
  > /tmp/argocd-admin-auth.conf

curl --fail --silent --show-error --cacert /tmp/argocd-ca.crt \
  --config /tmp/argocd-admin-auth.conf -H 'Content-Type: application/json' \
  --data-binary '{"name":"helix-gitops-readonly-90d","expiresIn":7776000}' \
  "$argo_url/api/v1/account/gitops-agent/token" > /tmp/argocd-token-response.json
argo_token=$(jq -er '.token' /tmp/argocd-token-response.json)
printf '%s' "$argo_token" |
  jq -Rs '{data:{GITOPS_ARGOCD_TOKEN:(. | @base64)}}' \
  > /tmp/argocd-token-patch.json
kubectl patch secret gitops-agent-credentials -n gitops-staging \
  --type merge --patch-file /tmp/argocd-token-patch.json
printf 'header = "Authorization: Bearer %s"\n' "$argo_token" \
  > /tmp/argocd-agent-auth.conf

read_code=$(curl --silent --show-error --cacert /tmp/argocd-ca.crt \
  --config /tmp/argocd-agent-auth.conf -o /tmp/argocd-read-check.json \
  -w '%{http_code}' \
  "$argo_url/api/v1/account/can-i/applications/get/agentic-gitops-accelerator%2Fprobe")
other_code=$(curl --silent --show-error --cacert /tmp/argocd-ca.crt \
  --config /tmp/argocd-agent-auth.conf -o /tmp/argocd-other-check.json \
  -w '%{http_code}' \
  "$argo_url/api/v1/account/can-i/applications/get/default%2Fprobe")
sync_code=$(curl --silent --show-error --cacert /tmp/argocd-ca.crt \
  --config /tmp/argocd-agent-auth.conf -o /tmp/argocd-sync-check.json \
  -w '%{http_code}' \
  "$argo_url/api/v1/account/can-i/applications/sync/agentic-gitops-accelerator%2Fprobe")

printf 'READ_SCOPE_HTTP=%s ' "$read_code"
jq -c . /tmp/argocd-read-check.json
printf 'OTHER_PROJECT_HTTP=%s ' "$other_code"
jq -c . /tmp/argocd-other-check.json
printf 'SYNC_SCOPE_HTTP=%s ' "$sync_code"
jq -c . /tmp/argocd-sync-check.json
kubectl describe secret gitops-agent-credentials -n gitops-staging
