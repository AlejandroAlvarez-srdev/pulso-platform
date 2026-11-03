"""Unit tests for scripts/policy_check.py: each rule must fire on a broken manifest."""

import copy
import unittest

from tests.support import policy_check


def good_stack() -> list[dict]:
    labels = {"app.kubernetes.io/name": "web"}
    container = {
        "name": "web",
        "image": "ghcr.io/pulso/web:1.0.0",
        "ports": [{"name": "http", "containerPort": 8080}],
        "envFrom": [{"configMapRef": {"name": "web-config"}}],
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
        "startupProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    return [
        {"kind": "Namespace", "metadata": {"name": "pulso", "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}},
        {"kind": "ServiceAccount", "metadata": {"name": "web"}},
        {"kind": "ConfigMap", "metadata": {"name": "web-config"}},
        {
            "kind": "Deployment",
            "metadata": {"name": "web"},
            "spec": {
                "strategy": {"type": "RollingUpdate"},
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "serviceAccountName": "web",
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [container],
                    },
                },
            },
        },
        {
            "kind": "Service",
            "metadata": {"name": "web"},
            "spec": {"selector": dict(labels), "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
        },
        {
            "kind": "Ingress",
            "metadata": {"name": "public"},
            "spec": {"rules": [{"http": {"paths": [{"backend": {"service": {"name": "web", "port": {"name": "http"}}}}]}}]},
        },
        {
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "web"},
            "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "minReplicas": 2, "maxReplicas": 4},
        },
        {"kind": "PodDisruptionBudget", "metadata": {"name": "web"}, "spec": {"maxUnavailable": 1, "selector": {"matchLabels": labels}}},
        {"kind": "NetworkPolicy", "metadata": {"name": "deny"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        {
            "kind": "NetworkPolicy",
            "metadata": {"name": "web-ingress"},
            "spec": {"podSelector": {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["web"]}]}},
        },
    ]


def find(docs, kind, name="web"):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def container_of(docs):
    return find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]


class PolicyCheckTest(unittest.TestCase):
    def assertViolation(self, docs, fragment):
        errors = policy_check.check(docs)
        self.assertTrue(any(fragment in e for e in errors), f"expected '{fragment}' in {errors}")

    def test_good_stack_passes(self):
        self.assertEqual(policy_check.check(good_stack()), [])

    def test_latest_and_untagged_images_are_rejected(self):
        for image in ("ghcr.io/pulso/web:latest", "ghcr.io/pulso/web", "localhost:5000/web"):
            docs = good_stack()
            container_of(docs)["image"] = image
            self.assertViolation(docs, "immutable tag")

    def test_digest_pinned_image_is_accepted(self):
        docs = good_stack()
        container_of(docs)["image"] = "ghcr.io/pulso/web@sha256:" + "a" * 64
        self.assertEqual(policy_check.check(docs), [])

    def test_missing_memory_limit(self):
        docs = good_stack()
        del container_of(docs)["resources"]["limits"]["memory"]
        self.assertViolation(docs, "resources.limits.memory")

    def test_missing_startup_probe_on_deployment(self):
        docs = good_stack()
        del container_of(docs)["startupProbe"]
        self.assertViolation(docs, "missing startupProbe")

    def test_writable_root_filesystem(self):
        docs = good_stack()
        container_of(docs)["securityContext"]["readOnlyRootFilesystem"] = False
        self.assertViolation(docs, "readOnlyRootFilesystem")

    def test_root_pod(self):
        docs = good_stack()
        find(docs, "Deployment")["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] = False
        self.assertViolation(docs, "runAsNonRoot")

    def test_capabilities_not_dropped(self):
        docs = good_stack()
        container_of(docs)["securityContext"]["capabilities"] = {"drop": ["NET_RAW"]}
        self.assertViolation(docs, "capabilities.drop")

    def test_hpa_target_with_fixed_replicas(self):
        docs = good_stack()
        find(docs, "Deployment")["spec"]["replicas"] = 3
        self.assertViolation(docs, "sets spec.replicas")

    def test_hpa_unknown_target(self):
        docs = good_stack()
        find(docs, "HorizontalPodAutoscaler")["spec"]["scaleTargetRef"]["name"] = "ghost"
        self.assertViolation(docs, "scaleTargetRef Deployment/ghost does not exist")

    def test_service_selector_without_pods(self):
        docs = good_stack()
        find(docs, "Service")["spec"]["selector"] = {"app.kubernetes.io/name": "typo"}
        self.assertViolation(docs, "matches no workload")

    def test_service_unknown_named_target_port(self):
        docs = good_stack()
        find(docs, "Service")["spec"]["ports"][0]["targetPort"] = "metrics"
        self.assertViolation(docs, "targetPort 'metrics'")

    def test_ingress_backend_missing(self):
        docs = good_stack()
        find(docs, "Ingress", "public")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] = "nope"
        self.assertViolation(docs, "backend Service 'nope'")

    def test_deployment_without_pdb(self):
        docs = [d for d in good_stack() if d["kind"] != "PodDisruptionBudget"]
        self.assertViolation(docs, "no PodDisruptionBudget")

    def test_pdb_that_blocks_drains(self):
        docs = good_stack()
        find(docs, "PodDisruptionBudget")["spec"]["maxUnavailable"] = 0
        self.assertViolation(docs, "blocks every node drain")

    def test_default_deny_required(self):
        docs = [d for d in good_stack() if d["metadata"]["name"] != "deny"]
        self.assertViolation(docs, "default-deny")

    def test_dangling_network_policy(self):
        docs = good_stack()
        find(docs, "NetworkPolicy", "web-ingress")["spec"]["podSelector"]["matchExpressions"][0]["values"] = ["old"]
        self.assertViolation(docs, "podSelector matches no workload")

    def test_missing_config_and_service_account(self):
        docs = [d for d in good_stack() if d["kind"] not in ("ConfigMap", "ServiceAccount")]
        self.assertViolation(docs, "envFrom ConfigMap 'web-config'")
        self.assertViolation(docs, "serviceAccountName 'web'")

    def test_namespace_must_enforce_restricted(self):
        docs = good_stack()
        find(docs, "Namespace", "pulso")["metadata"]["labels"] = {}
        self.assertViolation(docs, "restricted")

    def test_job_needs_ttl_but_no_probes(self):
        docs = good_stack()
        job = copy.deepcopy(find(docs, "Deployment"))
        job["kind"], job["metadata"]["name"] = "Job", "migrate"
        del job["spec"]["strategy"]
        job["spec"]["ttlSecondsAfterFinished"] = 300
        container = job["spec"]["template"]["spec"]["containers"][0]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            del container[probe]
        self.assertEqual(policy_check.check(docs + [job]), [])
        del job["spec"]["ttlSecondsAfterFinished"]
        self.assertViolation(docs + [job], "ttlSecondsAfterFinished")

    def test_secret_key_ref_must_exist(self):
        docs = good_stack()
        container_of(docs)["env"] = [{"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "db", "key": "url"}}}]
        self.assertViolation(docs, "references missing Secret 'db'")

    def test_selector_operators(self):
        labels = {"tier": "api"}
        self.assertTrue(policy_check.selector_matches({"matchExpressions": [{"key": "tier", "operator": "Exists"}]}, labels))
        self.assertFalse(policy_check.selector_matches({"matchExpressions": [{"key": "tier", "operator": "DoesNotExist"}]}, labels))
        self.assertFalse(
            policy_check.selector_matches({"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["api"]}]}, labels)
        )
        self.assertFalse(policy_check.selector_matches(None, labels))

    def test_input_is_not_mutated(self):
        docs = good_stack()
        snapshot = copy.deepcopy(docs)
        policy_check.check(docs)
        self.assertEqual(docs, snapshot)


if __name__ == "__main__":
    unittest.main()
