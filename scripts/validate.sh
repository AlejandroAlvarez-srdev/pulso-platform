#!/usr/bin/env bash
# Validate the rendered overlays: Kubernetes schemas, kube-linter, platform policy.
set -euo pipefail
cd "$(dirname "$0")/.."

K8S_VERSION="${K8S_VERSION:-1.30.0}"
PYTHON="${PYTHON:-python3}"

for tool in kubeconform kube-linter; do
  command -v "${tool}" >/dev/null 2>&1 || { echo "validate: ${tool} is not installed" >&2; exit 1; }
done

# Core schemas for the Kubernetes version, plus the community CRD catalog for
# ExternalSecret, SecretStore and the other operator resources.
CRD_CATALOG='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

echo "== kubeconform (Kubernetes ${K8S_VERSION}, strict)"
kubeconform -strict -summary -kubernetes-version "${K8S_VERSION}" \
  -schema-location default -schema-location "${CRD_CATALOG}" \
  rendered/*/manifests.yaml argocd/

for env in dev prod helm-dev helm-prod; do
  echo "== kube-linter (${env})"
  kube-linter lint "rendered/${env}"
done

for env in dev prod helm-dev helm-prod prod-canary; do
  echo "== platform policy (${env})"
  "${PYTHON}" scripts/policy_check.py "rendered/${env}/manifests.yaml"
done
