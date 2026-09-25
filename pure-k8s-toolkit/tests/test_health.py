import unittest
from unittest import mock

from ptk import probes
from ptk.health import HealthChecker
from tests import incident


def reachable(url="", **kw):
    body = '{"status": "healthy", "components": [{"name": "core", "status": "healthy"}]}' \
        if "/api/v2.0/health" in str(url) else "pong"
    return {"reachable": True, "status": 200, "body": body, "error": "", "tls_untrusted": False,
            "latency_ms": 3.0, "addresses": []}


def tcp_ok(host, port, timeout=3):
    return {"reachable": True, "error": "", "latency_ms": 1.0}


def run(fixed=False, https=reachable, tcp=tcp_ok):
    kube, etcd = incident.build(fixed)
    hc = HealthChecker(incident.CONFIG, kube=kube, etcd_request=etcd)
    with mock.patch.object(probes, "https", side_effect=https), mock.patch.object(probes, "tcp", side_effect=tcp):
        hc.run()
    return hc, {(c.area, c.name): c for c in hc.checks}


class IncidentTest(unittest.TestCase):
    """The failure chain from the context pack, as the health checker should see it."""

    @classmethod
    def setUpClass(cls):
        cls.hc, cls.c = run()

    def test_overall_red_and_areas(self):
        areas = self.hc.areas()
        self.assertEqual(self.hc.report()["overall"], "red")
        for area in ("etcd", "cilium", "roles", "rancher"):
            self.assertEqual(areas[area], "red", area)
        # 7/16 ingress controllers still serve Rancher's frontend: degraded, not down.
        self.assertEqual(areas["ingress"], "yellow")
        self.assertEqual(areas["haproxy"], "green")

    def test_w08_is_an_unexpected_etcd_member_with_zero_tolerance(self):
        m = self.c[("etcd", "membership")]
        self.assertEqual(m.status, "fail")
        self.assertIn("911dd1653e70620", m.message)
        self.assertIn("w08.morpheus.net", m.message)
        self.assertIn("known issue", m.message)
        self.assertIn("no failure tolerance", self.c[("etcd", "quorum")].message)
        self.assertEqual(self.hc.facts["etcd"]["failure_tolerance"], 0)
        self.assertEqual(self.c[("etcd", "leader")].status, "ok")

    def test_cilium_seven_of_sixteen(self):
        a = self.c[("cilium", "agents")]
        self.assertEqual(a.status, "fail")
        self.assertIn("desired 16, ready 7", a.message)
        missing = a.message.split("no ready agent on: ")[1].split(";")[0].split(", ")
        self.assertEqual(missing, [f"w{i:02d}" for i in range(5, 14)])  # c01-c03 and w01-w04 are the 7 ready

    def test_canal_installer_pod_is_flagged(self):
        c = self.c[("cilium", "cni_conflicts")]
        self.assertEqual(c.status, "fail")
        self.assertIn("helm-install-rke2-canal", c.message)

    def test_per_node_service_routing_from_node_check(self):
        c = self.c[("cilium", "pod_service_routes")]
        self.assertEqual(c.status, "fail")
        self.assertIn("w13", c.message)

    def test_rancher_webhook_chain(self):
        w = self.c[("rancher", "webhook")]
        self.assertEqual(w.status, "fail")
        self.assertIn("no ready endpoints", w.message)
        self.assertIn("w13:NotReady", w.message)
        self.assertIn("10.43.0.1/version", w.message)
        e = self.c[("rancher", "events")]
        self.assertEqual(e.status, "fail")
        self.assertIn("no endpoints available", e.message)
        self.assertEqual(self.c[("rancher", "rancher")].status, "fail")

    def test_roles_flag_w08(self):
        r = self.c[("roles", "node_roles")]
        self.assertEqual(r.status, "fail")
        self.assertIn("w08: worker is an etcd member (mode agent)", r.message)
        self.assertIn("w08: worker labelled control-plane,etcd", r.message)

    def test_api_distinguishes_apiserver_from_service_routing(self):
        self.assertEqual(self.c[("api", "control_plane_apiservers")].status, "ok")
        self.assertEqual(self.c[("api", "kubernetes_endpoints")].status, "ok")

    def test_metrics(self):
        text = self.hc.metrics().render()
        self.assertIn("ptk_etcd_members 4", text)
        self.assertIn("ptk_etcd_unexpected_members 1", text)
        self.assertIn("ptk_etcd_failure_tolerance 0", text)
        self.assertIn("ptk_cilium_agents_ready 7", text)
        self.assertIn('ptk_health_area_status{area="rancher"} 2', text)
        self.assertIn('ptk_etcd_member_healthy{expected="false",host="w08.morpheus.net",member_id="911dd1653e70620"} 0', text)

    def test_summary_text(self):
        s = self.hc.summary()
        self.assertTrue(s.startswith("morpheus-net-rke2: RED"))
        self.assertIn("RED     etcd", s)


class RecoveredTest(unittest.TestCase):
    def test_all_green_after_recovery(self):
        hc, c = run(fixed=True)
        bad = [x.as_dict() for x in hc.checks if x.status not in ("ok", "skip")]
        self.assertEqual(bad, [])
        self.assertEqual(hc.report()["overall"], "green")
        self.assertEqual(hc.facts["etcd"]["failure_tolerance"], 1)


class ProbeFailureTest(unittest.TestCase):
    def test_clusterip_unreachable_but_apiservers_up(self):
        def https(url, **kw):
            if "10.43.0.1" in url:
                return {"reachable": False, "status": 0, "body": "", "error": "timed out", "tls_untrusted": False, "latency_ms": 1000}
            return reachable(url)
        hc, c = run(fixed=True, https=https)
        self.assertEqual(c[("api", "clusterip_from_this_host")].status, "fail")
        self.assertIn("API server may be up", c[("api", "clusterip_from_this_host")].message)
        self.assertEqual(c[("api", "control_plane_apiservers")].status, "ok")

    def test_one_crashing_check_does_not_stop_others(self):
        kube, etcd = incident.build(True)
        del kube.objs["/apis/apps/v1/namespaces/kube-system/daemonsets/cilium"]
        kube.objs["/apis/apps/v1/namespaces/cattle-system/deployments/rancher"] = None  # malformed
        hc = HealthChecker(incident.CONFIG, kube=kube, etcd_request=etcd)
        with mock.patch.object(probes, "https", side_effect=reachable), mock.patch.object(probes, "tcp", side_effect=tcp_ok):
            hc.run()
        c = {(x.area, x.name): x for x in hc.checks}
        self.assertEqual(c[("cilium", "agents")].status, "skip")
        self.assertEqual(c[("rancher", "rancher")].status, "unknown")
        self.assertEqual(c[("rancher", "webhook")].status, "ok")

    def test_supervisor_lb_down(self):
        def https(url, **kw):
            if ":9345" in url:
                return {"reachable": False, "status": 0, "body": "", "error": "connection refused", "tls_untrusted": False, "latency_ms": 1}
            return reachable(url)
        hc, c = run(fixed=True, https=https)
        self.assertEqual(c[("api", "supervisor_lb")].status, "fail")


if __name__ == "__main__":
    unittest.main()
