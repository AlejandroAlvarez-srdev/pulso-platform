"""The Helm chart must stay equivalent to the Kustomize base and overlays.

helm itself runs in CI (lint + template + the same validators as the overlays); these tests
pin the values files to the manifests so the two deployment paths cannot drift apart.
"""

import copy
import unittest

from tests.support import APPS, K8S, LIVENESS_PATHS, METRICS, ROOT, generator_literals, kustomization, load_kustomization, read_yaml

CHART = ROOT / "charts" / "pulso"
BASE = K8S / "base"
DEV = K8S / "overlays" / "dev"
PROD = K8S / "overlays" / "prod"


def merge(base: dict, override: dict) -> dict:
    """Helm's values merge: maps merge recursively, everything else is replaced."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def values(env: str | None = None) -> dict:
    result = read_yaml(CHART / "values.yaml")[0]
    return merge(result, read_yaml(CHART / f"values-{env}.yaml")[0]) if env else result


def deployment(docs, name):
    return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)


def overlay_literals(directory, name):
    for gen in kustomization(directory).get("configMapGenerator", []):
        if gen["name"] == name:
            return dict(item.split("=", 1) for item in gen.get("literals", []))
    return {}


class HelmChartParityTest(unittest.TestCase):
    def test_chart_metadata(self):
        chart = read_yaml(CHART / "Chart.yaml")[0]
        self.assertEqual(chart["apiVersion"], "v2")
        self.assertEqual(chart["name"], "pulso")

    def test_same_services_ports_and_health(self):
        services = values()["services"]
        self.assertEqual(set(services), set(APPS))
        for name, (image, port, health) in APPS.items():
            with self.subTest(service=name):
                self.assertEqual(f"{values()['image']['registry']}/{services[name]['image']}", image)
                self.assertEqual(services[name]["port"], port)
                self.assertEqual(services[name]["readinessPath"], health)
                self.assertEqual(services[name]["livenessPath"], LIVENESS_PATHS.get(name, health))

    def test_shutdown_settings_match_base(self):
        docs = load_kustomization(BASE)
        for name, svc in values()["services"].items():
            with self.subTest(service=name):
                spec = deployment(docs, name)["spec"]["template"]["spec"]
                self.assertEqual(svc["terminationGracePeriodSeconds"], spec["terminationGracePeriodSeconds"])
                pre_stop = spec["containers"][0].get("lifecycle", {}).get("preStop", {}).get("sleep", {}).get("seconds", 0)
                self.assertEqual(svc["preStopSleepSeconds"], pre_stop)

    def test_resources_match_base_and_prod(self):
        base_docs = load_kustomization(BASE)
        for name, svc in values()["services"].items():
            container = deployment(base_docs, name)["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(svc["resources"], container["resources"], name)
        prod = values("prod")["services"]
        for name in APPS:
            patch = read_yaml(PROD / "patches" / f"{name}.yaml")[0]
            self.assertEqual(prod[name]["resources"], patch["spec"]["template"]["spec"]["containers"][0]["resources"], name)

    def test_env_matches_configmaps(self):
        for env, overlay in ((None, None), ("dev", DEV), ("prod", PROD)):
            chart = values(env)["services"]
            for name in APPS:
                with self.subTest(env=env or "base", service=name):
                    expected = generator_literals(BASE, f"{name}-config")
                    if overlay:
                        expected.update(overlay_literals(overlay, f"{name}-config"))
                    self.assertEqual(chart[name]["env"], expected)

    def test_autoscaling_matches_hpas(self):
        base_hpas = {d["metadata"]["name"]: d for d in load_kustomization(BASE) if d["kind"] == "HorizontalPodAutoscaler"}
        for env, bounds in ((None, None), ("dev", (1, 2)), ("prod", (3, 12))):
            for name, svc in values(env)["services"].items():
                with self.subTest(env=env or "base", service=name):
                    self.assertEqual(svc["autoscaling"]["enabled"], name in base_hpas)
                    if name in base_hpas:
                        expected = bounds or (base_hpas[name]["spec"]["minReplicas"], base_hpas[name]["spec"]["maxReplicas"])
                        self.assertEqual((svc["autoscaling"]["minReplicas"], svc["autoscaling"]["maxReplicas"]), expected)

    def test_prod_image_tags_match_overlay(self):
        tags = {i["name"].rsplit("/", 1)[1]: i["newTag"] for i in kustomization(PROD)["images"]}
        for name, svc in values("prod")["services"].items():
            self.assertEqual(svc["tag"], tags[svc["image"]], name)
        self.assertEqual(values("dev")["image"]["tag"], "dev")

    def test_secret_keys_and_dev_values_match_overlay(self):
        dev = values("dev")
        self.assertEqual(dev["secrets"]["mode"], "generated")
        self.assertEqual(values("prod")["secrets"]["mode"], "external")
        generators = {g["name"]: g for g in kustomization(DEV)["secretGenerator"]}
        for name, svc in dev["services"].items():
            literals = dict(item.split("=", 1) for item in generators.get(f"{name}-secrets", {}).get("literals", []))
            with self.subTest(service=name):
                self.assertEqual(sorted(svc["secretKeys"]), sorted(literals))
                if literals:
                    self.assertEqual(dev["secrets"]["generated"][name], literals)

    def test_external_secret_keys_match_prod_overlay(self):
        for name, svc in values("prod")["services"].items():
            path = PROD / "external-secrets" / f"{name}.yaml"
            if not svc["secretKeys"]:
                self.assertFalse(path.exists())
                continue
            external = read_yaml(path)[0]
            self.assertEqual([d["secretKey"] for d in external["spec"]["data"]], svc["secretKeys"], name)

    def test_network_policy_flags_match_base(self):
        policies = {d["metadata"]["name"]: d for d in load_kustomization(BASE) if d["kind"] == "NetworkPolicy"}
        services = values()["services"]
        self.assertTrue(services["orders-api"]["networkPolicy"]["postgres"] and "orders-api-postgres-egress" in policies)
        self.assertTrue(services["saga"]["networkPolicy"]["redis"] and "saga-redis-egress" in policies)
        self.assertEqual(services["matching"]["networkPolicy"]["allowFrom"], ["saga"])
        public = {n for n, s in services.items() if s["networkPolicy"]["public"]}
        self.assertEqual(public, {n for n, s in services.items() if s["ingressPath"]})
        self.assertEqual(public, {"orders-api", "support-agents"})

    def test_metrics_flags_match_the_exporters(self):
        self.assertEqual({n for n, s in values()["services"].items() if s["metrics"]}, set(METRICS))

    def test_dev_runs_data_stores_in_cluster_prod_does_not(self):
        self.assertTrue(values("dev")["dataStores"]["inCluster"])
        self.assertFalse(values("prod")["dataStores"]["inCluster"])
        sql = (DEV / "data" / "postgres-init.sql").read_text()
        for database in values("dev")["dataStores"]["postgres"]["databases"]:
            self.assertIn(f"CREATE DATABASE {database};", sql)


if __name__ == "__main__":
    unittest.main()
