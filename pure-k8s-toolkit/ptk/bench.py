"""fio benchmark against a mounted PVC, so StorageClasses, protocols and
multipath settings can be compared with numbers instead of guesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Dict, Iterable, List

# Marks the machine-readable line in Job logs so bench-report can find it.
RESULT_MARKER = "PTK_BENCH_RESULT "

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


def run(directory: str, size: str, runtime: int, profiles: List[str],
        device: str = "", direct: bool = True) -> Dict[str, Dict[str, float]]:
    """Benchmarks files in `directory`, or the raw block device `device` if given
    (volumeMode: Block PVC; its contents are overwritten)."""
    if not shutil.which("fio"):
        raise RuntimeError("fio is not installed in this image")
    target = [f"--filename={device}"] if device else [f"--directory={directory}", f"--size={size}"]
    results = {}
    for name in profiles:
        if name not in PROFILES:
            raise ValueError(f"unknown profile {name}; choose from {', '.join(PROFILES)}")
        cmd = ["fio", f"--name={name}", *target, "--ioengine=libaio", f"--direct={int(direct)}",
               "--time_based", f"--runtime={runtime}", "--ramp_time=5", "--group_reporting",
               "--output-format=json", *PROFILES[name]]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            hint = ""
            if direct and not device and "Invalid argument" in proc.stderr:
                hint = " (O_DIRECT rejected; under Kata virtio-fs, retry with PTK_BENCH_DIRECT=0)"
            raise RuntimeError(f"fio {name} failed{hint}: {proc.stderr.strip()[-500:]}")
        results[name] = summarize(json.loads(proc.stdout))
        if not device:
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


def parse_logs(lines: Iterable[str]) -> List[dict]:
    return [json.loads(line.split(RESULT_MARKER, 1)[1]) for line in lines if RESULT_MARKER in line]


def compare(records: List[dict], baseline: str = "runc") -> str:
    """One row per storageclass/runtime/profile, with change vs the baseline runtime
    on the same StorageClass and profile (how much Kata's VM boundary costs)."""
    def sc(r: dict) -> str:
        mode = r.get("volume_mode", "Filesystem")
        return r["storageclass"] + ("" if mode == "Filesystem" else f" [{mode}]")

    base = {(sc(r), p): v for r in records if r.get("runtime") == baseline
            for p, v in r["results"].items()}

    def delta(now: float, ref: float, lower_is_better: bool = False) -> str:
        if not ref:
            return ""
        pct = (now - ref) / ref * 100
        return f" ({pct:+.0f}%)" if abs(pct) >= 0.5 else " (=)"

    rows = [["storageclass", "runtime", "profile", "iops", "MiB/s", "p99 ms"]]
    for r in sorted(records, key=lambda r: (sc(r), r.get("runtime") != baseline, r.get("runtime", ""))):
        for prof, v in r["results"].items():
            ref = base.get((sc(r), prof)) if r.get("runtime") != baseline else None
            iops = v["read_iops"] + v["write_iops"]
            mib = v["read_mib_s"] + v["write_mib_s"]
            p99 = max(v["read_p99_ms"], v["write_p99_ms"])
            cells = [str(iops), f"{mib:.1f}", f"{p99:.3f}"]
            if ref:
                cells[0] += delta(iops, ref["read_iops"] + ref["write_iops"])
                cells[1] += delta(mib, ref["read_mib_s"] + ref["write_mib_s"])
                cells[2] += delta(p99, max(ref["read_p99_ms"], ref["write_p99_ms"]))
            rows.append([sc(r), r.get("runtime", "?"), prof, *cells])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) if i < 3 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths)))
                     for row in rows)
