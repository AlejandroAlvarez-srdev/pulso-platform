"""Monitoring objects agree with the services and with the metrics contract."""

import json
import re
import unittest

from tests.support import APPS, K8S, ROOT, load_kustomization, read_yaml

OBS = K8S / "observability"
CONTRACT = {"http_requests_total", "http_request_duration_seconds_bucket"}
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
        self.assertIn("rate(http_requests_total", exprs)
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
    def test_service_monitor_selects_every_service_on_http(self):
        monitor = read_yaml(OBS / "service-monitors.yaml")[0]
        values = monitor["spec"]["selector"]["matchExpressions"][0]["values"]
        self.assertEqual(set(values), set(APPS))
        self.assertEqual([e["port"] for e in monitor["spec"]["endpoints"]], ["http"])
        self.assertEqual(monitor["spec"]["endpoints"][0]["path"], "/metrics")
        services = {d["metadata"]["name"]: d for d in load_kustomization(K8S / "base") if d["kind"] == "Service"}
        for name in APPS:
            self.assertIn("http", [p["name"] for p in services[name]["spec"]["ports"]])

    def test_network_policy_lets_prometheus_reach_every_port(self):
        policy = next(d for d in load_kustomization(K8S / "base") if d["metadata"]["name"] == "allow-prometheus-scrape")
        ports = {p["port"] for p in policy["spec"]["ingress"][0]["ports"]}
        self.assertEqual(ports, {app[1] for app in APPS.values()})
        self.assertEqual(set(policy["spec"]["podSelector"]["matchExpressions"][0]["values"]), set(APPS))

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

    def test_alerts_have_severity_routed_by_alertmanager(self):
        routed = {"page", "ticket"}
        for group in rules():
            for rule in group["rules"]:
                if "alert" in rule:
                    self.assertIn(rule["labels"]["severity"], routed, rule["alert"])
                    self.assertIn("summary", rule["annotations"])


if __name__ == "__main__":
    unittest.main()
