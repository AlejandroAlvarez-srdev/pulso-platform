"""Consistency tests over the real manifests and the compose file."""

import re
import unittest

import yaml

from tests.support import (
    APPS,
    LIVENESS_PATHS,
    READS_PORT_ENV,
    K8S,
    ROOT,
    all_kustomization_dirs,
    generator_literals,
    kustomization,
    load_kustomization,
    policy_check,
    read_yaml,
)

BASE = K8S / "base"
DEV = K8S / "overlays" / "dev"
PROD = K8S / "overlays" / "prod"
KIND_SMOKE = K8S / "ci" / "kind-smoke"


def by_kind_name(docs):
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def container(docs, name):
    return by_kind_name(docs)[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]


class YamlSyntaxTest(unittest.TestCase):
    def test_every_yaml_file_parses(self):
        skip = {".venv", "rendered", "templates"}  # chart templates are Go templates, not YAML
        files = [p for p in ROOT.rglob("*.y*ml") if not skip & set(p.parts)]
        self.assertGreater(len(files), 30)
        for path in files:
            with self.subTest(path=path.relative_to(ROOT)):
                list(yaml.safe_load_all(path.read_text()))


class KustomizationStructureTest(unittest.TestCase):
    def test_referenced_files_exist(self):
        for directory in all_kustomization_dirs():
            k = kustomization(directory)
            paths = list(k.get("resources", []))
            paths += [p["path"] for p in k.get("patches", []) if "path" in p]
            for gen in k.get("configMapGenerator", []) + k.get("secretGenerator", []):
                paths += [f.split("=", 1)[-1] for f in gen.get("files", [])]
                paths += gen.get("envs", [])
            for rel in paths:
                with self.subTest(kustomization=directory.relative_to(ROOT), path=rel):
                    target = directory / rel
                    self.assertTrue(target.exists(), f"{target} does not exist")

    def test_patch_targets_exist(self):
        for overlay in (DEV, PROD, KIND_SMOKE):
            available = by_kind_name(load_kustomization(overlay))
            for patch in kustomization(overlay).get("patches", []):
                if "target" in patch:
                    kind, name = patch["target"]["kind"], patch["target"].get("name")
                    matches = [k for k in available if k[0] == kind and (name is None or k[1] == name)]
                else:
                    smp = read_yaml(overlay / patch["path"])[0]
                    matches = [k for k in available if k == (smp["kind"], smp["metadata"]["name"])]
                with self.subTest(overlay=overlay.name, patch=patch.get("path") or patch["target"]):
                    self.assertTrue(matches, "patch would not apply to anything")

    def test_overlay_images_exist_in_base(self):
        base_images = {
            c["image"].rsplit(":", 1)[0]
            for d in load_kustomization(BASE)
            if d["kind"] in ("Deployment", "Job")
            for c in d["spec"]["template"]["spec"]["containers"]
        }
        self.assertEqual(base_images, {app[0] for app in APPS.values()} | {"ghcr.io/pulso/pulso-orders-api-migrate"})
        for overlay in (DEV, PROD, KIND_SMOKE):
            with self.subTest(overlay=overlay.name):
                self.assertEqual({i["name"] for i in kustomization(overlay)["images"]}, base_images)

    def test_secrets_never_generated_for_prod(self):
        self.assertNotIn("secretGenerator", kustomization(BASE))
        self.assertNotIn("secretGenerator", kustomization(PROD))

    def test_every_referenced_secret_has_an_external_secret_in_prod(self):
        docs = load_kustomization(PROD)
        referenced = set()
        for d in docs:
            if d["kind"] in ("Deployment", "Job"):
                for c in d["spec"]["template"]["spec"]["containers"]:
                    referenced |= {e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e}
                    referenced |= {e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})}
        external = {d["spec"]["target"]["name"] for d in docs if d["kind"] == "ExternalSecret"}
        self.assertEqual(referenced, external)
        dev = {g["name"] for g in kustomization(DEV)["secretGenerator"]}
        self.assertLessEqual(referenced, dev, "dev must generate every Secret prod pulls from the store")

    def test_kind_smoke_uses_only_public_images(self):
        for image in kustomization(KIND_SMOKE)["images"]:
            self.assertFalse(image["newName"].startswith("ghcr.io/pulso"))
            self.assertNotIn(image["newTag"], ("", "latest"))

    def test_kind_smoke_placeholders_listen_on_real_ports(self):
        for patch in kustomization(KIND_SMOKE)["patches"]:
            if patch["target"]["kind"] != "Deployment":
                continue
            name = patch["target"]["name"]
            args = yaml.safe_load(patch["patch"])[0]["value"]
            self.assertIn(f"-listen=:{APPS[name][1]}", args)

    def test_prod_has_no_in_cluster_data_stores(self):
        names = {d["metadata"]["name"] for d in load_kustomization(PROD)}
        self.assertFalse({"postgres", "redis"} & names)


class PolicyOnSourcesTest(unittest.TestCase):
    """The platform policy holds on the unpatched sources (CI re-runs it on rendered output)."""

    def test_prod_sources_pass_policy(self):
        self.assertEqual(policy_check.check(load_kustomization(PROD)), [])

    def test_dev_passes_policy(self):
        self.assertEqual(policy_check.check(load_kustomization(DEV)), [])

    def test_replica_ownership(self):
        docs = load_kustomization(BASE)
        hpa_targets = {d["spec"]["scaleTargetRef"]["name"] for d in docs if d["kind"] == "HorizontalPodAutoscaler"}
        self.assertEqual(hpa_targets, {"orders-api", "matching"})
        # SQLite checkpoints are per pod: support-agents must not be scaled out.
        self.assertEqual(by_kind_name(docs)[("Deployment", "support-agents")]["spec"]["replicas"], 1)
        self.assertNotIn("support-agents", [r["name"] for r in kustomization(PROD)["replicas"]])


class PortContractTest(unittest.TestCase):
    """Ports and health paths match the service repos in compose, Deployments, ConfigMaps, policies and smoke."""

    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        cls.base = load_kustomization(BASE)

    def test_container_ports_and_probes(self):
        for name, (_, port, health) in APPS.items():
            with self.subTest(service=name):
                c = container(self.base, name)
                self.assertEqual(c["ports"][0]["containerPort"], port)
                live = LIVENESS_PATHS.get(name, health)
                self.assertEqual(c["readinessProbe"]["httpGet"]["path"], health)
                self.assertEqual(c["startupProbe"]["httpGet"]["path"], live)
                self.assertEqual(c["livenessProbe"]["httpGet"]["path"], live)
                literals = generator_literals(BASE, f"{name}-config")
                if name in READS_PORT_ENV:
                    self.assertEqual(literals["PORT"], str(port))
                else:
                    self.assertNotIn("PORT", literals, "this image ignores PORT; setting it would mislead")

    def test_orders_api_shutdown_fits_the_grace_period(self):
        spec = by_kind_name(self.base)[("Deployment", "orders-api")]["spec"]["template"]["spec"]
        literals = generator_literals(BASE, "orders-api-config")
        self.assertLess(int(literals["SHUTDOWN_TIMEOUT_MS"]) / 1000, spec["terminationGracePeriodSeconds"])
        self.assertLess(int(literals["SHUTDOWN_DRAIN_DELAY_MS"]), int(literals["SHUTDOWN_TIMEOUT_MS"]))
        self.assertNotIn("lifecycle", spec["containers"][0], "the app drains itself; a preStop would only add delay")

    def test_compose_ports_and_healthchecks(self):
        for name, (_, port, health) in APPS.items():
            with self.subTest(service=name):
                svc = self.compose["services"][name]
                if name in READS_PORT_ENV:
                    self.assertEqual(svc["environment"]["PORT"], str(port))
                self.assertTrue(svc["ports"][0].endswith(f":{port}"))
                self.assertIn(f"127.0.0.1:{port}{health}'", " ".join(svc["healthcheck"]["test"]))

    def test_smoke_script_uses_health_paths(self):
        smoke = (ROOT / "scripts" / "smoke.sh").read_text()
        for name, (_, _, health) in APPS.items():
            self.assertRegex(smoke, rf'"{name}:\d+:{health}"')

    def test_network_policy_ports_match_containers(self):
        port_of = {name: app[1] for name, app in APPS.items()}
        for policy in (d for d in self.base if d["kind"] == "NetworkPolicy"):
            target = policy["spec"]["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name")
            for rule in policy["spec"].get("ingress", []):
                if target in port_of:
                    self.assertEqual([p["port"] for p in rule["ports"]], [port_of[target]], policy["metadata"]["name"])
            for rule in policy["spec"].get("egress", []):
                for peer in rule["to"]:
                    dest = peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
                    if dest in port_of:
                        self.assertEqual([p["port"] for p in rule["ports"]], [port_of[dest]], policy["metadata"]["name"])

    def test_prod_moves_every_ingress_to_the_prod_host(self):
        targets = {p["target"]["name"] for p in kustomization(PROD)["patches"] if p.get("target", {}).get("kind") == "Ingress"}
        ingresses = {d["metadata"]["name"] for d in self.base if d["kind"] == "Ingress"}
        self.assertEqual(targets, ingresses)

    def test_ingress_routes_only_public_services(self):
        ingress = by_kind_name(self.base)[("Ingress", "pulso-public")]
        backends = {p["backend"]["service"]["name"] for r in ingress["spec"]["rules"] for p in r["http"]["paths"]}
        self.assertEqual(backends, {"orders-api", "support-agents"})


class ComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    def test_services_build_from_sibling_repos(self):
        for name in APPS:
            self.assertEqual(self.compose["services"][name]["build"]["context"], f"../pulso-{name}")

    def test_dependencies_wait_for_health_or_completion(self):
        services = self.compose["services"]
        for name, svc in services.items():
            for dep, cond in svc.get("depends_on", {}).items():
                with self.subTest(service=name, dependency=dep):
                    if cond["condition"] == "service_completed_successfully":
                        self.assertEqual(services[dep]["restart"], "no", "a one-shot must not restart")
                    else:
                        self.assertEqual(cond["condition"], "service_healthy")
                        self.assertIn("healthcheck", services[dep])

    def test_orders_api_waits_for_migrations(self):
        services = self.compose["services"]
        self.assertEqual(services["orders-api"]["depends_on"]["orders-migrate"]["condition"], "service_completed_successfully")
        self.assertEqual(services["orders-migrate"]["build"]["target"], "build")

    def test_data_volumes_are_named(self):
        self.assertEqual(set(self.compose["volumes"]), {"postgres-data", "redis-data", "support-data"})

    def test_app_containers_are_hardened(self):
        for name in APPS:
            svc = self.compose["services"][name]
            self.assertTrue(svc["read_only"])
            self.assertEqual(svc["cap_drop"], ["ALL"])

    def test_env_example_covers_interpolated_variables(self):
        text = (ROOT / "docker-compose.yml").read_text()
        used = set(re.findall(r"\$\{([A-Z_]+):-", text))
        documented = {line.split("=", 1)[0] for line in (ROOT / ".env.example").read_text().splitlines() if "=" in line}
        self.assertEqual(used - documented, set())

    def test_postgres_init_script_creates_every_database(self):
        sql = (DEV / "data" / "postgres-init.sql").read_text()
        urls = [v for s in self.compose["services"].values() for k, v in s.get("environment", {}).items() if k == "DATABASE_URL"]
        self.assertTrue(urls)
        for url in urls:
            database = url.rsplit("/", 1)[1].split("?", 1)[0]
            self.assertIn(f"CREATE DATABASE {database};", sql)


if __name__ == "__main__":
    unittest.main()
