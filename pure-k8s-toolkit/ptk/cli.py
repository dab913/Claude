"""ptk - Pure Storage toolkit for RKE2 clusters."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from ptk import __version__

log = logging.getLogger("ptk")


def _csv(value: str) -> list:
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_node_check(args: argparse.Namespace) -> int:
    from ptk.metrics import Exporter
    from ptk.nodecheck import NodeChecker, node_name
    from ptk.policy import load_policy

    checker = NodeChecker(load_policy(), host_root=args.host_root, proc=args.proc)
    node = node_name()
    if args.once:
        checker.run()
        report = checker.report(node)
        print(json.dumps(report, indent=2))
        return {"ok": 0, "warn": 0 if not args.strict else 1, "fail": 2}[report["status"]]

    exporter = Exporter(args.port, ready=checker.routes_ok)
    exporter.start()
    log.info("node-check on %s serving :%d every %ds", node, args.port, args.interval)
    while True:
        checker.run()
        report = checker.report(node)
        exporter.publish(checker.metrics(node), report)
        for r in report["checks"]:
            if r["status"] in ("warn", "fail"):
                log.warning("%s %s: %s", r["status"].upper(), r["check"], r["message"])
        log.info("node %s status=%s", node, report["status"])
        time.sleep(args.interval)


def cmd_audit(args: argparse.Namespace) -> int:
    from ptk.audit import load_arrays, run_audit, to_metrics
    from ptk.kube import Kube
    from ptk.metrics import Exporter

    kube = Kube()
    arrays = load_arrays(args.arrays_file)
    drivers, classes = _csv(args.drivers), _csv(args.storage_classes)

    def once() -> tuple:
        started = time.time()
        result, errors = run_audit(kube, arrays, drivers, classes)
        for name, err in errors.items():
            log.error("array %s: %s", name, err)
        log.info("audit: %d mapped, %d missing, %d destroyed-but-bound, %d orphans",
                 len(result["mapped"]), len(result["missing"]),
                 len(result["destroyed_but_pv_exists"]), len(result["orphans"]))
        return result, errors, started

    if args.once:
        result, errors, _ = once()
        result.pop("_arrays_ok", None)
        print(json.dumps({"errors": errors, **result}, indent=2))
        return 1 if errors else 0

    exporter = Exporter(args.port)
    exporter.start()
    while True:
        try:
            result, errors, started = once()
            exporter.publish(to_metrics(result, errors, started), {"errors": errors, **result})
        except Exception:
            log.exception("audit cycle failed")
        time.sleep(args.interval)


def cmd_health(args: argparse.Namespace) -> int:
    from ptk.health import HealthChecker, load_config
    from ptk.metrics import Exporter

    checker = HealthChecker(load_config(args.config))
    exit_code = {"green": 0, "yellow": 1, "red": 2}

    if args.once:
        checker.run()
        report = checker.report()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(checker.summary())
        if args.output:
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2)
        return exit_code[report["overall"]]

    exporter = Exporter(args.port)
    exporter.start()
    log.info("health checks serving :%d every %ds", args.port, args.interval)
    while True:
        try:
            checker.run()
            report = checker.report()
            exporter.publish(checker.metrics(), report)
            log.info("overall=%s %s", report["overall"],
                     " ".join(f"{a}={c}" for a, c in report["areas"].items()))
        except Exception:
            log.exception("health cycle failed")
        time.sleep(args.interval)


def cmd_bench(args: argparse.Namespace) -> int:
    from ptk import bench

    profiles = _csv(args.profiles) if args.profiles else list(bench.PROFILES)
    results = bench.run(args.dir, args.size, args.runtime, profiles,
                        device=args.device, direct=args.direct)
    print(bench.table(results))
    record = {
        "storageclass": os.environ.get("PTK_STORAGECLASS", ""),
        "runtime": os.environ.get("PTK_RUNTIME_CLASS") or "runc",
        "node": os.environ.get("NODE_NAME", ""),
        "volume_mode": "Block" if args.device else "Filesystem",
        "results": results,
    }
    print(bench.RESULT_MARKER + json.dumps(record))
    return 0


def cmd_bench_report(args: argparse.Namespace) -> int:
    from ptk import bench

    records = []
    for path in args.files:
        with (sys.stdin if path == "-" else open(path)) as f:
            records.extend(bench.parse_logs(f))
    if not records:
        print("no PTK_BENCH_RESULT lines found", file=sys.stderr)
        return 1
    print(bench.compare(records, baseline=args.baseline))
    return 0


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(prog="ptk", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("node-check", help="check this node's multipath/iSCSI/NVMe setup for Pure")
    p.add_argument("--host-root", default=os.environ.get("PTK_HOST_ROOT", "/host"))
    p.add_argument("--proc", default=os.environ.get("PTK_PROC", "/proc"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PTK_PORT", "9110")))
    p.add_argument("--interval", type=int, default=int(os.environ.get("PTK_INTERVAL", "300")))
    p.add_argument("--once", action="store_true", help="print a JSON report and exit (2 on fail)")
    p.add_argument("--strict", action="store_true", help="with --once, exit 1 on warnings too")
    p.set_defaults(func=cmd_node_check)

    p = sub.add_parser("audit", help="correlate PVs with FlashArray volumes")
    p.add_argument("--arrays-file", default=os.environ.get("PTK_ARRAYS_FILE", "/etc/ptk/arrays.json"))
    p.add_argument("--drivers", default=os.environ.get("PTK_CSI_DRIVERS", "pxd.portworx.com,pure-csi"))
    p.add_argument("--storage-classes", default=os.environ.get("PTK_STORAGECLASSES", ""),
                   help="only these StorageClasses (overrides --drivers)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PTK_PORT", "9111")))
    p.add_argument("--interval", type=int, default=int(os.environ.get("PTK_INTERVAL", "600")))
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("health", help="cluster health: etcd, API, Cilium, roles, Rancher, ingress, HAProxy, Harbor")
    p.add_argument("--config", default=os.environ.get("PTK_HEALTH_CONFIG"),
                   help="JSON config (see deploy/health/morpheus-net.json)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PTK_PORT", "9112")))
    p.add_argument("--interval", type=int, default=int(os.environ.get("PTK_INTERVAL", "60")))
    p.add_argument("--once", action="store_true", help="run once, print, exit 0 green / 1 yellow / 2 red")
    p.add_argument("--json", action="store_true", help="with --once, print the JSON report instead of the summary")
    p.add_argument("--output", help="with --once, also write the JSON report to this file")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("bench", help="run fio profiles against a mounted volume")
    p.add_argument("--dir", default=os.environ.get("PTK_BENCH_DIR", "/data"))
    p.add_argument("--size", default=os.environ.get("PTK_BENCH_SIZE", "4g"))
    p.add_argument("--runtime", type=int, default=int(os.environ.get("PTK_BENCH_RUNTIME", "60")))
    p.add_argument("--profiles", default=os.environ.get("PTK_BENCH_PROFILES", ""))
    p.add_argument("--device", default=os.environ.get("PTK_BENCH_DEVICE", ""),
                   help="raw block device of a volumeMode: Block PVC (overwritten)")
    p.add_argument("--no-direct", dest="direct", action="store_false",
                   default=os.environ.get("PTK_BENCH_DIRECT", "1") != "0",
                   help="buffered I/O, for filesystems that reject O_DIRECT")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("bench-report", help="compare bench Job logs, e.g. Kata vs runc")
    p.add_argument("files", nargs="+", help="saved Job logs, or - for stdin")
    p.add_argument("--baseline", default="runc", help="runtime to compare the others against")
    p.set_defaults(func=cmd_bench_report)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    return args.func(args)
