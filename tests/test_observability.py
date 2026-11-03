"""Monitoring objects agree with the services and with the metrics contract."""

import json
import re
import unittest

from tests.support import APPS, K8S, METRICS, ROOT, load_kustomization, read_yaml

OBS = K8S / "observability"
CONTRACT = {"http_request_duration_seconds_count", "http_request_duration_seconds_bucket"}
# Series that come from Prometheus itself or kube-state-metrics, not from the services.
PLATFORM = {
    "up",
    "kube_pod_container_status_restarts_total",
    "kube_pod_container_status_last_terminated_reason",
    "kube_horizontalpodautoscaler_status_current_replicas",
    "kube_horizontalpodautoscaler_spec_max_replicas",
}
PROMQL_WORDS = {"sum", "by", "rate", "increase", "histogram_quantile", "and", "on", "le", "job", "route", "status",
                "pod", "namespace", "container", "count"}


def metric_names(expr: str) -> set[str]:
    """Identifiers used as metric selectors (followed by '{' or '[')."""
    return set(re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*[{\[]", expr)) - PROMQL_WORDS


def rules():
    return read_yaml(OBS / "prometheus-rules.yaml")[0]["spec"]["groups"]


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads((OBS / "dashboards" / "pulso-red.json").read_text())

    def test_identity_and_unique_panels(self):
        self.assertEqual(self.dashboard["uid"], "pulso-red")
        ids = [p["id"] for p in self.dashboard["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_covers_rate_errors_duration(self):
        exprs = " ".join(t["expr"] for p in self.dashboard["panels"] for t in p["targets"])
        self.assertIn("rate(http_request_duration_seconds_count", exprs)
        self.assertIn('status=~"5.."', exprs)
        self.assertIn("histogram_quantile(0.95", exprs)

    def test_queries_only_use_known_metrics(self):
        for panel in self.dashboard["panels"]:
            for target in panel["targets"]:
                with self.subTest(panel=panel["title"]):
                    self.assertLessEqual(metric_names(target["expr"]), CONTRACT | PLATFORM)

    def test_shipped_as_sidecar_configmap(self):
        k = read_yaml(OBS / "kustomization.yaml")[0]
        gen = k["configMapGenerator"][0]
        self.assertEqual(gen["options"]["labels"], {"grafana_dashboard": "1"})
        self.assertIn("dashboards/pulso-red.json", gen["files"])


class ScrapeConfigTest(unittest.TestCase):
    def test_monitors_only_for_services_that_serve_metrics(self):
        monitors = {m["metadata"]["name"]: m for m in read_yaml(OBS / "service-monitors.yaml")}
        self.assertEqual(set(monitors), set(METRICS))
        services = {d["metadata"]["name"]: d for d in load_kustomization(K8S / "base") if d["kind"] == "Service"}
        for name, monitor in monitors.items():
            with self.subTest(service=name):
                self.assertEqual(monitor["spec"]["selector"]["matchLabels"], {"app.kubernetes.io/name": name})
                endpoint = monitor["spec"]["endpoints"][0]
                self.assertEqual((endpoint["port"], endpoint["path"]), ("http", "/metrics"))
                self.assertIn("http", [p["name"] for p in services[name]["spec"]["ports"]])

    def test_relabelings_normalise_to_the_contract(self):
        monitors = {m["metadata"]["name"]: m for m in read_yaml(OBS / "service-monitors.yaml")}
        for name, (histogram, status_label) in METRICS.items():
            relabelings = monitors[name]["spec"]["endpoints"][0].get("metricRelabelings", [])
            with self.subTest(service=name):
                if histogram != "http_request_duration_seconds":
                    rename = next(r for r in relabelings if r.get("targetLabel") == "__name__")
                    self.assertTrue(re.fullmatch(rename["regex"], f"{histogram}_count"))
                    self.assertEqual(re.sub(rename["regex"], rename["replacement"].replace("$1", r"\1"), f"{histogram}_count"),
                                     "http_request_duration_seconds_count")
                if status_label != "status":
                    copy = next(r for r in relabelings if r.get("targetLabel") == "status")
                    self.assertEqual(copy["sourceLabels"], [status_label])
                    self.assertTrue(any(r["action"] == "labeldrop" and r["regex"] == status_label for r in relabelings))

    def test_network_policy_lets_prometheus_reach_exporters_only(self):
        policy = next(d for d in load_kustomization(K8S / "base") if d["metadata"]["name"] == "allow-prometheus-scrape")
        ports = {p["port"] for p in policy["spec"]["ingress"][0]["ports"]}
        self.assertEqual(ports, {APPS[name][1] for name in METRICS})
        self.assertEqual(set(policy["spec"]["podSelector"]["matchExpressions"][0]["values"]), set(METRICS))

    def test_public_routes_exclude_metrics(self):
        ingresses = {d["metadata"]["name"]: d for d in load_kustomization(K8S / "base") if d["kind"] == "Ingress"}
        self.assertEqual(set(ingresses), {"pulso-public"}, "a second Ingress on the host leaks its annotations")
        paths = {p["backend"]["service"]["name"]: p["path"] for p in ingresses["pulso-public"]["spec"]["rules"][0]["http"]["paths"]}
        for name, path in paths.items():
            with self.subTest(service=name):
                prefix = path.split("(")[0]
                route = re.compile(path)
                self.assertEqual(route.fullmatch(f"{prefix}/health").group(2), "health")
                self.assertEqual(route.fullmatch(f"{prefix}").group(2), "")
                if name in METRICS:
                    self.assertIn("(?!metrics(?:/|$))", path)
                    for blocked in (f"{prefix}/metrics", f"{prefix}/metrics/", f"{prefix}/metrics/x"):
                        self.assertIsNone(route.fullmatch(blocked), blocked)
                    self.assertEqual(route.fullmatch(f"{prefix}/metricsfoo").group(2), "metricsfoo")

    def test_chart_route_uses_the_same_lookahead(self):
        template = (ROOT / "charts" / "pulso" / "templates" / "ingress.yaml").read_text()
        self.assertIn('printf "%s(/|$)(?!metrics(?:/|$))(.*)"', template)
        self.assertNotIn("blocked", template)

    def test_stack_selects_monitors_from_all_namespaces(self):
        values = read_yaml(ROOT / "observability" / "kube-prometheus-stack" / "values.yaml")[0]
        spec = values["prometheus"]["prometheusSpec"]
        self.assertFalse(spec["serviceMonitorSelectorNilUsesHelmValues"])
        self.assertFalse(spec["ruleSelectorNilUsesHelmValues"])
        self.assertEqual(values["grafana"]["sidecar"]["dashboards"]["label"], "grafana_dashboard")
        self.assertNotIn("adminPassword", values["grafana"])


class AlertRulesTest(unittest.TestCase):
    def test_expressions_use_known_metrics_or_recordings(self):
        recorded = {r["record"] for g in rules() for r in g["rules"] if "record" in r}
        for group in rules():
            for rule in group["rules"]:
                with self.subTest(rule=rule.get("alert") or rule.get("record")):
                    used = metric_names(rule["expr"]) | set(re.findall(r"pulso:[a-z_:0-9]+", rule["expr"]))
                    self.assertLessEqual(used, CONTRACT | PLATFORM | recorded)

    def test_runbook_links_resolve(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text()
        anchors = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in re.findall(r"^#+ (.+)$", runbook, re.M)}
        for group in rules():
            for rule in group["rules"]:
                url = rule.get("annotations", {}).get("runbook_url")
                if url:
                    with self.subTest(alert=rule["alert"]):
                        self.assertTrue(url.split("#")[0].endswith("docs/RUNBOOK.md"))
                        self.assertIn(url.split("#")[1], anchors)

    def test_alerts_have_severity_routed_by_alertmanager(self):
        routed = {"page", "ticket"}
        for group in rules():
            for rule in group["rules"]:
                if "alert" in rule:
                    self.assertIn(rule["labels"]["severity"], routed, rule["alert"])
                    self.assertIn("summary", rule["annotations"])


if __name__ == "__main__":
    unittest.main()
