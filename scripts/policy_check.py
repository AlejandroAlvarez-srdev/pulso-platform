#!/usr/bin/env python3
"""Platform policy checks for Kubernetes manifests.

kubeconform answers "is this valid Kubernetes?" and kube-linter answers "is this a
generally good idea?". This script encodes the rules *this* platform commits to, plus
cross-object consistency that neither tool covers (Service selectors, Ingress
backends, HPA vs. spec.replicas, PDB coverage, dangling NetworkPolicies...).

Usage:
    kustomize build k8s/overlays/prod | python scripts/policy_check.py -
    python scripts/policy_check.py rendered/dev/manifests.yaml
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import yaml

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
LONG_RUNNING_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


# --------------------------------------------------------------------------- loading


def load_documents(sources: Iterable[str]) -> list[dict]:
    """Load every YAML document from the given paths ("-" reads stdin)."""
    docs: list[dict] = []
    for source in sources:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
        docs.extend(d for d in yaml.safe_load_all(text) if isinstance(d, dict))
    return docs


def _ref(doc: dict) -> str:
    return f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"


def _of_kind(docs: list[dict], *kinds: str) -> Iterator[dict]:
    return (d for d in docs if d.get("kind") in kinds)


def _pod_spec(workload: dict) -> dict:
    return workload.get("spec", {}).get("template", {}).get("spec", {}) or {}


def _template_labels(workload: dict) -> dict:
    return workload.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {}


def selector_matches(selector: dict | None, labels: dict) -> bool:
    """Evaluate a LabelSelector (matchLabels + matchExpressions) against labels."""
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


# --------------------------------------------------------------------------- per object


def check_workload(doc: dict) -> list[str]:
    ref = _ref(doc)
    spec = _pod_spec(doc)
    errors: list[str] = []

    if spec.get("automountServiceAccountToken") is not False:
        errors.append(f"{ref}: automountServiceAccountToken must be false")
    pod_sc = spec.get("securityContext") or {}
    if pod_sc.get("runAsNonRoot") is not True:
        errors.append(f"{ref}: pod securityContext.runAsNonRoot must be true")
    if (pod_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        errors.append(f"{ref}: pod seccompProfile must be RuntimeDefault")
    if doc["kind"] == "Deployment" and (doc["spec"].get("strategy") or {}).get("type") != "RollingUpdate":
        errors.append(f"{ref}: Deployment strategy must be RollingUpdate")
    if doc["kind"] == "Job" and "ttlSecondsAfterFinished" not in doc["spec"]:
        errors.append(f"{ref}: Job needs ttlSecondsAfterFinished (finished Jobs block re-applies)")

    for container in spec.get("containers", []) + spec.get("initContainers", []):
        cref = f"{ref}[{container.get('name')}]"
        image = container.get("image", "")
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        if "@sha256:" not in image and tag in ("", "latest"):
            errors.append(f"{cref}: image '{image}' must use an immutable tag (not latest/untagged)")

        resources = container.get("resources") or {}
        for section in ("requests", "limits"):
            for resource in ("cpu", "memory"):
                if resource not in (resources.get(section) or {}):
                    errors.append(f"{cref}: missing resources.{section}.{resource}")

        probes = ["livenessProbe", "readinessProbe"] if doc["kind"] in LONG_RUNNING_KINDS else []
        if doc["kind"] == "Deployment":
            probes.append("startupProbe")
        for probe in probes:
            if probe not in container:
                errors.append(f"{cref}: missing {probe}")

        sc = container.get("securityContext") or {}
        if sc.get("allowPrivilegeEscalation") is not False:
            errors.append(f"{cref}: allowPrivilegeEscalation must be false")
        if sc.get("readOnlyRootFilesystem") is not True:
            errors.append(f"{cref}: readOnlyRootFilesystem must be true")
        if sc.get("privileged"):
            errors.append(f"{cref}: privileged containers are not allowed")
        if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
            errors.append(f"{cref}: capabilities.drop must include ALL")
    return errors


# --------------------------------------------------------------------------- cross object


def check_references(docs: list[dict]) -> list[str]:
    errors: list[str] = []
    workloads = list(_of_kind(docs, *WORKLOAD_KINDS))
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    # An ExternalSecret materialises a Secret named after spec.target.name (default: its own name).
    names |= {
        ("Secret", (d.get("spec", {}).get("target") or {}).get("name") or d["metadata"]["name"])
        for d in _of_kind(docs, "ExternalSecret")
    }

    for wl in workloads:
        spec = _pod_spec(wl)
        sa = spec.get("serviceAccountName")
        if sa and ("ServiceAccount", sa) not in names:
            errors.append(f"{_ref(wl)}: serviceAccountName '{sa}' has no ServiceAccount")
        for container in spec.get("containers", []):
            for env in container.get("env", []):
                ref = (env.get("valueFrom") or {}).get("secretKeyRef")
                if ref and ("Secret", ref["name"]) not in names:
                    errors.append(f"{_ref(wl)}: env {env['name']} references missing Secret '{ref['name']}'")
            for source in container.get("envFrom", []):
                for key, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                    if key in source and (kind, source[key]["name"]) not in names:
                        errors.append(f"{_ref(wl)}: envFrom {kind} '{source[key]['name']}' does not exist")
        for volume in spec.get("volumes", []):
            if "configMap" in volume and ("ConfigMap", volume["configMap"]["name"]) not in names:
                errors.append(f"{_ref(wl)}: volume ConfigMap '{volume['configMap']['name']}' does not exist")

    for svc in _of_kind(docs, "Service"):
        selector = svc["spec"].get("selector")
        if not selector:
            continue
        backends = [w for w in workloads if selector_matches({"matchLabels": selector}, _template_labels(w))]
        if not backends:
            errors.append(f"{_ref(svc)}: selector {selector} matches no workload")
            continue
        port_names = {p.get("name") for w in backends for c in _pod_spec(w)["containers"] for p in c.get("ports", [])}
        for port in svc["spec"].get("ports", []):
            target = port.get("targetPort")
            if isinstance(target, str) and target not in port_names:
                errors.append(f"{_ref(svc)}: targetPort '{target}' is not a named container port")

    services = {d["metadata"]["name"]: d for d in _of_kind(docs, "Service")}
    for ing in _of_kind(docs, "Ingress"):
        for rule in ing["spec"].get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                backend = path["backend"]["service"]
                svc = services.get(backend["name"])
                if svc is None:
                    errors.append(f"{_ref(ing)}: backend Service '{backend['name']}' does not exist")
                    continue
                wanted = backend["port"].get("name") or backend["port"].get("number")
                available = {p.get("name") for p in svc["spec"]["ports"]} | {p["port"] for p in svc["spec"]["ports"]}
                if wanted not in available:
                    errors.append(f"{_ref(ing)}: Service '{backend['name']}' has no port '{wanted}'")

    deployments = {d["metadata"]["name"]: d for d in _of_kind(docs, "Deployment")}
    # An Argo Rollout with workloadRef owns the pods of the Deployment it references.
    rollouts = {d["metadata"]["name"]: d for d in _of_kind(docs, "Rollout")}
    for hpa in _of_kind(docs, "HorizontalPodAutoscaler"):
        target = hpa["spec"]["scaleTargetRef"]
        dep = None
        if target["kind"] == "Deployment":
            dep = deployments.get(target["name"])
        elif target["kind"] == "Rollout" and target["name"] in rollouts:
            ref = rollouts[target["name"]]["spec"].get("workloadRef") or {}
            dep = deployments.get(ref.get("name"))
        if dep is None:
            errors.append(f"{_ref(hpa)}: scaleTargetRef {target['kind']}/{target['name']} does not exist")
        elif "replicas" in dep["spec"]:
            errors.append(
                f"{_ref(hpa)}: target Deployment sets spec.replicas; every apply would reset the HPA's count"
            )
        if hpa["spec"].get("minReplicas", 1) > hpa["spec"]["maxReplicas"]:
            errors.append(f"{_ref(hpa)}: minReplicas is greater than maxReplicas")

    pdbs = list(_of_kind(docs, "PodDisruptionBudget"))
    for dep in deployments.values():
        if not any(selector_matches(p["spec"].get("selector"), _template_labels(dep)) for p in pdbs):
            errors.append(f"{_ref(dep)}: no PodDisruptionBudget selects this Deployment")
    for pdb in pdbs:
        if pdb["spec"].get("maxUnavailable") == 0:
            errors.append(f"{_ref(pdb)}: maxUnavailable 0 blocks every node drain")

    policies = list(_of_kind(docs, "NetworkPolicy"))
    if workloads and not any(
        p["spec"].get("podSelector") == {} and set(p["spec"].get("policyTypes", [])) == {"Ingress", "Egress"}
        and not p["spec"].get("ingress") and not p["spec"].get("egress")
        for p in policies
    ):
        errors.append("NetworkPolicy: a default-deny policy (empty podSelector, Ingress+Egress) is required")
    for policy in policies:
        selector = policy["spec"].get("podSelector")
        if selector and not any(selector_matches(selector, _template_labels(w)) for w in workloads):
            errors.append(f"{_ref(policy)}: podSelector matches no workload")

    errors.extend(check_network_edges(workloads, policies))

    for ns in _of_kind(docs, "Namespace"):
        labels = ns["metadata"].get("labels") or {}
        if labels.get("pod-security.kubernetes.io/enforce") != "restricted":
            errors.append(f"{_ref(ns)}: Pod Security Admission must enforce 'restricted'")
    return errors


# --------------------------------------------------------------------------- network edges


def _policy_types(policy: dict) -> set[str]:
    """Effective policyTypes: the API defaults to Ingress, plus Egress when egress rules exist."""
    spec = policy["spec"]
    if spec.get("policyTypes"):
        return set(spec["policyTypes"])
    return {"Ingress"} | ({"Egress"} if spec.get("egress") else set())


def _resolve_port(workload: dict, protocol: str, port: int | str) -> set[int]:
    """Container port numbers a NetworkPolicy port (number or name) means on this workload."""
    return {
        p["containerPort"]
        for c in _pod_spec(workload).get("containers", [])
        for p in c.get("ports", [])
        if p.get("protocol", "TCP") == protocol and port in (p["containerPort"], p.get("name"))
    }


def _in_namespace_selector(peer: dict) -> dict | None:
    """The podSelector of a peer meaning "pods of this namespace"; None for any other peer.

    namespaceSelector and ipBlock peers point outside the rendered manifests (ingress-nginx,
    Prometheus, managed databases), so the other end of those edges cannot be checked here.
    """
    if "namespaceSelector" in peer or "ipBlock" in peer:
        return None
    return peer.get("podSelector")


def _rule_ports(rule: dict) -> list[tuple[str, int | str]] | None:
    """(protocol, port) pairs a rule allows; None means every port."""
    if not rule.get("ports"):
        return None
    return [(p.get("protocol", "TCP"), p["port"]) for p in rule["ports"] if "port" in p]


_SIDES = {"Ingress": ("ingress", "from"), "Egress": ("egress", "to")}


def _allows(policies: list[dict], direction: str, pod: dict, peer: dict, dst: dict, protocol: str, port: int) -> bool:
    """Does `pod`'s side allow this connection? (Ingress: from `peer`; Egress: to `peer`.)

    A pod no policy of that direction selects is unrestricted; otherwise policies are
    additive, so one matching rule in any selecting policy is enough.
    """
    rules_key, peers_key = _SIDES[direction]
    selecting = [
        p for p in policies
        if direction in _policy_types(p) and selector_matches(p["spec"].get("podSelector"), _template_labels(pod))
    ]
    if not selecting:
        return True
    for policy in selecting:
        for rule in policy["spec"].get(rules_key) or []:
            ports = _rule_ports(rule)
            if ports is not None and not any(port in _resolve_port(dst, proto, p) for proto, p in ports if proto == protocol):
                continue
            peers = rule.get(peers_key)
            if not peers:
                return True
            for candidate in peers:
                selector = _in_namespace_selector(candidate)
                if selector is not None and selector_matches(selector, _template_labels(peer)):
                    return True
    return False


def check_network_edges(workloads: list[dict], policies: list[dict]) -> list[str]:
    """Every in-namespace edge must be open on both ends, on a port the destination serves.

    Under default deny, saga -> matching needs an egress rule on saga *and* an ingress rule
    on matching. Writing only one half is the classic mistake: the manifests are valid, every
    linter passes, and the connection times out at runtime. Peers that select no workload are
    skipped here: the prod overlay swaps them for ipBlocks with patches.
    """
    errors: set[str] = set()
    for policy in policies:
        selected = [w for w in workloads if selector_matches(policy["spec"].get("podSelector"), _template_labels(w))]
        for direction in _policy_types(policy):
            rules_key, peers_key = _SIDES[direction]
            reverse = "Egress" if direction == "Ingress" else "Ingress"
            for rule in policy["spec"].get(rules_key) or []:
                for peer in rule.get(peers_key) or []:
                    selector = _in_namespace_selector(peer)
                    if selector is None:
                        continue
                    for pod in selected:
                        for other in (w for w in workloads if selector_matches(selector, _template_labels(w))):
                            src, dst = (other, pod) if direction == "Ingress" else (pod, other)
                            if not any(c.get("ports") for c in _pod_spec(dst).get("containers", [])):
                                continue  # e.g. a Job: nothing connects to it
                            ports = _rule_ports(rule)
                            if ports is None:
                                ports = [(p.get("protocol", "TCP"), p["containerPort"])
                                         for c in _pod_spec(dst)["containers"] for p in c.get("ports", [])]
                            for protocol, port in ports:
                                numbers = _resolve_port(dst, protocol, port)
                                if not numbers:
                                    errors.add(f"{_ref(policy)}: allows {protocol}/{port} between {_ref(src)} "
                                               f"and {_ref(dst)}, but {_ref(dst)} does not serve it")
                                    continue
                                far_end = src if reverse == "Egress" else dst
                                near_peer = dst if reverse == "Egress" else src
                                if not any(_allows(policies, reverse, far_end, near_peer, dst, protocol, n) for n in numbers):
                                    errors.add(f"{_ref(policy)}: {_ref(src)} -> {_ref(dst)} on {protocol}/{port} is "
                                               f"open on one end only; no {reverse} rule on {_ref(far_end)} allows it")
    return sorted(errors)


def check(docs: list[dict]) -> list[str]:
    errors: list[str] = []
    for doc in _of_kind(docs, *WORKLOAD_KINDS):
        errors.extend(check_workload(doc))
    errors.extend(check_references(docs))
    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    docs = load_documents(argv)
    errors = check(docs)
    for error in errors:
        print(f"POLICY {error}")
    print(f"policy_check: {len(docs)} objects, {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
