"""Correlates Kubernetes PersistentVolumes with FlashArray volumes.

Answers questions neither Rancher nor the array UI answers alone:
  * which namespace / PVC owns array volume X, and how well does it reduce?
  * which array volumes look CSI-provisioned but no longer have a PV (orphans)?
  * which PVs point at a volume that is missing or sitting in the array's
    destroyed-volumes bucket (eradication pending)?
  * which WWID does a PVC have, so node multipath path counts can be joined to it?
"""

from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List

from ptk.flasharray import wwid_for_serial
from ptk.metrics import Snapshot

PV_NAME_RE = re.compile(r"pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _quantity_bytes(q: str) -> float:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
             "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", q.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * units.get(m.group(2), 1)


def select_pvs(pvs: Iterable[dict], drivers: List[str], storage_classes: List[str]) -> List[dict]:
    """PVs that should have a same-named volume on a FlashArray."""
    out = []
    for pv in pvs:
        spec = pv.get("spec", {})
        sc = spec.get("storageClassName", "")
        driver = (spec.get("csi") or {}).get("driver", "")
        if storage_classes:
            if sc in storage_classes:
                out.append(pv)
        elif driver in drivers:
            out.append(pv)
    return out


def correlate(pvs: List[dict], arrays: Dict[str, dict]) -> dict:
    """arrays maps array name -> {"volumes": [...], "destroyed": [...]} from FlashArray REST."""
    by_pv: Dict[str, tuple] = {}
    destroyed_by_pv: Dict[str, tuple] = {}
    candidates: List[tuple] = []
    for array_name, data in arrays.items():
        for vol in data.get("volumes", []):
            m = PV_NAME_RE.search(vol.get("name", ""))
            if m:
                by_pv[m.group(0)] = (array_name, vol)
                candidates.append((array_name, vol, m.group(0)))
        for vol in data.get("destroyed", []):
            m = PV_NAME_RE.search(vol.get("name", ""))
            if m:
                destroyed_by_pv[m.group(0)] = (array_name, vol)

    pv_names = {pv["metadata"]["name"] for pv in pvs}
    mapped, missing, pending_eradication = [], [], []
    for pv in pvs:
        name = pv["metadata"]["name"]
        spec = pv.get("spec", {})
        claim = spec.get("claimRef") or {}
        row = {
            "pv": name,
            "namespace": claim.get("namespace", ""),
            "pvc": claim.get("name", ""),
            "storageclass": spec.get("storageClassName", ""),
            "phase": pv.get("status", {}).get("phase", ""),
            "requested_bytes": _quantity_bytes((spec.get("capacity") or {}).get("storage", "0")),
        }
        hit = by_pv.get(name)
        if hit:
            array_name, vol = hit
            space = vol.get("space") or {}
            row.update({
                "array": array_name,
                "array_volume": vol.get("name", ""),
                "wwid": wwid_for_serial(vol["serial"]) if vol.get("serial") else "",
                "provisioned_bytes": vol.get("provisioned", 0) or 0,
                "virtual_bytes": space.get("virtual", 0) or 0,
                "physical_bytes": space.get("total_physical", 0) or 0,
                "data_reduction": space.get("data_reduction", 0) or 0,
            })
            mapped.append(row)
        elif name in destroyed_by_pv:
            array_name, vol = destroyed_by_pv[name]
            row.update({"array": array_name, "array_volume": vol.get("name", "")})
            pending_eradication.append(row)
        else:
            missing.append(row)

    orphans = []
    for array_name, vol, pv_name in candidates:
        if pv_name not in pv_names:
            orphans.append({
                "array": array_name,
                "array_volume": vol.get("name", ""),
                "provisioned_bytes": vol.get("provisioned", 0) or 0,
                "physical_bytes": (vol.get("space") or {}).get("total_physical", 0) or 0,
            })
    return {"mapped": mapped, "missing": missing,
            "destroyed_but_pv_exists": pending_eradication, "orphans": orphans}


def to_metrics(result: dict, errors: Dict[str, str], started: float) -> Snapshot:
    snap = Snapshot()
    for row in result.get("mapped", []):
        labels = {"namespace": row["namespace"], "pvc": row["pvc"], "pv": row["pv"],
                  "storageclass": row["storageclass"], "array": row["array"]}
        snap.add("ptk_pvc_volume_info", 1, dict(labels, array_volume=row["array_volume"], wwid=row["wwid"]),
                 help="Maps a PVC to its FlashArray volume and WWID (join with ptk_multipath_paths on wwid)")
        snap.add("ptk_pvc_provisioned_bytes", row["provisioned_bytes"], labels, help="Provisioned size on the array")
        snap.add("ptk_pvc_virtual_bytes", row["virtual_bytes"], labels, help="Logical data written, before reduction")
        snap.add("ptk_pvc_physical_bytes", row["physical_bytes"], labels, help="Physical space consumed after reduction")
        snap.add("ptk_pvc_data_reduction_ratio", row["data_reduction"], labels, help="Array-reported data reduction")
    for row in result.get("missing", []):
        snap.add("ptk_pv_missing_on_array", 1,
                 {"pv": row["pv"], "namespace": row["namespace"], "pvc": row["pvc"], "storageclass": row["storageclass"]},
                 help="PV whose backing volume was not found on any configured array")
    for row in result.get("destroyed_but_pv_exists", []):
        snap.add("ptk_pv_volume_destroyed", 1,
                 {"pv": row["pv"], "namespace": row["namespace"], "pvc": row["pvc"], "array": row["array"]},
                 help="PV whose array volume is in the destroyed bucket (will be eradicated)")
    for row in result.get("orphans", []):
        snap.add("ptk_array_orphan_volume_bytes", row["provisioned_bytes"],
                 {"array": row["array"], "array_volume": row["array_volume"]},
                 help="CSI-named array volume with no matching PV")
    snap.add("ptk_array_orphan_volumes", len(result.get("orphans", [])), help="Count of orphaned CSI volumes")
    for array, err in errors.items():
        snap.add("ptk_array_up", 0, {"array": array, "error": err[:120]}, help="1 if the array API was reachable")
    for array in result.get("_arrays_ok", []):
        snap.add("ptk_array_up", 1, {"array": array, "error": ""}, help="1 if the array API was reachable")
    snap.add("ptk_audit_duration_seconds", time.time() - started)
    if not errors:
        snap.add("ptk_audit_last_success_timestamp_seconds", time.time())
    return snap


def run_audit(kube, arrays: list, drivers: List[str], storage_classes: List[str]) -> tuple:
    """Returns (result, errors). `arrays` is a list of FlashArray clients."""
    pvs = select_pvs(kube.list("/api/v1/persistentvolumes"), drivers, storage_classes)
    data: Dict[str, dict] = {}
    errors: Dict[str, str] = {}
    for fa in arrays:
        try:
            data[fa.name] = {"volumes": fa.volumes(), "destroyed": fa.destroyed_volumes()}
        except Exception as exc:  # keep auditing the other arrays
            errors[fa.name] = f"{type(exc).__name__}: {exc}"
    result = correlate(pvs, data)
    if errors:
        # Without every array's volume list, "missing" and "orphan" are unreliable.
        result["missing"] = []
        result["orphans"] = []
    result["_arrays_ok"] = sorted(data)
    return result, errors


def load_arrays(path: str) -> list:
    """Array list JSON: [{"name","endpoint","token_file","ca_file"?,"insecure"?}]"""
    import json

    from ptk.flasharray import FlashArray

    with open(path) as f:
        entries = json.load(f)
    out = []
    for e in entries:
        with open(e["token_file"]) as tf:
            token = tf.read().strip()
        out.append(FlashArray(e["name"], e["endpoint"], token,
                              ca_file=e.get("ca_file"), insecure=bool(e.get("insecure", False))))
    return out


