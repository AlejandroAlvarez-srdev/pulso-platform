#!/usr/bin/env bash
# Render every overlay to rendered/<name>/manifests.yaml (kustomize, or kubectl's built-in).
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

for example in k8s/overlays/prod/secrets/*.env.example; do
  if [[ ! -f "${example%.example}" ]]; then
    echo "render: ${example%.example} is missing (the release pipeline writes it;" \
      "run 'make prod-secrets-placeholder' to render with the templates)" >&2
    exit 1
  fi
done

for pair in dev=k8s/overlays/dev prod=k8s/overlays/prod kind-smoke=k8s/ci/kind-smoke; do
  name="${pair%%=*}"
  dir="${pair#*=}"
  mkdir -p "rendered/${name}"
  build "${dir}" >"rendered/${name}/manifests.yaml"
  echo "render: ${dir} -> rendered/${name}/manifests.yaml ($(grep -c '^kind:' "rendered/${name}/manifests.yaml") objects)"
done
