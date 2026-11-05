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

    def test_external_secret_provides_the_secret(self):
        docs = good_stack()
        container_of(docs)["envFrom"].append({"secretRef": {"name": "web-secrets"}})
        self.assertViolation(docs, "envFrom Secret 'web-secrets'")
        docs.append({"kind": "ExternalSecret", "metadata": {"name": "web"}, "spec": {"target": {"name": "web-secrets"}}})
        self.assertEqual(policy_check.check(docs), [])

    def test_hpa_may_target_a_rollout_with_workload_ref(self):
        docs = good_stack()
        find(docs, "HorizontalPodAutoscaler")["spec"]["scaleTargetRef"] = {"kind": "Rollout", "name": "web"}
        self.assertViolation(docs, "scaleTargetRef Rollout/web does not exist")
        docs.append({"kind": "Rollout", "metadata": {"name": "web"}, "spec": {"workloadRef": {"kind": "Deployment", "name": "web"}}})
        self.assertEqual(policy_check.check(docs), [])

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


def two_tier_stack() -> list[dict]:
    """good_stack plus an internal `api` Deployment that `web` calls on its named port."""
    docs = good_stack()
    api = copy.deepcopy(find(docs, "Deployment"))
    api["metadata"]["name"] = "api"
    api["spec"]["selector"]["matchLabels"] = {"app.kubernetes.io/name": "api"}
    api["spec"]["template"]["metadata"]["labels"] = {"app.kubernetes.io/name": "api"}
    api["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"name": "grpc", "containerPort": 9000}]
    to_api = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "api"}}}
    from_web = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "web"}}}
    return docs + [
        api,
        {"kind": "PodDisruptionBudget", "metadata": {"name": "api"},
         "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "api"}}}},
        {"kind": "NetworkPolicy", "metadata": {"name": "web-egress"},
         "spec": {"podSelector": from_web["podSelector"], "policyTypes": ["Egress"],
                  "egress": [{"to": [to_api], "ports": [{"protocol": "TCP", "port": 9000}]}]}},
        {"kind": "NetworkPolicy", "metadata": {"name": "api-ingress"},
         "spec": {"podSelector": to_api["podSelector"], "policyTypes": ["Ingress"],
                  "ingress": [{"from": [from_web], "ports": [{"protocol": "TCP", "port": "grpc"}]}]}},
    ]


class NetworkEdgeTest(unittest.TestCase):
    """Under default deny an in-namespace edge needs an egress rule and an ingress rule."""

    def violations(self, docs):
        return [e for e in policy_check.check(docs) if "NetworkPolicy/" in e]

    def test_edge_open_on_both_ends_passes(self):
        # Egress by number, ingress by port name: both resolve to the same container port.
        self.assertEqual(policy_check.check(two_tier_stack()), [])

    def test_egress_without_matching_ingress(self):
        docs = [d for d in two_tier_stack() if d["metadata"]["name"] != "api-ingress"]
        self.assertEqual(self.violations(docs), [
            "NetworkPolicy/web-egress: Deployment/web -> Deployment/api on TCP/9000 is open on one end only; "
            "no Ingress rule on Deployment/api allows it",
        ])

    def test_ingress_without_matching_egress(self):
        docs = [d for d in two_tier_stack() if d["metadata"]["name"] != "web-egress"]
        self.assertEqual(self.violations(docs), [
            "NetworkPolicy/api-ingress: Deployment/web -> Deployment/api on TCP/grpc is open on one end only; "
            "no Egress rule on Deployment/web allows it",
        ])

    def test_halves_on_different_ports(self):
        docs = two_tier_stack()
        find(docs, "NetworkPolicy", "web-egress")["spec"]["egress"][0]["ports"][0]["port"] = 8080
        errors = self.violations(docs)
        self.assertIn(
            "NetworkPolicy/web-egress: allows TCP/8080 between Deployment/web and Deployment/api, "
            "but Deployment/api does not serve it", errors)
        self.assertTrue(any("api-ingress" in e and "no Egress rule on Deployment/web" in e for e in errors), errors)

    def test_protocol_must_match(self):
        docs = two_tier_stack()
        find(docs, "NetworkPolicy", "api-ingress")["spec"]["ingress"][0]["ports"][0]["protocol"] = "UDP"
        self.assertTrue(any("does not serve it" in e for e in self.violations(docs)))

    def test_rule_without_ports_or_peers_allows_everything(self):
        docs = two_tier_stack()
        find(docs, "NetworkPolicy", "api-ingress")["spec"]["ingress"] = [{}]
        self.assertEqual(policy_check.check(docs), [])

    def test_policy_types_default_from_the_rules(self):
        # Without policyTypes, a policy with egress rules is an Egress policy too.
        docs = two_tier_stack()
        del find(docs, "NetworkPolicy", "web-egress")["spec"]["policyTypes"]
        self.assertEqual(policy_check.check(docs), [])

    def test_unrestricted_end_needs_no_rule(self):
        # Without a default-deny for Egress, web's egress is open and only api's ingress matters.
        docs = [d for d in two_tier_stack() if d["metadata"]["name"] != "web-egress"]
        find(docs, "NetworkPolicy", "deny")["spec"]["policyTypes"] = ["Ingress"]
        self.assertEqual(self.violations(docs), [])

    def test_peers_outside_the_namespace_are_not_checked(self):
        docs = two_tier_stack()
        peer = find(docs, "NetworkPolicy", "api-ingress")["spec"]["ingress"][0]["from"][0]
        peer["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}
        # api now only admits ingress-nginx, so web's egress rule is the half left open.
        self.assertEqual(len(self.violations(docs)), 1)
        self.assertIn("no Ingress rule on Deployment/api", self.violations(docs)[0])

    def test_destination_without_ports_is_skipped(self):
        docs = two_tier_stack()
        find(docs, "Deployment", "api")["spec"]["template"]["spec"]["containers"][0].pop("ports")
        self.assertEqual(self.violations(docs), [])


if __name__ == "__main__":
    unittest.main()
