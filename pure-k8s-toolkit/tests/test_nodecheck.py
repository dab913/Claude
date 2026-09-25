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


if __name__ == "__main__":
    unittest.main()
