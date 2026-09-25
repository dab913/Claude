import unittest

from ptk.audit import correlate, select_pvs, to_metrics

UID_A = "pvc-11111111-2222-3333-4444-555555555555"
UID_B = "pvc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UID_C = "pvc-99999999-8888-7777-6666-555555555555"
UID_ORPHAN = "pvc-00000000-0000-0000-0000-000000000000"


def pv(name, sc="px-fada", driver="pxd.portworx.com", ns="db", claim="data-0"):
    return {"metadata": {"name": name},
            "spec": {"storageClassName": sc, "csi": {"driver": driver, "volumeHandle": "x"},
                     "capacity": {"storage": "100Gi"}, "claimRef": {"namespace": ns, "name": claim}},
            "status": {"phase": "Bound"}}


def vol(name, serial="ABCDEF0123456789", provisioned=107374182400):
    return {"name": name, "serial": serial, "provisioned": provisioned,
            "space": {"virtual": 5e9, "total_physical": 1e9, "data_reduction": 5.0}}


class AuditTest(unittest.TestCase):
    def test_select_by_driver_or_class(self):
        pvs = [pv(UID_A), pv(UID_B, sc="local-path", driver="rancher.io/local-path")]
        self.assertEqual([p["metadata"]["name"] for p in select_pvs(pvs, ["pxd.portworx.com"], [])], [UID_A])
        self.assertEqual(select_pvs(pvs, ["pxd.portworx.com"], ["local-path"])[0]["metadata"]["name"], UID_B)

    def test_correlate(self):
        pvs = [pv(UID_A), pv(UID_B, ns="web", claim="cache"), pv(UID_C)]
        arrays = {"fa01": {"volumes": [vol(f"px_1a2b3c-{UID_A}"), vol(f"px_1a2b3c-{UID_ORPHAN}"),
                                       vol("vcenter-datastore-01")],
                           "destroyed": [vol(f"px_1a2b3c-{UID_C}")]}}
        r = correlate(pvs, arrays)
        self.assertEqual(len(r["mapped"]), 1)
        row = r["mapped"][0]
        self.assertEqual((row["namespace"], row["pvc"], row["array"]), ("db", "data-0", "fa01"))
        self.assertEqual(row["wwid"], "3624a9370abcdef0123456789")
        self.assertEqual(row["requested_bytes"], 100 * 2**30)
        self.assertEqual([m["pv"] for m in r["missing"]], [UID_B])
        self.assertEqual([m["pv"] for m in r["destroyed_but_pv_exists"]], [UID_C])
        # Non-CSI volumes such as datastores are never reported as orphans.
        self.assertEqual([o["array_volume"] for o in r["orphans"]], [f"px_1a2b3c-{UID_ORPHAN}"])

        text = to_metrics(dict(r, _arrays_ok=["fa01"]), {}, 0).render()
        self.assertIn("ptk_array_orphan_volumes 1", text)
        self.assertIn('ptk_pvc_data_reduction_ratio{array="fa01",namespace="db"', text)
        self.assertIn('ptk_array_up{array="fa01",error=""} 1', text)


if __name__ == "__main__":
    unittest.main()
