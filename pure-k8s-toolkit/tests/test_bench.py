import unittest

import json

from ptk.bench import RESULT_MARKER, compare, parse_logs, summarize, table


class BenchTest(unittest.TestCase):
    def test_summarize(self):
        fio = {"jobs": [{"read": {"iops": 1000.4, "bw_bytes": 4096000,
                                  "clat_ns": {"percentile": {"99.000000": 850000}}},
                         "write": {"iops": 0, "bw_bytes": 0, "clat_ns": {}}}]}
        s = summarize(fio)
        self.assertEqual(s["read_iops"], 1000)
        self.assertEqual(s["read_p99_ms"], 0.85)
        self.assertEqual(s["write_p99_ms"], 0)
        self.assertIn("randread", table({"randread": s}))


    def test_compare_kata_against_runc(self):
        def rec(runtime, iops, p99):
            r = {"read_iops": iops, "write_iops": 0, "read_mib_s": iops / 256, "write_mib_s": 0,
                 "read_p99_ms": p99, "write_p99_ms": 0}
            return RESULT_MARKER + json.dumps({"storageclass": "px-fada", "runtime": runtime,
                                               "results": {"randread-4k": r}})
        logs = ["fio noise", rec("kata-qemu", 60000, 1.5), rec("runc", 100000, 1.0)]
        out = compare(parse_logs(logs))
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("px-fada") and "runc" in lines[1])  # baseline first
        self.assertIn("60000 (-40%)", lines[2])
        self.assertIn("1.500 (+50%)", lines[2])


if __name__ == "__main__":
    unittest.main()


class RunTest(unittest.TestCase):
    def _fake_fio(self, body):
        import os
        import stat
        import tempfile
        bindir = tempfile.mkdtemp()
        path = os.path.join(bindir, "fio")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, stat.S_IRWXU)
        return bindir

    def _run(self, body, **kw):
        import os
        import tempfile
        from unittest import mock

        from ptk import bench
        bindir = self._fake_fio(body)
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            return bench.run(tempfile.mkdtemp(), "1g", 1, ["randread-4k"], **kw), bindir

    def test_block_device_args(self):
        body = 'echo "$@" > "$(dirname "$0")/args"; echo \'{"jobs": []}\'\n'
        results, bindir = self._run(body, device="/dev/xvda", direct=True)
        with open(bindir + "/args") as f:
            args = f.read()
        self.assertIn("--filename=/dev/xvda", args)
        self.assertNotIn("--directory", args)
        self.assertIn("--direct=1", args)
        self.assertEqual(results["randread-4k"]["read_iops"], 0)

    def test_odirect_rejection_hint(self):
        body = 'echo "fio: pid=1, err=22/file:filesetup.c, error=Invalid argument" >&2; exit 1\n'
        with self.assertRaises(RuntimeError) as ctx:
            self._run(body)
        self.assertIn("PTK_BENCH_DIRECT=0", str(ctx.exception))
