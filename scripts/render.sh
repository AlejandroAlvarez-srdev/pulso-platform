#!/usr/bin/env bash
# Render every Kustomize overlay and the Helm chart (dev and prod values) to
# rendered/<name>/manifests.yaml. Needs kustomize (or kubectl) and helm.
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v kustomize >/dev/null 2>&1; then
  build() { kustomize build "$1"; }
elif command -v kubectl >/dev/null 2>&1; then
  build() { kubectl kustomize "$1"; }
else
  echo "render: need kustomize or kubectl on PATH" >&2
  exit 1
fi

for pair in dev=k8s/overlays/dev prod=k8s/overlays/prod kind-smoke=k8s/ci/kind-smoke; do
  name="${pair%%=*}"
  dir="${pair#*=}"
  mkdir -p "rendered/${name}"
  build "${dir}" >"rendered/${name}/manifests.yaml"
  echo "render: ${dir} -> rendered/${name}/manifests.yaml ($(grep -c '^kind:' "rendered/${name}/manifests.yaml") objects)"
done

command -v helm >/dev/null 2>&1 || { echo "render: need helm on PATH" >&2; exit 1; }
for env in dev prod; do
  mkdir -p "rendered/helm-${env}"
  helm template pulso charts/pulso --namespace pulso \
    --values "charts/pulso/values-${env}.yaml" >"rendered/helm-${env}/manifests.yaml"
  echo "render: charts/pulso + values-${env}.yaml -> rendered/helm-${env}/manifests.yaml ($(grep -c '^kind:' "rendered/helm-${env}/manifests.yaml") objects)"
done
