#!/usr/bin/env bash
# Validate the rendered manifests (run scripts/render.sh first).
#
#   validate.sh schemas   kubeconform against K8S_VERSION (default 1.30.0) + CRD catalog
#   validate.sh lint      kube-linter and the platform policy
#   validate.sh security  trivy misconfiguration scan (HIGH and CRITICAL fail)
#   validate.sh           all of the above
set -euo pipefail
cd "$(dirname "$0")/.."

K8S_VERSION="${K8S_VERSION:-1.30.0}"
PYTHON="${PYTHON:-python3}"
# Core schemas for the Kubernetes version, plus the community CRD catalog for
# ExternalSecret, ServiceMonitor, Rollout, Application and the other operator resources.
CRD_CATALOG='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "validate: $1 is not installed" >&2; exit 1; }
}

schemas() {
  need kubeconform
  echo "== kubeconform (Kubernetes ${K8S_VERSION}, strict)"
  kubeconform -strict -summary -kubernetes-version "${K8S_VERSION}" \
    -schema-location default -schema-location "${CRD_CATALOG}" \
    rendered/*/manifests.yaml argocd/
}

lint() {
  need kube-linter
  for env in dev prod helm-dev helm-prod; do
    echo "== kube-linter (${env})"
    kube-linter lint "rendered/${env}"
  done
  for env in dev prod helm-dev helm-prod prod-canary; do
    echo "== platform policy (${env})"
    "${PYTHON}" scripts/policy_check.py "rendered/${env}/manifests.yaml"
  done
}

security() {
  need trivy
  echo "== trivy config (rendered manifests and Helm chart)"
  trivy config --severity HIGH,CRITICAL --exit-code 1 rendered/
  trivy config --severity HIGH,CRITICAL --exit-code 1 charts/pulso
}

case "${1:-all}" in
  schemas) schemas ;;
  lint) lint ;;
  security) security ;;
  all) schemas; lint; security ;;
  *) echo "usage: $0 [schemas|lint|security]" >&2; exit 2 ;;
esac
