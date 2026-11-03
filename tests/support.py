"""Helpers shared by the test suite.

There is no kustomize binary in the unit-test job, so `load_kustomization` does a
deliberately small resolution: it follows `resources` (files and nested
kustomizations) and turns configMap/secret generators into stub objects. Patches are
not applied; the fully rendered overlays are checked in CI with the real kustomize.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "k8s"
sys.path.insert(0, str(ROOT / "scripts"))

import policy_check  # noqa: E402  (path set up above)

APPS = {
    # name: (image, container port, readiness path) as served by each service repository
    "orders-api": ("ghcr.io/pulso/pulso-orders-api", 3000, "/health/ready"),
    "matching": ("ghcr.io/pulso/pulso-matching", 8000, "/health"),
    "support-agents": ("ghcr.io/pulso/pulso-support-agents", 8000, "/healthz"),
    "saga": ("ghcr.io/pulso/pulso-saga", 8090, "/health"),
}
# Liveness/startup path where it differs from the readiness path (orders-api's readiness
# includes Postgres and draining; its liveness deliberately does not).
LIVENESS_PATHS = {"orders-api": "/health/live"}
# Services that serve Prometheus metrics on /metrics, and the raw names of their request
# histograms (the ServiceMonitors normalise both to http_request_duration_seconds{status}).
METRICS = {
    "orders-api": ("http_request_duration_seconds", "status_code"),
    "matching": ("pulso_http_request_duration_seconds", "status"),
}
# Services whose port comes from $PORT; the two FastAPI images start uvicorn on a fixed :8000.
READS_PORT_ENV = {"orders-api", "saga"}


def read_yaml(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def kustomization(directory: Path) -> dict:
    return read_yaml(directory / "kustomization.yaml")[0]


def load_kustomization(directory: Path) -> list[dict]:
    """Resources of a kustomization (recursively) plus stub generated objects."""
    k = kustomization(directory)
    docs: list[dict] = []
    for resource in k.get("resources", []):
        path = (directory / resource).resolve()
        docs.extend(load_kustomization(path) if path.is_dir() else read_yaml(path))
    known = {(d["kind"], d["metadata"]["name"]) for d in docs}
    for key, kind in (("configMapGenerator", "ConfigMap"), ("secretGenerator", "Secret")):
        for gen in k.get(key, []):
            if (kind, gen["name"]) not in known:
                docs.append({"apiVersion": "v1", "kind": kind, "metadata": {"name": gen["name"]}})
    return docs


def generator_literals(directory: Path, name: str) -> dict[str, str]:
    for gen in kustomization(directory).get("configMapGenerator", []):
        if gen["name"] == name:
            return dict(item.split("=", 1) for item in gen.get("literals", []))
    raise KeyError(name)


def all_kustomization_dirs() -> list[Path]:
    return sorted(p.parent for p in K8S.rglob("kustomization.yaml"))
