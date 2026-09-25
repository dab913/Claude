import unittest

from ptk.bench import summarize, table


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


if __name__ == "__main__":
    unittest.main()
