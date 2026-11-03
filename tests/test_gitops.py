"""Argo CD Applications point at real paths and follow the promotion rules."""

import unittest

from tests.support import K8S, ROOT, read_yaml

ARGO = ROOT / "argocd"
REPO = "https://github.com/pulso/pulso-platform.git"


def applications():
    docs = [d for p in sorted((ARGO / "apps").glob("*.yaml")) for d in read_yaml(p)]
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "Application"}


def project():
    return read_yaml(ARGO / "apps" / "project.yaml")[0]


def destination_key(dest):
    return (dest.get("server") or dest.get("name"), dest["namespace"])


class GitOpsTest(unittest.TestCase):
    def test_root_manages_the_apps_directory(self):
        root = read_yaml(ARGO / "root.yaml")[0]
        self.assertEqual(root["spec"]["source"]["path"], "argocd/apps")
        self.assertTrue(root["spec"]["syncPolicy"]["automated"]["selfHeal"])

    def test_repo_paths_exist_and_are_kustomizations(self):
        for name, app in applications().items():
            sources = app["spec"].get("sources") or [app["spec"]["source"]]
            for source in sources:
                if source["repoURL"] == REPO and "path" in source:
                    with self.subTest(app=name):
                        self.assertTrue((ROOT / source["path"] / "kustomization.yaml").is_file())

    def test_value_files_from_this_repo_exist(self):
        for name, app in applications().items():
            for source in app["spec"].get("sources", []):
                for value_file in source.get("helm", {}).get("valueFiles", []):
                    self.assertTrue((ROOT / value_file.removeprefix("$values/")).is_file(), name)

    def test_every_app_is_allowed_by_the_project(self):
        proj = project()
        allowed = {destination_key(d) for d in proj["spec"]["destinations"]}
        for name, app in applications().items():
            with self.subTest(app=name):
                self.assertEqual(app["spec"]["project"], "pulso")
                self.assertIn(destination_key(app["spec"]["destination"]), allowed)
                for source in app["spec"].get("sources") or [app["spec"]["source"]]:
                    self.assertIn(source["repoURL"], proj["spec"]["sourceRepos"])

    def test_promotion_rules(self):
        apps = applications()
        dev, prod = apps["pulso-dev"], apps["pulso-prod"]
        self.assertEqual(dev["spec"]["source"]["targetRevision"], "main")
        self.assertEqual(prod["spec"]["source"]["targetRevision"], "prod")
        self.assertTrue(dev["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertFalse(prod["spec"]["syncPolicy"]["automated"]["prune"], "prod deletions need a reviewed sync")
        self.assertTrue(prod["spec"]["syncPolicy"]["automated"]["selfHeal"])
        windows = [w for w in project()["spec"]["syncWindows"] if "pulso-prod" in w["applications"]]
        self.assertTrue(windows and all(w["manualSync"] for w in windows))

    def test_monitoring_syncs_first_and_server_side(self):
        monitoring = applications()["monitoring"]
        self.assertEqual(monitoring["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-1")
        self.assertIn("ServerSideApply=true", monitoring["spec"]["syncPolicy"]["syncOptions"])

    def test_sync_waves_order_secrets_migration_services(self):
        job = read_yaml(K8S / "base" / "orders-api" / "migrate-job.yaml")[0]["metadata"]["annotations"]
        self.assertEqual(job["argocd.argoproj.io/hook"], "Sync")
        self.assertEqual(job["argocd.argoproj.io/sync-wave"], "-1")
        for path in sorted((K8S / "overlays" / "prod" / "external-secrets").glob("*.yaml")):
            for doc in read_yaml(path):
                if doc["kind"] == "ExternalSecret":
                    self.assertLess(int(doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]), -1, path.name)
        dev = read_yaml(K8S / "overlays" / "dev" / "kustomization.yaml")[0]
        self.assertEqual(dev["generatorOptions"]["annotations"]["argocd.argoproj.io/sync-wave"], "-2")


if __name__ == "__main__":
    unittest.main()
