"""Checks a RHEL node's storage stack for Pure FlashArray readiness.

Runs as a DaemonSet (host /etc and /sys mounted read-only under /host, hostPID
so host processes are visible) or once from podman on a single node.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket
from typing import Dict, List, Optional

from ptk import multipath
from ptk.metrics import Snapshot

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
SEVERITY = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}

# Pure FlashArray SCSI WWIDs are NAA 6 with Pure's OUI; the volume serial follows.
PURE_WWID_PREFIX = "3624a9370"


class Result:
    def __init__(self, check: str, status: str, message: str) -> None:
        self.check, self.status, self.message = check, status, message

    def as_dict(self) -> Dict[str, str]:
        return {"check": self.check, "status": self.status, "message": self.message}


class NodeChecker:
    def __init__(self, policy: dict, host_root: str = "/host", proc: str = "/proc") -> None:
        self.policy = policy
        self.root = host_root
        self.etc = os.path.join(host_root, "etc")
        self.sys = os.path.join(host_root, "sys")
        self.proc = proc
        self.results: List[Result] = []
        self.info: Dict[str, str] = {}
        self.paths: List[Dict[str, object]] = []
        self.kata_handlers: List[str] = []
        self.kata_capable = False
        self.rke2_mode = ""
        self.etcd_running = False
        self.cni_configs: List[Dict[str, object]] = []

    # -- helpers -----------------------------------------------------------

    def _add(self, check: str, status: str, message: str) -> None:
        self.results.append(Result(check, status, message))

    def _read(self, path: str) -> Optional[str]:
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def _processes(self) -> set:
        names = set()
        for comm in glob.glob(os.path.join(self.proc, "[0-9]*", "comm")):
            name = self._read(comm)
            if not name:
                continue
            name = name.strip()
            names.add(name)
            if name == "rke2" and not self.rke2_mode:
                args = (self._read(os.path.join(os.path.dirname(comm), "cmdline")) or "").split("\0")
                if len(args) > 1 and args[1] in ("server", "agent"):
                    self.rke2_mode = args[1]
        return names

    def _modules(self) -> set:
        text = self._read(os.path.join(self.proc, "modules")) or ""
        mods = {line.split()[0] for line in text.splitlines() if line.strip()}
        # Built-in modules do not appear in /proc/modules but do in /sys/module.
        mods.update(os.path.basename(p) for p in glob.glob(os.path.join(self.sys, "module", "*")))
        return mods

    # -- checks ------------------------------------------------------------

    def run(self) -> List[Result]:
        self.results, self.info, self.paths = [], {}, []
        self.kata_handlers, self.kata_capable = [], False
        self.rke2_mode, self.etcd_running, self.cni_configs = "", False, []
        protocol = self.policy.get("protocol", "any")
        procs = self._processes()
        mods = self._modules()

        self._check_kernel(mods, protocol)
        self._check_daemons(procs, protocol)
        self._check_rke2_role(procs)
        if self.policy.get("cni"):
            self._check_cni(self.policy["cni"])
        self._check_identity(protocol)
        conf = self._check_multipath_conf(protocol)
        self._check_devices(conf)
        if self.policy.get("kata", "auto") != "off":
            self._check_kata(mods)
        return self.results

    def _check_kernel(self, mods: set, protocol: str) -> None:
        if "dm_multipath" in mods:
            self._add("kernel.dm_multipath", OK, "dm_multipath loaded")
        else:
            self._add("kernel.dm_multipath", FAIL, "dm_multipath not loaded; install device-mapper-multipath and run mpathconf --enable")
        if protocol in ("iscsi", "fc"):
            status = OK if "scsi_dh_alua" in mods else WARN
            self._add("kernel.scsi_dh_alua", status, "scsi_dh_alua " + ("loaded" if status == OK else "not loaded; ALUA path priority will not work"))
        wanted = {"iscsi": ["iscsi_tcp"], "nvme-tcp": ["nvme_tcp"], "fc": ["qla2xxx", "lpfc"]}.get(protocol)
        if wanted:
            present = [m for m in wanted if m in mods]
            self._add(f"kernel.{protocol}", OK if present else FAIL,
                      f"{'/'.join(present or wanted)} {'loaded' if present else 'not loaded'}")
        if protocol == "nvme-tcp":
            native = (self._read(os.path.join(self.sys, "module", "nvme_core", "parameters", "multipath")) or "?").strip()
            self.info["nvme_native_multipath"] = native
            self._add("kernel.nvme_native_multipath", INFO,
                      f"nvme_core.multipath={native}; make sure this matches what your CSI driver expects (dm-multipath vs native)")

    def _check_daemons(self, procs: set, protocol: str) -> None:
        if not procs:
            self._add("daemon.visibility", WARN, "no host processes visible; run with hostPID so daemon checks work")
            return
        self._add("daemon.multipathd", OK if "multipathd" in procs else FAIL,
                  "multipathd " + ("running" if "multipathd" in procs else "not running; systemctl enable --now multipathd"))
        if protocol == "iscsi":
            self._add("daemon.iscsid", OK if "iscsid" in procs else FAIL,
                      "iscsid " + ("running" if "iscsid" in procs else "not running; systemctl enable --now iscsid"))

    def _check_rke2_role(self, procs: set) -> None:
        """What this host actually runs, to compare against its Kubernetes role labels
        (alert PtkNodeRoleMismatch) and the expected etcd member count (PtkEtcdMemberCount).
        The host cannot see etcd membership itself; scripts/etcd-members.sh does that."""
        if not procs:
            return
        self.etcd_running = "etcd" in procs
        mode = self.rke2_mode or "unknown"
        self.info["rke2_mode"] = mode
        if mode == "agent" and self.etcd_running:
            self._add("rke2.role", FAIL, "etcd is running on a node whose RKE2 service is rke2-agent")
        elif mode == "server" and not self.etcd_running and self.policy.get("etcd_on_servers", True):
            self._add("rke2.role", WARN, "rke2-server without a running etcd on this node")
        else:
            self._add("rke2.role", INFO, f"rke2 {mode}" + (", etcd running" if self.etcd_running else ""))

    def _check_cni(self, expected: str) -> None:
        """containerd uses the first CNI config file in /etc/cni/net.d (sorted by name).
        Leftovers from another CNI (for example RKE2's default Canal) can silently win."""
        net_d = os.path.join(self.etc, "cni", "net.d")
        files = sorted(f for f in os.listdir(net_d) if f.endswith((".conf", ".conflist", ".json"))) \
            if os.path.isdir(net_d) else []
        for f in files:
            try:
                conf = json.loads(self._read(os.path.join(net_d, f)) or "{}")
            except ValueError:
                conf = {}
            plugins = [p.get("type", "") for p in conf.get("plugins", [conf]) if isinstance(p, dict)]
            self.cni_configs.append({"file": f, "name": conf.get("name", ""), "plugins": plugins})
        if not files:
            self._add("cni.config", FAIL, f"no CNI config in /etc/cni/net.d; {expected} has not initialised this node")
            return
        first = self.cni_configs[0]
        wanted = f"{expected}-cni" if expected == "cilium" else expected
        first_ok = wanted in first["plugins"] or expected in str(first["name"])
        others = [c["file"] for c in self.cni_configs[1:]]
        if not first_ok:
            self._add("cni.config", FAIL, f"active CNI config is {first['file']} ({', '.join(first['plugins'])}), not {expected}")
        elif others:
            self._add("cni.config", WARN, f"{first['file']} is active, but other CNI configs remain: {', '.join(others)}; "
                                          "remove them so they cannot take over")
        else:
            self._add("cni.config", OK, f"{expected} is the only CNI ({first['file']})")
        if "istio-cni" in first["plugins"]:
            self.info["istio_cni_chained"] = "true"

    def _check_identity(self, protocol: str) -> None:
        # vCenter template clones often share these; duplicates are caught cluster-wide
        # by the PrometheusRule that counts nodes per IQN / NQN / machine-id.
        machine_id = (self._read(os.path.join(self.etc, "machine-id")) or "").strip()
        if machine_id:
            self.info["machine_id"] = machine_id
        if protocol == "iscsi":
            text = self._read(os.path.join(self.etc, "iscsi", "initiatorname.iscsi")) or ""
            iqn = ""
            for line in text.splitlines():
                if line.strip().startswith("InitiatorName="):
                    iqn = line.split("=", 1)[1].strip()
            if iqn:
                self.info["iqn"] = iqn
                self._add("identity.iqn", OK, f"initiator {iqn}")
            else:
                self._add("identity.iqn", FAIL, "/etc/iscsi/initiatorname.iscsi missing or empty")
        if protocol == "nvme-tcp":
            nqn = (self._read(os.path.join(self.etc, "nvme", "hostnqn")) or "").strip()
            if nqn:
                self.info["hostnqn"] = nqn
                self._add("identity.hostnqn", OK, f"host NQN {nqn}")
            else:
                self._add("identity.hostnqn", FAIL, "/etc/nvme/hostnqn missing; run nvme gen-hostnqn > /etc/nvme/hostnqn")

    def _check_multipath_conf(self, protocol: str) -> Optional[multipath.Block]:
        main = os.path.join(self.etc, "multipath.conf")
        text = self._read(main)
        if text is None:
            self._add("multipath.conf", FAIL, "/etc/multipath.conf missing; run mpathconf --enable")
            return None
        roots = [multipath.parse(text)]
        for frag in sorted(glob.glob(os.path.join(self.etc, "multipath", "conf.d", "*.conf"))):
            roots.append(multipath.parse(self._read(frag) or ""))
        conf = multipath.merge(roots)
        self._add("multipath.conf", OK, f"parsed {len(roots)} file(s)")

        defaults = {}
        for d in conf.sections("defaults"):
            defaults.update(d.attrs)
        for key, want in self.policy.get("defaults", {}).items():
            have = defaults.get(key)
            status = OK if have == want else WARN
            self._add(f"multipath.defaults.{key}", status,
                      f"{key}={have or '(unset)'}" + ("" if status == OK else f", expected {want}"))

        stanzas = []
        if protocol in ("iscsi", "fc", "any"):
            stanzas.append(("scsi", "PURE", "FlashArray", "pure_scsi_device"))
        if protocol in ("nvme-tcp", "any"):
            stanzas.append(("nvme", "NVME", "Pure Storage FlashArray", "pure_nvme_device"))
        for label, vendor, product, key in stanzas:
            dev = multipath.find_device(conf, vendor, product)
            if dev is None:
                self._add(f"multipath.device.{label}", WARN,
                          f'no device stanza for vendor "{vendor}" product "{product}"; multipathd falls back to built-in defaults')
                continue
            wrong = []
            for attr, want in self.policy.get(key, {}).items():
                have = dev.attrs.get(attr)
                if have != want:
                    wrong.append(f"{attr}={have or '(unset)'} (expected {want})")
            self._add(f"multipath.device.{label}", OK if not wrong else WARN,
                      "Pure device stanza matches policy" if not wrong else "; ".join(wrong))

        for entry in self.policy.get("require_blacklist", []):
            listed = multipath.is_blacklisted(conf, entry["vendor"], entry["product"])
            self._add(f"multipath.blacklist.{entry['vendor']}", OK if listed else WARN,
                      f"{entry['vendor']} {entry['product']} " +
                      ("blacklisted" if listed else "not blacklisted; multipathd may claim the VM's own disks"))
        return conf

    def _check_devices(self, conf: Optional[multipath.Block]) -> None:
        block = os.path.join(self.sys, "block")
        pure_sd = {}
        for dev in glob.glob(os.path.join(block, "sd*")):
            vendor = (self._read(os.path.join(dev, "device", "vendor")) or "").strip()
            if vendor == "PURE":
                pure_sd[os.path.basename(dev)] = dev
        nvme_pure = []
        for dev in glob.glob(os.path.join(block, "nvme*n*")):
            model = (self._read(os.path.join(dev, "device", "model")) or "").strip()
            if "Pure" in model:
                nvme_pure.append(os.path.basename(dev))

        want_sched = self.policy.get("scheduler")
        bad_sched = []
        for name, dev in pure_sd.items():
            sched = self._read(os.path.join(dev, "queue", "scheduler")) or ""
            if want_sched and f"[{want_sched}]" not in sched:
                bad_sched.append(name)
        if pure_sd and want_sched:
            self._add("device.scheduler", OK if not bad_sched else WARN,
                      f"all Pure paths use '{want_sched}'" if not bad_sched else
                      f"scheduler not '{want_sched}' on {', '.join(sorted(bad_sched))}; add a udev rule")

        min_paths = int(self.policy.get("min_paths", 2))
        degraded = []
        for dm in glob.glob(os.path.join(block, "dm-*")):
            uuid = (self._read(os.path.join(dm, "dm", "uuid")) or "").strip()
            if not uuid.startswith("mpath-"):
                continue
            wwid = uuid[len("mpath-"):]
            slaves = [os.path.basename(s) for s in glob.glob(os.path.join(dm, "slaves", "*"))]
            if not (wwid.startswith(PURE_WWID_PREFIX) or any(s in pure_sd for s in slaves)):
                continue
            running = 0
            for s in slaves:
                state = (self._read(os.path.join(block, s, "device", "state")) or "running").strip()
                if state == "running":
                    running += 1
            name = (self._read(os.path.join(dm, "dm", "name")) or "").strip()
            self.paths.append({"dm": os.path.basename(dm), "name": name, "wwid": wwid,
                               "paths": len(slaves), "running": running})
            if running < min_paths:
                degraded.append(f"{name or os.path.basename(dm)} ({running}/{len(slaves)} running)")
        if self.paths:
            status = OK if not degraded else (FAIL if any(p["running"] == 0 for p in self.paths) else WARN)
            self._add("device.paths", status,
                      f"{len(self.paths)} Pure multipath device(s), all with >= {min_paths} running paths"
                      if not degraded else "degraded: " + ", ".join(degraded))
        else:
            self._add("device.paths", INFO, "no Pure volumes attached to this node right now")
        if pure_sd and conf is not None and not self.paths:
            self._add("device.unclaimed", WARN,
                      f"{len(pure_sd)} Pure SCSI path(s) present but none are under dm-multipath")
        self.info["pure_scsi_paths"] = str(len(pure_sd))
        self.info["pure_nvme_namespaces"] = str(len(nvme_pure))

    def _check_kata(self, mods: set) -> None:
        """Kata runs each pod in a VM. On vSphere that is nested virtualization:
        the RHEL VM needs VT-x/AMD-V exposed by ESXi and a working /dev/kvm."""
        mode = self.policy.get("kata", "auto")  # auto | required | off
        conf_dir = self.policy.get("containerd_config_dir", "var/lib/rancher/rke2/agent/etc/containerd")
        conf_path = os.path.join(self.root, conf_dir, "config.toml")
        text = self._read(conf_path)
        if text is None and os.path.exists(conf_path):
            self._add("kata.containerd_readable", WARN,
                      "cannot read RKE2 containerd config.toml; node-check must run as uid 0 to see Kata handlers")
        text = text or ""
        # Matches containerd 1.x (plugins."io.containerd.grpc.v1.cri"...) and 2.x
        # (plugins.'io.containerd.cri.v1.runtime'...) runtime tables.
        self.kata_handlers = sorted(set(re.findall(
            r"containerd\.runtimes\.['\"]?(kata[\w-]*)['\"]?\]", text)))
        configured = bool(self.kata_handlers)
        if not configured and mode != "required":
            # Kata not installed here (for example, control-plane nodes): report capability only.
            fail_status = INFO
        else:
            fail_status = FAIL

        cpuinfo = self._read(os.path.join(self.proc, "cpuinfo")) or ""
        flags = set()
        for line in cpuinfo.splitlines():
            if line.startswith("flags"):
                flags.update(line.split(":", 1)[1].split())
                break
        vendor = (self._read(os.path.join(self.sys, "class", "dmi", "id", "sys_vendor")) or "").strip()
        hint = (" In vCenter: power the VM off, Edit Settings > CPU > enable 'Expose hardware "
                "assisted virtualization to the guest OS'." if "VMware" in vendor else "")
        has_virt = bool(flags & {"vmx", "svm"})
        self._add("kata.cpu_virtualization", OK if has_virt else fail_status,
                  "CPU exposes " + "/".join(sorted(flags & {"vmx", "svm"})) if has_virt
                  else "no vmx/svm CPU flag; nested VMs cannot start." + hint)

        has_kvm = os.path.exists(os.path.join(self.sys, "class", "misc", "kvm"))
        kvm_mod = next((m for m in ("kvm_intel", "kvm_amd") if m in mods), "")
        self._add("kata.kvm", OK if has_kvm else fail_status,
                  f"/dev/kvm available ({kvm_mod or 'kvm'})" if has_kvm
                  else "/dev/kvm missing; load kvm_intel or kvm_amd (needs the CPU flag above)")

        if configured:
            self._add("kata.containerd", OK, "containerd runtime handlers: " + ", ".join(self.kata_handlers))
            shim = os.path.join(self.root, "opt", "kata", "bin", "containerd-shim-kata-v2")
            self._add("kata.shim", OK if os.path.exists(shim) else FAIL,
                      "Kata shim installed in /opt/kata/bin" if os.path.exists(shim)
                      else "containerd has a Kata handler but /opt/kata/bin/containerd-shim-kata-v2 is missing")
        else:
            self._add("kata.containerd", WARN if mode == "required" else INFO,
                      "no Kata runtime handler in RKE2 containerd config" +
                      ("; install kata-deploy configured for RKE2" if mode == "required" else ""))
        self.kata_capable = has_virt and has_kvm
        self.info["kata_capable"] = str(self.kata_capable).lower()

    # -- output ------------------------------------------------------------

    def report(self, node: str) -> dict:
        worst = max((SEVERITY[r.status] for r in self.results), default=0)
        return {
            "node": node,
            "status": {0: OK, 1: WARN, 2: FAIL}[worst],
            "checks": [r.as_dict() for r in self.results],
            "info": self.info,
            "multipath_devices": self.paths,
        }

    def metrics(self, node: str) -> Snapshot:
        snap = Snapshot()
        for r in self.results:
            snap.add("ptk_node_check_status", SEVERITY[r.status],
                     {"node": node, "check": r.check},
                     help="0=ok/info, 1=warn, 2=fail")
        worst = max((SEVERITY[r.status] for r in self.results), default=0)
        snap.add("ptk_node_ready", 1 if worst < 2 else 0, {"node": node},
                 help="1 when no check failed on this node")
        if "iqn" in self.info:
            snap.add("ptk_node_iscsi_initiator_info", 1, {"node": node, "iqn": self.info["iqn"]},
                     help="iSCSI initiator name; count by iqn > 1 means cloned VMs share an IQN")
        if "hostnqn" in self.info:
            snap.add("ptk_node_nvme_hostnqn_info", 1, {"node": node, "hostnqn": self.info["hostnqn"]},
                     help="NVMe host NQN; must be unique per node")
        if "machine_id" in self.info:
            snap.add("ptk_node_machine_id_info", 1, {"node": node, "machine_id": self.info["machine_id"]},
                     help="/etc/machine-id; must be unique per node")
        if self.rke2_mode:
            snap.add("ptk_node_rke2_mode_info", 1, {"node": node, "mode": self.rke2_mode},
                     help="RKE2 service running on this host (server or agent)")
            snap.add("ptk_node_etcd_running", 1 if self.etcd_running else 0, {"node": node},
                     help="1 if an etcd process runs on this host")
        for i, c in enumerate(self.cni_configs):
            snap.add("ptk_node_cni_config_info", 1,
                     {"node": node, "file": str(c["file"]), "plugins": ",".join(c["plugins"]),
                      "active": "true" if i == 0 else "false"},
                     help="CNI config files in /etc/cni/net.d; containerd uses the first one")
        if self.policy.get("kata", "auto") != "off":
            snap.add("ptk_node_kata_capable", 1 if self.kata_capable else 0, {"node": node},
                     help="1 when the node can start Kata VMs (vmx/svm and /dev/kvm)")
            for handler in self.kata_handlers:
                snap.add("ptk_node_kata_runtime_info", 1, {"node": node, "handler": handler},
                         help="Kata runtime handler present in containerd config")
        for p in self.paths:
            labels = {"node": node, "wwid": str(p["wwid"]), "dm": str(p["dm"])}
            snap.add("ptk_multipath_paths", p["paths"], labels, help="Paths for a Pure multipath device")
            snap.add("ptk_multipath_paths_running", p["running"], labels, help="Paths in running state")
        return snap


def node_name() -> str:
    return os.environ.get("NODE_NAME") or socket.gethostname()
