import tempfile
import unittest

from ptk.nodecheck import NodeChecker
from ptk.policy import load_policy
from tests import fakehost


def check(**kwargs):
    tmp = tempfile.mkdtemp()
    host, proc = fakehost.build(tmp, **kwargs)
    c = NodeChecker(load_policy(), host_root=host, proc=proc)
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


if __name__ == "__main__":
    unittest.main()
