"""The orders-api canary component wires into objects that exist in base/observability."""

import unittest

from tests.support import K8S, load_kustomization, policy_check, read_yaml

COMPONENT = K8S / "components" / "orders-api-canary"


def component_docs():
    return {(d["kind"], d["metadata"]["name"]): d for p in sorted(COMPONENT.glob("*.yaml")) if p.name != "kustomization.yaml" for d in read_yaml(p)}


class CanaryComponentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = {(d["kind"], d["metadata"]["name"]): d for d in load_kustomization(K8S / "base")}
        cls.docs = component_docs()
        cls.rollout = cls.docs[("Rollout", "orders-api")]

    def test_rollout_adopts_the_base_deployment(self):
        ref = self.rollout["spec"]["workloadRef"]
        deployment = self.base[(ref["kind"], ref["name"])]
        self.assertEqual(self.rollout["spec"]["selector"], deployment["spec"]["selector"])
        self.assertNotIn("replicas", deployment["spec"], "the HPA (via the Rollout) owns the count")

    def test_services_and_ingress_exist(self):
        canary = self.rollout["spec"]["strategy"]["canary"]
        stable = self.base[("Service", canary["stableService"])]
        self.assertEqual(self.docs[("Service", canary["canaryService"])]["spec"]["selector"], stable["spec"]["selector"])
        self.assertIn(("Ingress", canary["trafficRouting"]["nginx"]["stableIngress"]), self.base)

    def test_analysis_template_and_metric_labels(self):
        canary = self.rollout["spec"]["strategy"]["canary"]
        name = canary["analysis"]["templates"][0]["templateName"]
        template = self.docs[("AnalysisTemplate", name)]
        self.assertEqual([a["name"] for a in template["spec"]["args"]], [a["name"] for a in canary["analysis"]["args"]])
        query = template["spec"]["metrics"][0]["provider"]["prometheus"]["query"]
        self.assertIn("rollouts_pod_template_hash", query)
        self.assertIn("http_request_duration_seconds_count", query)
        monitors = {m["metadata"]["name"]: m for m in read_yaml(K8S / "observability" / "service-monitors.yaml")}
        self.assertIn("rollouts-pod-template-hash", monitors["orders-api"]["spec"]["podTargetLabels"])

    def test_steps_end_with_manual_promotion(self):
        steps = self.rollout["spec"]["strategy"]["canary"]["steps"]
        weights = [s["setWeight"] for s in steps if "setWeight" in s]
        self.assertEqual(weights, sorted(weights))
        self.assertEqual(steps[-1], {"pause": {}})

    def test_policy_holds_with_the_component(self):
        docs = load_kustomization(K8S / "overlays" / "prod") + list(self.docs.values())
        for doc in docs:
            if doc["kind"] == "HorizontalPodAutoscaler" and doc["metadata"]["name"] == "orders-api":
                doc["spec"]["scaleTargetRef"] = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "name": "orders-api"}
        self.assertEqual(policy_check.check(docs), [])

    def test_example_overlay_uses_the_component(self):
        example = read_yaml(K8S / "examples" / "prod-canary" / "kustomization.yaml")[0]
        self.assertEqual(example["components"], ["../../components/orders-api-canary"])
        self.assertEqual(example["resources"], ["../../overlays/prod"])


if __name__ == "__main__":
    unittest.main()
