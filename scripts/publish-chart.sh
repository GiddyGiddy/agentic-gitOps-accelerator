#!/bin/sh
set -eu
set +x

chart=deploy/helm/helix-gitops-agents
name=helix-gitops-agents
host=harbor.giddyitsolutions.com
project=charts

rm -f gitops-provenance.json
: "${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}"
: "${CI_PIPELINE_ID:?CI_PIPELINE_ID is required}"
: "${CI_JOB_ID:?CI_JOB_ID is required}"
: "${HARBOR_USERNAME:?HARBOR_USERNAME is required}"
: "${HARBOR_PASSWORD_FILE:?HARBOR_PASSWORD_FILE is required}"
printf '%s\n' "$CI_COMMIT_SHA" | grep -Eq '^([a-f0-9]{40}|[a-f0-9]{64})$'
printf '%s\n' "$CI_PIPELINE_ID" "$CI_JOB_ID" | grep -Ev '^[1-9][0-9]*$' > /dev/null && {
  echo "Invalid pipeline or job ID" >&2
  exit 1
}

metadata=$(helm show chart "$chart")
chart_name=$(printf '%s\n' "$metadata" | sed -n 's/^name: //p')
base_version=$(printf '%s\n' "$metadata" | sed -n 's/^version: //p')
if [ "$chart_name" != "$name" ] || [ "$(printf '%s\n' "$base_version" | wc -l)" -ne 1 ] \
  || ! printf '%s\n' "$base_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "Chart name or base version is invalid" >&2
  exit 1
fi
version="${base_version}-ci.${CI_PIPELINE_ID}.${CI_JOB_ID}"
temp_dir=$(mktemp -d)
package="$temp_dir/$name-$version.tgz"
export HELM_REGISTRY_CONFIG="$temp_dir/registry.json"
cleanup() {
  rm -f "$HELM_REGISTRY_CONFIG" "$package" "$temp_dir/push.log"
  rmdir "$temp_dir"
}
trap cleanup EXIT

helm lint "$chart"
helm package "$chart" --version "$version" --destination "$temp_dir"
helm registry login "$host" --username "$HARBOR_USERNAME" --password-stdin < "$HARBOR_PASSWORD_FILE"
helm push "$package" "oci://$host/$project" > "$temp_dir/push.log"
digest=$(sed -n 's/^Digest: //p' "$temp_dir/push.log")
if [ "$(grep -c '^Digest: ' "$temp_dir/push.log" || true)" -ne 1 ] \
  || ! printf '%s\n' "$digest" | grep -Eq '^sha256:[a-f0-9]{64}$'; then
  echo "Helm did not return exactly one valid OCI digest" >&2
  exit 1
fi
cat "$temp_dir/push.log"
printf '{"schema_version":1,"commit_sha":"%s","chart":{"name":"%s","version":"%s","harbor_project":"%s","harbor_repository":"%s","tag":"%s","digest":"%s"},"fixes":[]}\n' \
  "$CI_COMMIT_SHA" "$name" "$version" "$project" "$name" "$version" "$digest" > gitops-provenance.json
