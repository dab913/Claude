"""fio benchmark against a mounted PVC, so StorageClasses, protocols and
multipath settings can be compared with numbers instead of guesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Dict, List

PROFILES: Dict[str, List[str]] = {
    "randread-4k": ["--rw=randread", "--bs=4k", "--iodepth=32", "--numjobs=4"],
    "randwrite-4k": ["--rw=randwrite", "--bs=4k", "--iodepth=32", "--numjobs=4"],
    "mixed-70r-8k": ["--rw=randrw", "--rwmixread=70", "--bs=8k", "--iodepth=16", "--numjobs=4"],
    "seqread-1m": ["--rw=read", "--bs=1m", "--iodepth=8", "--numjobs=2"],
    "seqwrite-1m": ["--rw=write", "--bs=1m", "--iodepth=8", "--numjobs=2"],
    # Single-threaded sync writes: what a database commit log sees.
    "latency-4k-qd1": ["--rw=randwrite", "--bs=4k", "--iodepth=1", "--numjobs=1", "--fsync=1"],
}


def summarize(fio_json: dict) -> Dict[str, float]:
    read = {"iops": 0.0, "bw": 0.0, "lat": []}
    write = {"iops": 0.0, "bw": 0.0, "lat": []}
    for job in fio_json.get("jobs", []):
        for side, acc in (("read", read), ("write", write)):
            d = job.get(side, {})
            acc["iops"] += d.get("iops", 0.0)
            acc["bw"] += d.get("bw_bytes", 0.0)
            p99 = (d.get("clat_ns", {}).get("percentile") or {}).get("99.000000")
            if p99 and d.get("iops", 0) > 0:
                acc["lat"].append(p99)
    return {
        "read_iops": round(read["iops"]),
        "write_iops": round(write["iops"]),
        "read_mib_s": round(read["bw"] / 2**20, 1),
        "write_mib_s": round(write["bw"] / 2**20, 1),
        "read_p99_ms": round(max(read["lat"], default=0) / 1e6, 3),
        "write_p99_ms": round(max(write["lat"], default=0) / 1e6, 3),
    }


def run(directory: str, size: str, runtime: int, profiles: List[str]) -> Dict[str, Dict[str, float]]:
    if not shutil.which("fio"):
        raise RuntimeError("fio is not installed in this image")
    results = {}
    for name in profiles:
        if name not in PROFILES:
            raise ValueError(f"unknown profile {name}; choose from {', '.join(PROFILES)}")
        cmd = ["fio", f"--name={name}", f"--directory={directory}", f"--size={size}",
               "--ioengine=libaio", "--direct=1", "--time_based", f"--runtime={runtime}",
               "--ramp_time=5", "--group_reporting", "--output-format=json", *PROFILES[name]]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        results[name] = summarize(json.loads(proc.stdout))
        for f in os.listdir(directory):
            if f.startswith(name + "."):
                os.remove(os.path.join(directory, f))
    return results


def table(results: Dict[str, Dict[str, float]]) -> str:
    cols = ["read_iops", "write_iops", "read_mib_s", "write_mib_s", "read_p99_ms", "write_p99_ms"]
    rows = [["profile", *cols]] + [[n, *(str(r[c]) for c in cols)] for n, r in results.items()]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(v.rjust(w) if i else v.ljust(w) for i, (v, w) in enumerate(zip(r, widths)))
                     for r in rows)
