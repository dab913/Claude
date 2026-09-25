import tempfile
import unittest

from ptk.nodecheck import NodeChecker
from ptk.policy import load_policy
from tests import fakehost


def check(policy=None, **kwargs):
    tmp = tempfile.mkdtemp()
    host, proc = fakehost.build(tmp, **kwargs)
    c = NodeChecker(dict(load_policy(), **(policy or {})), host_root=host, proc=proc)
    c.run()
    return c, {r.check: r for r in c.results}


class NodeCheckTest(unittest.TestCase):
    def test_healthy_node(self):
        c, results = check()
        bad = [r.as_dict() for r in results.values() if r.status in ("warn", "fail")]
        self.assertEqual(bad, [])
        self.assertEqual(c.report("n1")["status"], "ok")
        self.assertEqual(c.paths[0]["running"], 2)
        text = c.metrics("n1").render()
        self.assertIn(f'ptk_multipath_paths_running{{dm="dm-3",node="n1",wwid="{fakehost.WWID}"}} 2', text)
        self.assertIn('iqn="iqn.1994-05.com.redhat:abc123"', text)

    def test_single_running_path_warns(self):
        c, results = check(path_states=("running", "offline"))
        self.assertEqual(results["device.paths"].status, "warn")
        self.assertIn("1/2 running", results["device.paths"].message)

    def test_no_running_paths_fails(self):
        c, results = check(path_states=("offline", "offline"))
        self.assertEqual(results["device.paths"].status, "fail")

    def test_missing_daemons_fail(self):
        c, results = check(processes=("sshd",))
        self.assertEqual(results["daemon.multipathd"].status, "fail")
        self.assertEqual(results["daemon.iscsid"].status, "fail")
        self.assertEqual(c.report("n1")["status"], "fail")

    def test_default_rhel_conf_flags_problems(self):
        # What `mpathconf --enable` writes on RHEL 9: friendly names on, no Pure stanza, no VMware blacklist.
        c, results = check(conf="defaults {\n user_friendly_names yes\n find_multipaths yes\n}\n")
        self.assertEqual(results["multipath.defaults.user_friendly_names"].status, "warn")
        self.assertEqual(results["multipath.device.scsi"].status, "warn")
        self.assertEqual(results["multipath.blacklist.VMware"].status, "warn")

    def test_wrong_scheduler_warns(self):
        c, results = check(scheduler="none [mq-deadline] kyber")
        self.assertEqual(results["device.scheduler"].status, "warn")


    def test_kata_configured_and_capable(self):
        c, results = check(kata=fakehost.CONTAINERD_V3)
        self.assertEqual(c.kata_handlers, ["kata-qemu"])
        self.assertEqual(results["kata.containerd"].status, "ok")
        self.assertEqual(results["kata.shim"].status, "ok")
        self.assertEqual(c.report("n1")["status"], "ok")
        text = c.metrics("n1").render()
        self.assertIn('ptk_node_kata_capable{node="n1"} 1', text)
        self.assertIn('ptk_node_kata_runtime_info{handler="kata-qemu",node="n1"} 1', text)

    def test_kata_configured_without_nested_virt_fails_with_vcenter_hint(self):
        c, results = check(kata=fakehost.CONTAINERD_V3, cpu_flags="fpu vme sse2", kvm=False)
        self.assertEqual(results["kata.cpu_virtualization"].status, "fail")
        self.assertIn("Expose hardware assisted virtualization", results["kata.cpu_virtualization"].message)
        self.assertEqual(results["kata.kvm"].status, "fail")
        self.assertEqual(c.report("n1")["status"], "fail")

    def test_kata_absent_is_informational(self):
        # e.g. control-plane nodes where kata-deploy is not scheduled
        c, results = check(cpu_flags="fpu sse2", kvm=False)
        self.assertEqual(results["kata.cpu_virtualization"].status, "info")
        self.assertEqual(results["kata.containerd"].status, "info")
        self.assertEqual(c.report("n1")["status"], "ok")
        self.assertIn('ptk_node_kata_capable{node="n1"} 0', c.metrics("n1").render())

    def test_kata_required_warns_when_not_installed(self):
        c, results = check(policy={"kata": "required"})
        self.assertEqual(results["kata.containerd"].status, "warn")

    def test_kata_containerd_v2_config(self):
        v2 = '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.kata]\n  runtime_type = "io.containerd.kata.v2"\n'
        c, _ = check(kata=v2)
        self.assertEqual(c.kata_handlers, ["kata"])

    def test_kata_unreadable_config_warns(self):
        import os
        tmp = tempfile.mkdtemp()
        host, proc = fakehost.build(tmp, kata=fakehost.CONTAINERD_V3)
        conf = os.path.join(host, "var/lib/rancher/rke2/agent/etc/containerd/config.toml")
        os.chmod(conf, 0)
        if os.access(conf, os.R_OK):
            self.skipTest("running as root; permission bits are not enforced")
        c = NodeChecker(load_policy(), host_root=host, proc=proc)
        results = {r.check: r for r in c.run()}
        self.assertEqual(results["kata.containerd_readable"].status, "warn")

    def test_kata_off(self):
        c, results = check(policy={"kata": "off"})
        self.assertFalse(any(k.startswith("kata.") for k in results))
        self.assertNotIn("ptk_node_kata", c.metrics("n1").render())


    # -- RKE2 role (the w08 situation) ------------------------------------

    def test_agent_node_reports_mode_without_etcd(self):
        c, results = check(rke2="agent")
        self.assertEqual(results["rke2.role"].status, "info")
        text = c.metrics("w08").render()
        self.assertIn('ptk_node_rke2_mode_info{mode="agent",node="w08"} 1', text)
        self.assertIn('ptk_node_etcd_running{node="w08"} 0', text)

    def test_etcd_on_agent_fails(self):
        c, results = check(rke2="agent", processes=("multipathd", "iscsid", "etcd"))
        self.assertEqual(results["rke2.role"].status, "fail")

    def test_server_with_etcd(self):
        c, results = check(rke2="server")
        self.assertEqual(results["rke2.role"].status, "info")
        self.assertIn('ptk_node_etcd_running{node="c01"} 1', c.metrics("c01").render())

    def test_server_without_etcd_warns(self):
        c, results = check(processes=("multipathd", "iscsid", ("rke2", "rke2\0server\0")))
        self.assertEqual(results["rke2.role"].status, "warn")

    # -- CNI ----------------------------------------------------------------

    def test_cilium_only(self):
        c, results = check(policy={"cni": "cilium"},
                           cni={"05-cilium.conflist": fakehost.CILIUM,
                                "10-canal.conflist.cilium_bak": fakehost.CANAL})  # renamed by Cilium: ignored
        self.assertEqual(results["cni.config"].status, "ok")

    def test_canal_leftover_warns(self):
        c, results = check(policy={"cni": "cilium"},
                           cni={"05-cilium.conflist": fakehost.CILIUM, "10-canal.conflist": fakehost.CANAL})
        self.assertEqual(results["cni.config"].status, "warn")
        self.assertIn("10-canal.conflist", results["cni.config"].message)
        text = c.metrics("w1").render()
        self.assertIn('ptk_node_cni_config_info{active="true",file="05-cilium.conflist"', text)
        self.assertIn('ptk_node_cni_config_info{active="false",file="10-canal.conflist"', text)

    def test_other_cni_active_fails(self):
        c, results = check(policy={"cni": "cilium"},
                           cni={"00-canal.conflist": fakehost.CANAL, "05-cilium.conflist": fakehost.CILIUM})
        self.assertEqual(results["cni.config"].status, "fail")

    def test_no_cni_config_fails(self):
        c, results = check(policy={"cni": "cilium"})
        self.assertEqual(results["cni.config"].status, "fail")

    def test_istio_cni_chained_into_cilium(self):
        c, results = check(policy={"cni": "cilium"}, cni={"05-cilium.conflist": fakehost.CILIUM_ISTIO})
        self.assertEqual(results["cni.config"].status, "ok")
        self.assertEqual(c.info.get("istio_cni_chained"), "true")

    def test_cni_check_off_by_default(self):
        c, results = check()
        self.assertNotIn("cni.config", results)


    # -- Service routing from the pod network --------------------------------

    def _routes(self, https_ok, dns_ok):
        from unittest import mock

        from ptk import probes
        def https(url, **kw):
            return {"reachable": https_ok, "status": 401 if https_ok else 0, "latency_ms": 2.0,
                    "error": "" if https_ok else "timed out"}
        def dns(name, **kw):
            return {"reachable": dns_ok, "addresses": ["10.43.0.1"] if dns_ok else [], "latency_ms": 1.0,
                    "error": "" if dns_ok else "temporary failure in name resolution"}
        policy = {"service_probes": ["https://10.43.0.1:443/version", "kubernetes.default.svc.cluster.local"]}
        with mock.patch.object(probes, "https", side_effect=https), mock.patch.object(probes, "dns", side_effect=dns):
            return check(policy=policy)

    def test_service_routes_ok_even_on_401(self):
        c, results = self._routes(True, True)
        self.assertEqual(results["route.10.43.0.1:443"].status, "ok")
        self.assertEqual(results["route.dns:kubernetes.default.svc.cluster.local"].status, "ok")
        self.assertTrue(c.routes_ok())
        self.assertIn('ptk_node_service_route_ok{node="w13",target="10.43.0.1:443"} 1', c.metrics("w13").render())

    def test_service_route_timeout_fails_and_marks_unready(self):
        # rancher-webhook on w13: "Get https://10.43.0.1/version ... i/o timeout"
        c, results = self._routes(False, False)
        self.assertEqual(results["route.10.43.0.1:443"].status, "fail")
        self.assertIn("Cilium agent", results["route.10.43.0.1:443"].message)
        self.assertFalse(c.routes_ok())
        self.assertIn('ptk_node_service_route_ok{node="w13",target="10.43.0.1:443"} 0', c.metrics("w13").render())


if __name__ == "__main__":
    unittest.main()
