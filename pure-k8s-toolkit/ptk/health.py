"""Cluster health checks for an RKE2 cluster with embedded etcd, Cilium, Rancher,
ingress-nginx, HAProxy and Harbor. Produces a red/yellow/green summary per area,
a JSON report and Prometheus metrics.

It is built to keep working while the cluster is unhealthy: it talks to the API
server and etcd directly on a control-plane node (never through the Service
network it may be diagnosing), every check has a timeout, and one failing check
never stops the others.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from ptk import probes
from ptk.kube import Kube, KubeError
from ptk.metrics import Snapshot

OK, WARN, FAIL, UNKNOWN, SKIP = "ok", "warn", "fail", "unknown", "skip"
RANK = {SKIP: -1, OK: 0, UNKNOWN: 1, WARN: 1, FAIL: 2}
COLOR = {-1: "grey", 0: "green", 1: "yellow", 2: "red"}
AREAS = ["etcd", "api", "cilium", "roles", "rancher", "ingress", "haproxy", "harbor"]

DEFAULT_CONFIG: Dict[str, object] = {
    "cluster_name": "rke2",
    "node_count_total": 0,
    "control_plane_nodes": [],          # [{"name": "c01.example", "ip": "10.0.0.1"}]
    "kubernetes_service_ip": "10.43.0.1",
    "kube": {},                          # {} = in-cluster; or api/token_file/ca_file/cert_file/key_file
    "etcd": {},                          # endpoint/ca_file/cert_file/key_file/known_unintended_members
    "api_lb_url": "",                    # https://rke2-api.example:9345 (RKE2 supervisor)
    "rancher": {"url": "", "namespace": "cattle-system"},
    "harbor_url": "",
    "haproxy_stats_url": "",             # e.g. http://lb01:8404/stats;csv
    "intended_cni": "cilium",
    "undesired_cnis": ["canal", "calico", "flannel"],
    "ingress_daemonset": "kube-system/rke2-ingress-nginx-controller",
    "image_pull_namespaces": ["kube-system", "cattle-system"],
    "rke2_manifests_dir": "",            # host /var/lib/rancher/rke2/server/manifests, mounted read-only
    "ca_file": None,                     # extra CA bundle for Rancher/Harbor/HAProxy probes
    "timeout": 5,
    "runbooks": {},
}


def with_defaults(user: dict) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def load_config(path: Optional[str]) -> dict:
    if not path:
        return with_defaults({})
    with open(path) as f:
        return with_defaults(json.load(f))


class Check:
    def __init__(self, area: str, name: str, status: str, message: str, data: Optional[dict] = None) -> None:
        self.area, self.name, self.status, self.message, self.data = area, name, status, message, data or {}

    def as_dict(self) -> dict:
        d = {"area": self.area, "check": self.name, "status": self.status, "message": self.message}
        if self.data:
            d["data"] = self.data
        return d


def _ready(obj: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in obj.get("status", {}).get("conditions", []))


def _ready_transition(obj: dict) -> str:
    for c in obj.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready":
            return c.get("lastTransitionTime", "")
    return ""


def _restarts(pod: dict) -> int:
    return sum(cs.get("restartCount", 0) for cs in pod.get("status", {}).get("containerStatuses", []) or [])


def _short(name: str) -> str:
    return name.split(".")[0]


class HealthChecker:
    def __init__(self, cfg: dict, kube: Optional[Kube] = None,
                 etcd_request: Optional[Callable[..., Dict[str, object]]] = None) -> None:
        self.cfg = with_defaults(cfg)
        self.timeout = float(cfg.get("timeout", 5))
        self._kube = kube
        self._kube_error = ""
        self._etcd_request = etcd_request
        self.checks: List[Check] = []
        self.facts: Dict[str, object] = {}
        self._cache: Dict[str, object] = {}

    # -- plumbing ----------------------------------------------------------

    @property
    def kube(self) -> Optional[Kube]:
        if self._kube is None and not self._kube_error:
            k = self.cfg.get("kube") or {}
            try:
                self._kube = Kube(api=k.get("api"), token_file=k.get("token_file"), ca_file=k.get("ca_file"),
                                  cert_file=k.get("cert_file"), key_file=k.get("key_file"),
                                  timeout=self.timeout * 2)
            except Exception as exc:  # noqa: BLE001 - reported as a check result
                self._kube_error = f"{type(exc).__name__}: {exc}"
        return self._kube

    def _list(self, path: str) -> List[dict]:
        if path not in self._cache:
            if self.kube is None:
                raise RuntimeError(f"no Kubernetes API client: {self._kube_error}")
            self._cache[path] = self.kube.list(path)
        return self._cache[path]  # type: ignore[return-value]

    def _get(self, path: str) -> dict:
        if path not in self._cache:
            if self.kube is None:
                raise RuntimeError(f"no Kubernetes API client: {self._kube_error}")
            self._cache[path] = self.kube.get(path)
        return self._cache[path]  # type: ignore[return-value]

    def add(self, area: str, name: str, status: str, message: str, data: Optional[dict] = None) -> None:
        self.checks.append(Check(area, name, status, message, data))

    def _run(self, area: str, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except KubeError as exc:
            if exc.status == 404:
                self.add(area, name, SKIP, f"not present in this cluster ({exc})")
            else:
                self.add(area, name, UNKNOWN, f"API error: {exc}")
        except Exception as exc:  # noqa: BLE001 - one failing check must not stop the rest
            self.add(area, name, UNKNOWN, f"check could not run: {type(exc).__name__}: {exc}")

    def _probe(self, url: str, **kw: object) -> Dict[str, object]:
        kw.setdefault("ca_file", self.cfg.get("ca_file"))
        return probes.https(url, timeout=self.timeout, **kw)  # type: ignore[arg-type]

    @property
    def cp_nodes(self) -> List[dict]:
        return list(self.cfg.get("control_plane_nodes") or [])

    # -- run ---------------------------------------------------------------

    def run(self) -> List[Check]:
        self.checks, self.facts, self._cache = [], {}, {}
        started = time.time()
        steps = [
            ("etcd", "membership", self.check_etcd),
            ("etcd", "peer_ports", self.check_etcd_peers),
            ("api", "control_plane_apiservers", self.check_apiservers),
            ("api", "supervisor_lb", self.check_supervisor_lb),
            ("api", "kubernetes_endpoints", self.check_kubernetes_endpoints),
            ("api", "clusterip_from_this_host", self.check_clusterip_host),
            ("api", "nodes", self.check_nodes),
            ("cilium", "agents", self.check_cilium),
            ("cilium", "operator", self.check_cilium_operator),
            ("cilium", "cni_conflicts", self.check_cni_conflicts),
            ("cilium", "pod_service_routes", self.check_pod_service_routes),
            ("roles", "node_roles", self.check_roles),
            ("rancher", "rancher", self.check_rancher),
            ("rancher", "webhook", self.check_rancher_webhook),
            ("rancher", "events", self.check_rancher_events),
            ("rancher", "frontend", self.check_rancher_frontend),
            ("ingress", "controller", self.check_ingress),
            ("haproxy", "backends", self.check_haproxy),
            ("harbor", "health", self.check_harbor),
            ("harbor", "image_pulls", self.check_image_pulls),
        ]
        for area, name, fn in steps:
            self._run(area, name, fn)
        self.facts["duration_seconds"] = round(time.time() - started, 2)
        self.facts["finished_at"] = time.time()
        return self.checks

    # -- etcd --------------------------------------------------------------

    def _etcd(self, url: str, path: str, method: str = "POST") -> Dict[str, object]:
        e = self.cfg.get("etcd") or {}
        if self._etcd_request:
            return self._etcd_request(url, path, method)
        return probes.https(url.rstrip("/") + path, timeout=self.timeout, ca_file=e.get("ca_file"),
                            cert_file=e.get("cert_file"), key_file=e.get("key_file"),
                            method=method, data=b"{}" if method == "POST" else None)

    def check_etcd(self) -> None:
        e = self.cfg.get("etcd") or {}
        endpoint = e.get("endpoint")
        if not endpoint or not e.get("cert_file"):
            self.add("etcd", "membership", SKIP, "no etcd client certificate configured (run on a control-plane node)")
            return
        r = self._etcd(endpoint, "/v3/cluster/member/list")
        if not r["reachable"] or r["status"] != 200:
            self.add("etcd", "membership", FAIL, f"cannot list etcd members at {endpoint}: {r['error'] or r['status']}")
            return
        members = json.loads(str(r["body"])).get("members", [])
        expected_ips = {n["ip"] for n in self.cp_nodes}
        expected_names = {_short(n["name"]) for n in self.cp_nodes}
        known = {str(k.get("id", "")).lower(): k for k in e.get("known_unintended_members", [])}

        rows = []
        for m in members:
            mid = format(int(m.get("ID", 0)), "x")
            client_urls = m.get("clientURLs") or []
            peer_ips = sorted({urlparse(u).hostname or "" for u in m.get("peerURLs", [])} - {""})
            name = m.get("name", "")
            host = re.sub(r"-[0-9a-f]{8}$", "", name)  # RKE2 names members <hostname>-<8 hex>
            expected = bool(set(peer_ips) & expected_ips) or _short(host) in expected_names
            health = self._etcd(client_urls[0], "/health", "GET") if client_urls else {"reachable": False, "error": "no client URL"}
            try:
                healthy = bool(health["reachable"]) and json.loads(str(health.get("body") or "{}")).get("health") == "true"
            except ValueError:
                healthy = False
            status = self._etcd(client_urls[0], "/v3/maintenance/status") if client_urls and healthy else {}
            body = json.loads(str(status.get("body") or "{}")) if status.get("status") == 200 else {}
            rows.append({"id": mid, "name": name, "host": host, "peer_ips": peer_ips, "expected": expected,
                         "healthy": healthy, "learner": bool(m.get("isLearner")),
                         "leader": format(int(body["leader"]), "x") if body.get("leader") else "",
                         "raft_term": int(body.get("raftTerm") or body.get("raft_term") or 0),
                         "error": "" if healthy else str(health.get("error") or health.get("status"))})

        voters = [r for r in rows if not r["learner"]]
        quorum = len(voters) // 2 + 1
        up = sum(1 for r in voters if r["healthy"])
        tolerance = up - quorum
        leaders = {r["leader"] for r in rows if r["leader"]}
        leader_id = next(iter(leaders)) if len(leaders) == 1 else ""
        leader_name = next((r["host"] for r in rows if r["id"] == leader_id), "")
        unexpected = [r for r in rows if not r["expected"]]
        self.facts["etcd"] = {"members": rows, "voters": len(voters), "quorum": quorum, "healthy_voters": up,
                              "failure_tolerance": tolerance, "leader": leader_name or leader_id,
                              "raft_term": max((r["raft_term"] for r in rows), default=0)}

        expected_count = len(self.cp_nodes) or 3
        if unexpected:
            parts = []
            for r in unexpected:
                k = known.get(r["id"])
                note = f" (known issue; runbook {self.cfg.get('runbooks', {}).get('etcd_stale_member', '')})" if k else ""
                parts.append(f"{r['host'] or r['id']} [{r['id']}, {','.join(r['peer_ips'])}]{note}")
            self.add("etcd", "membership", FAIL,
                     f"{len(rows)} members, expected {expected_count}; unexpected: " + "; ".join(parts),
                     {"unexpected": [r["id"] for r in unexpected]})
        elif len(rows) != expected_count:
            self.add("etcd", "membership", FAIL, f"{len(rows)} members, expected {expected_count}")
        else:
            self.add("etcd", "membership", OK, f"{len(rows)} members, all expected: " +
                     ", ".join(sorted(r["host"] for r in rows)))

        unhealthy = [f"{r['host'] or r['id']} ({r['error']})" for r in rows if not r["healthy"]]
        if tolerance < 0:
            self.add("etcd", "quorum", FAIL, f"quorum lost: {up}/{len(voters)} voters healthy, need {quorum}")
        elif tolerance == 0:
            self.add("etcd", "quorum", FAIL,
                     f"no failure tolerance: {up}/{len(voters)} voters healthy, quorum {quorum}; "
                     "any control-plane restart will stop the API" + (f"; unhealthy: {', '.join(unhealthy)}" if unhealthy else ""))
        else:
            self.add("etcd", "quorum", OK if not unhealthy else WARN,
                     f"{up}/{len(voters)} voters healthy, tolerates {tolerance} more failure(s)" +
                     (f"; unhealthy: {', '.join(unhealthy)}" if unhealthy else ""))

        if len(leaders) > 1:
            self.add("etcd", "leader", FAIL, f"members disagree on the leader: {sorted(leaders)}")
        elif not leader_id:
            self.add("etcd", "leader", FAIL, "no etcd leader reported by any healthy member")
        else:
            self.add("etcd", "leader", OK, f"leader {leader_name or leader_id}, raft term {self.facts['etcd']['raft_term']}")

    def check_etcd_peers(self) -> None:
        if not self.cp_nodes:
            self.add("etcd", "peer_ports", SKIP, "no control_plane_nodes configured")
            return
        bad = []
        for n in self.cp_nodes:
            r = probes.tcp(n["ip"], 2380, timeout=self.timeout)
            if not r["reachable"]:
                bad.append(f"{_short(n['name'])} {n['ip']}:2380 ({r['error']})")
        self.add("etcd", "peer_ports", FAIL if bad else OK,
                 "etcd peer port unreachable from this host: " + ", ".join(bad) if bad
                 else "TCP 2380 reachable on all control-plane nodes")

    # -- API ---------------------------------------------------------------

    def check_apiservers(self) -> None:
        if not self.cp_nodes:
            self.add("api", "control_plane_apiservers", SKIP, "no control_plane_nodes configured")
            return
        k = self.cfg.get("kube") or {}
        token = ""
        if k.get("token_file") and os.path.exists(k["token_file"]):
            with open(k["token_file"]) as f:
                token = f.read().strip()
        down, results = [], {}
        for n in self.cp_nodes:
            r = self._probe(f"https://{n['ip']}:6443/readyz", verify=False, token=token,
                            cert_file=k.get("cert_file"), key_file=k.get("key_file"))
            results[_short(n["name"])] = r["status"] or r["error"]
            if not r["reachable"]:
                down.append(f"{_short(n['name'])} ({r['error']})")
            elif r["status"] not in (200, 401, 403):
                down.append(f"{_short(n['name'])} (readyz HTTP {r['status']})")
        self.add("api", "control_plane_apiservers", FAIL if len(down) == len(self.cp_nodes) else WARN if down else OK,
                 "kube-apiserver :6443 not ready on " + ", ".join(down) if down
                 else f"kube-apiserver :6443 ready on all {len(self.cp_nodes)} control-plane nodes", results)

    def check_supervisor_lb(self) -> None:
        url = self.cfg.get("api_lb_url")
        if not url:
            self.add("api", "supervisor_lb", SKIP, "api_lb_url not configured")
            return
        r = self._probe(url.rstrip("/") + "/ping", ca_file=(self.cfg.get("kube") or {}).get("ca_file"))
        if not r["reachable"]:
            self.add("api", "supervisor_lb", FAIL, f"{url} unreachable: {r['error']}")
        elif r["status"] == 200:
            self.add("api", "supervisor_lb", WARN if r["tls_untrusted"] else OK,
                     f"{url}/ping answered in {r['latency_ms']} ms" + (f"; {r['error']}" if r["tls_untrusted"] else ""))
        else:
            self.add("api", "supervisor_lb", WARN, f"{url}/ping returned HTTP {r['status']}")

    def check_kubernetes_endpoints(self) -> None:
        ep = self._get("/api/v1/namespaces/default/endpoints/kubernetes")
        ips = sorted({a["ip"] for s in ep.get("subsets", []) or [] for a in s.get("addresses", []) or []})
        expected = sorted(n["ip"] for n in self.cp_nodes)
        self.facts["kubernetes_endpoints"] = ips
        extra, missing = sorted(set(ips) - set(expected)), sorted(set(expected) - set(ips))
        if not ips:
            self.add("api", "kubernetes_endpoints", FAIL, "the kubernetes Service has no endpoints")
        elif extra:
            self.add("api", "kubernetes_endpoints", FAIL, f"kubernetes Service points at non-control-plane IPs: {', '.join(extra)}")
        elif missing and expected:
            self.add("api", "kubernetes_endpoints", WARN, f"API servers missing from the kubernetes Service: {', '.join(missing)}")
        else:
            self.add("api", "kubernetes_endpoints", OK, f"kubernetes Service endpoints: {', '.join(ips)}")

    def check_clusterip_host(self) -> None:
        ip = self.cfg.get("kubernetes_service_ip")
        r = self._probe(f"https://{ip}:443/version", verify=False)
        if r["reachable"]:
            self.add("api", "clusterip_from_this_host", OK, f"{ip}:443 answered HTTP {r['status']} in {r['latency_ms']} ms (host network)")
        else:
            self.add("api", "clusterip_from_this_host", FAIL,
                     f"{ip}:443 unreachable from this host ({r['error']}); Service routing is broken here "
                     "even though the API server may be up (compare control_plane_apiservers)")

    def check_nodes(self) -> None:
        nodes = self._list("/api/v1/nodes")
        not_ready = sorted(_short(n["metadata"]["name"]) for n in nodes if not _ready(n))
        want = int(self.cfg.get("node_count_total") or 0)
        self.facts["nodes"] = {"total": len(nodes), "not_ready": not_ready}
        msg = f"{len(nodes) - len(not_ready)}/{len(nodes)} nodes Ready"
        if want and len(nodes) != want:
            msg += f"; expected {want} nodes"
        if not_ready:
            msg += f"; NotReady: {', '.join(not_ready)}"
        status = FAIL if len(not_ready) > len(nodes) // 2 else WARN if not_ready or (want and len(nodes) != want) else OK
        self.add("api", "nodes", status, msg)

    # -- Cilium / CNI ------------------------------------------------------

    def check_cilium(self) -> None:
        ds = self._get("/apis/apps/v1/namespaces/kube-system/daemonsets/cilium")
        st = ds.get("status", {})
        desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
        counts = {"desired": desired, "current": st.get("currentNumberScheduled", 0), "ready": ready,
                  "available": st.get("numberAvailable", 0), "updated": st.get("updatedNumberScheduled", 0)}
        pods = self._list("/api/v1/namespaces/kube-system/pods?labelSelector=k8s-app%3Dcilium")
        per_node = {}
        for p in pods:
            node = p.get("spec", {}).get("nodeName", "")
            per_node[_short(node)] = {"ready": _ready(p), "restarts": _restarts(p),
                                      "ready_since": _ready_transition(p), "phase": p.get("status", {}).get("phase", "")}
        nodes = [_short(n["metadata"]["name"]) for n in self._list("/api/v1/nodes")]
        without = sorted(n for n in nodes if not per_node.get(n, {}).get("ready"))
        restarting = sorted(n for n, v in per_node.items() if v["restarts"] >= 5)
        self.facts["cilium"] = dict(counts, per_node=per_node, nodes_without_ready_agent=without)
        msg = f"cilium DaemonSet desired {desired}, ready {ready}, available {counts['available']}"
        if without:
            msg += f"; no ready agent on: {', '.join(without)}"
        if restarting:
            msg += f"; restarting (>=5): {', '.join(restarting)}"
        status = OK
        if ready < desired or without:
            status = FAIL if ready <= desired // 2 or len(without) > 1 else WARN
        elif restarting:
            status = WARN
        self.add("cilium", "agents", status, msg, counts)

    def check_cilium_operator(self) -> None:
        d = self._get("/apis/apps/v1/namespaces/kube-system/deployments/cilium-operator")
        want, ready = d.get("spec", {}).get("replicas", 1), d.get("status", {}).get("readyReplicas", 0) or 0
        self.add("cilium", "operator", OK if ready >= want else FAIL if ready == 0 else WARN,
                 f"cilium-operator {ready}/{want} ready")

    def check_cni_conflicts(self) -> None:
        bad_names = [c.lower() for c in self.cfg.get("undesired_cnis", [])]
        pattern = re.compile("|".join(re.escape(c) for c in bad_names)) if bad_names else None
        found, flagged_ds = [], set()
        if pattern:
            for ds in self._list("/apis/apps/v1/daemonsets"):
                name = ds["metadata"]["name"]
                if pattern.search(name.lower()):
                    flagged_ds.add(name)
                    found.append(f"DaemonSet {ds['metadata']['namespace']}/{name} "
                                 f"({ds.get('status', {}).get('numberReady', 0)} ready)")
            for p in self._list("/api/v1/namespaces/kube-system/pods"):
                name = p["metadata"]["name"]
                owners = {o.get("name") for o in p["metadata"].get("ownerReferences", []) if o.get("kind") == "DaemonSet"}
                if pattern.search(name.lower()) and not owners & flagged_ds:
                    # e.g. helm-install-rke2-canal Jobs trying to deploy Canal
                    found.append(f"Pod kube-system/{name} on {_short(p.get('spec', {}).get('nodeName', '?'))}")
        try:
            charts = self._list("/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts")
        except KubeError as exc:
            if exc.status != 404:
                raise
            charts = []
        intended = str(self.cfg.get("intended_cni", "cilium")).lower()
        for hc in charts:
            name = hc["metadata"]["name"].lower()
            if pattern and pattern.search(name):
                found.append(f"HelmChart kube-system/{hc['metadata']['name']}")
            elif name == f"rke2-{intended}":
                # Cilium here is managed separately; RKE2's bundled chart would be a second owner.
                found.append(f"HelmChart kube-system/{hc['metadata']['name']} (RKE2-bundled {intended}, "
                             "but the intended install is managed separately)")
        mdir = self.cfg.get("rke2_manifests_dir")
        if mdir and os.path.isdir(mdir):
            for f in sorted(os.listdir(mdir)):
                if pattern and pattern.search(f.lower()) and not f.endswith(".skip"):
                    found.append(f"manifest {f} in RKE2 server manifests (add a .skip file to disable)")
        self.add("cilium", "cni_conflicts", FAIL if found else OK,
                 ("other CNI artifacts present alongside " + intended + ": " + "; ".join(found)) if found
                 else f"no {', '.join(bad_names)} DaemonSets, pods or HelmCharts")

    def check_pod_service_routes(self) -> None:
        """Reads node-check DaemonSet readiness: node-check reports NotReady on a node
        whose pods cannot reach the kubernetes Service (see ptk.nodecheck service probes)."""
        pods = self._list("/api/v1/namespaces/ptk-system/pods?labelSelector=app.kubernetes.io%2Fname%3Dptk-node-check")
        if not pods:
            self.add("cilium", "pod_service_routes", SKIP, "ptk node-check DaemonSet not deployed; per-node pod routing not measured")
            return
        failing = sorted(_short(p.get("spec", {}).get("nodeName", "?")) for p in pods if not _ready(p))
        ip = self.cfg.get("kubernetes_service_ip")
        self.add("cilium", "pod_service_routes", FAIL if failing else OK,
                 f"pods cannot reach {ip}:443 on: {', '.join(failing)}" if failing
                 else f"pods on all {len(pods)} nodes reach {ip}:443")

    # -- node roles --------------------------------------------------------

    def check_roles(self) -> None:
        nodes = self._list("/api/v1/nodes")
        cp_names = {_short(n["name"]) for n in self.cp_nodes}
        etcd_hosts = {_short(r["host"]) for r in (self.facts.get("etcd") or {}).get("members", [])}  # type: ignore[union-attr]
        problems, rows = [], []
        for n in nodes:
            name = _short(n["metadata"]["name"])
            labels = n["metadata"].get("labels", {})
            roles = sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")
                           and k.split("/", 1)[1] in ("etcd", "control-plane", "master"))
            try:
                args = json.loads(n["metadata"].get("annotations", {}).get("rke2.io/node-args", "[]"))
            except ValueError:
                args = []
            mode = args[0] if args and args[0] in ("server", "agent") else "unknown"
            in_etcd = name in etcd_hosts
            rows.append({"node": name, "mode": mode, "role_labels": roles, "etcd_member": in_etcd,
                         "intended": "control-plane" if name in cp_names else "worker"})
            if cp_names and name not in cp_names:
                if in_etcd:
                    problems.append((FAIL, f"{name}: worker is an etcd member (mode {mode})"))
                if roles:
                    problems.append((WARN, f"{name}: worker labelled {','.join(roles)} (mode {mode})"))
                if mode == "server":
                    problems.append((WARN, f"{name}: worker registered by rke2-server"))
            elif name in cp_names and mode == "agent":
                problems.append((FAIL, f"{name}: control-plane node is running rke2-agent"))
        self.facts["roles"] = rows
        if problems:
            worst = FAIL if any(s == FAIL for s, _ in problems) else WARN
            msg = "; ".join(m for _, m in problems)
            if any("labelled" in m for _, m in problems):
                msg += ". Remove stale role labels only after the node's etcd membership is removed"
            self.add("roles", "node_roles", worst, msg)
        else:
            self.add("roles", "node_roles", OK, "node role labels, RKE2 mode and etcd membership agree with the intended layout")

    # -- Rancher -----------------------------------------------------------

    def _deploy(self, ns: str, name: str) -> tuple:
        d = self._get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        return d.get("spec", {}).get("replicas", 1), d.get("status", {}).get("readyReplicas", 0) or 0

    def check_rancher(self) -> None:
        ns = (self.cfg.get("rancher") or {}).get("namespace", "cattle-system")
        want, ready = self._deploy(ns, "rancher")
        pods = self._list(f"/api/v1/namespaces/{ns}/pods?labelSelector=app%3Drancher")
        restarts = {_short(p["spec"].get("nodeName", "?")): _restarts(p) for p in pods}
        status = FAIL if ready == 0 else WARN if ready < want else OK
        self.add("rancher", "rancher", status, f"rancher {ready}/{want} ready; restarts {restarts}")

    def check_rancher_webhook(self) -> None:
        ns = (self.cfg.get("rancher") or {}).get("namespace", "cattle-system")
        want, ready = self._deploy(ns, "rancher-webhook")
        slices = self._list(f"/apis/discovery.k8s.io/v1/namespaces/{ns}/endpointslices"
                            "?labelSelector=kubernetes.io%2Fservice-name%3Drancher-webhook")
        ready_eps = sum(1 for s in slices for e in s.get("endpoints", []) or []
                        if (e.get("conditions") or {}).get("ready"))
        pods = self._list(f"/api/v1/namespaces/{ns}/pods?labelSelector=app%3Drancher-webhook")
        where = [f"{_short(p['spec'].get('nodeName', '?'))}:{'Ready' if _ready(p) else 'NotReady'}" for p in pods]
        if ready_eps == 0:
            self.add("rancher", "webhook", FAIL,
                     f"rancher-webhook Service has no ready endpoints ({ready}/{want} pods ready: {', '.join(where)}); "
                     "every Rancher call to the validating webhook fails. If the pod logs show "
                     f"'{self.cfg.get('kubernetes_service_ip')}/version ... i/o timeout', fix Service routing on its node first")
        else:
            self.add("rancher", "webhook", OK if ready >= want else WARN,
                     f"rancher-webhook {ready}/{want} ready, {ready_eps} ready endpoint(s): {', '.join(where)}")

    def check_rancher_events(self) -> None:
        ns = (self.cfg.get("rancher") or {}).get("namespace", "cattle-system")
        events = self._list(f"/api/v1/namespaces/{ns}/events")
        probe_fail, webhook_fail = [], []
        for ev in events:
            msg = ev.get("message", "") or ""
            obj = (ev.get("involvedObject") or {}).get("name", "")
            count = ev.get("count", 1) or 1
            if ev.get("reason") == "Unhealthy" and "9443" in msg:
                probe_fail.append(f"{obj} x{count}")
            if "failed calling webhook" in msg or "no endpoints available for service" in msg:
                webhook_fail.append(f"{obj}: {msg[:140]}")
        if webhook_fail:
            self.add("rancher", "events", FAIL, f"webhook call failures: {' | '.join(webhook_fail[:3])}")
        elif probe_fail:
            self.add("rancher", "events", WARN, f"webhook probe failures on :9443: {', '.join(probe_fail[:5])}")
        else:
            self.add("rancher", "events", OK, f"no webhook or :9443 probe failures in {len(events)} recent {ns} events")

    def check_rancher_frontend(self) -> None:
        url = (self.cfg.get("rancher") or {}).get("url")
        if not url:
            self.add("rancher", "frontend", SKIP, "rancher.url not configured")
            return
        r = self._probe(url.rstrip("/") + "/ping")
        if not r["reachable"]:
            self.add("rancher", "frontend", FAIL, f"{url} unreachable through the load balancer: {r['error']}")
        elif r["status"] == 200 and "pong" in str(r["body"]):
            self.add("rancher", "frontend", WARN if r["tls_untrusted"] else OK,
                     f"{url}/ping -> pong in {r['latency_ms']} ms" + (f"; {r['error']}" if r["tls_untrusted"] else ""))
        else:
            self.add("rancher", "frontend", FAIL, f"{url}/ping returned HTTP {r['status']} (ingress up, Rancher not serving)")

    # -- ingress / HAProxy / Harbor ------------------------------------------

    def check_ingress(self) -> None:
        ns, name = str(self.cfg.get("ingress_daemonset")).split("/", 1)
        ds = self._get(f"/apis/apps/v1/namespaces/{ns}/daemonsets/{name}")
        st = ds.get("status", {})
        desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
        self.add("ingress", "controller", OK if ready >= desired else FAIL if ready == 0 else WARN,
                 f"{name} {ready}/{desired} ready")

    def check_haproxy(self) -> None:
        url = self.cfg.get("haproxy_stats_url")
        if not url:
            # Without HAProxy's stats page, probe each supervisor backend directly.
            if not self.cp_nodes:
                self.add("haproxy", "backends", SKIP, "neither haproxy_stats_url nor control_plane_nodes configured")
                return
            bad = []
            for n in self.cp_nodes:
                r = probes.tcp(n["ip"], 9345, timeout=self.timeout)
                if not r["reachable"]:
                    bad.append(f"{_short(n['name'])}:9345 ({r['error']})")
            if bad:
                status = FAIL if len(bad) == len(self.cp_nodes) else WARN
                self.add("haproxy", "backends", status, "supervisor backends unreachable: " + ", ".join(bad))
            else:
                self.add("haproxy", "backends", OK,
                         "all supervisor backends accept TCP 9345 (set haproxy_stats_url for HAProxy's own view)")
            return
        if url.startswith("https"):
            r = self._probe(url)
        else:
            import urllib.request
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=self.timeout) as resp:
                    r = {"reachable": True, "status": resp.status, "body": resp.read().decode(errors="replace"), "error": ""}
            except OSError as exc:
                r = {"reachable": False, "status": 0, "body": "", "error": str(exc)}
        if not r["reachable"] or r["status"] != 200:
            self.add("haproxy", "backends", FAIL, f"HAProxy stats unavailable at {url}: {r['error'] or r['status']}")
            return
        down = []
        text = str(r["body"]).lstrip("# ")
        for row in csv.DictReader(io.StringIO(text)):
            sv = row.get("svname", "")
            if sv in ("FRONTEND", "BACKEND") or not sv:
                continue
            if not row.get("status", "").startswith("UP") and row.get("status") != "no check":
                down.append(f"{row.get('pxname')}/{sv} {row.get('status')}")
        self.add("haproxy", "backends", FAIL if down else OK,
                 ("HAProxy backends not UP: " + ", ".join(down)) if down else "all HAProxy backend servers UP")

    def check_harbor(self) -> None:
        url = self.cfg.get("harbor_url")
        if not url:
            self.add("harbor", "health", SKIP, "harbor_url not configured")
            return
        r = self._probe(url.rstrip("/") + "/api/v2.0/health")
        if not r["reachable"]:
            self.add("harbor", "health", FAIL, f"{url} unreachable: {r['error']}")
            return
        try:
            body = json.loads(str(r["body"]))
        except ValueError:
            body = {}
        bad = [f"{c.get('name')}: {c.get('error') or c.get('status')}" for c in body.get("components", [])
               if c.get("status") != "healthy"]
        status = OK if body.get("status") == "healthy" and not bad else FAIL if not body else WARN
        msg = f"Harbor {body.get('status', 'HTTP ' + str(r['status']))}" + (f"; unhealthy: {', '.join(bad)}" if bad else "")
        if r["tls_untrusted"]:
            status, msg = (WARN if status == OK else status), msg + f"; {r['error']}"
        self.add("harbor", "health", status, msg)

    def check_image_pulls(self) -> None:
        failing = []
        for ns in self.cfg.get("image_pull_namespaces", []):
            for p in self._list(f"/api/v1/namespaces/{ns}/pods"):
                for cs in (p.get("status", {}).get("containerStatuses") or []) + \
                          (p.get("status", {}).get("initContainerStatuses") or []):
                    reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
                    if reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                        failing.append(f"{ns}/{p['metadata']['name']} {cs.get('image')} ({reason})")
        self.add("harbor", "image_pulls", FAIL if failing else OK,
                 ("image pull failures: " + "; ".join(failing[:6]) + (f" (+{len(failing) - 6} more)" if len(failing) > 6 else ""))
                 if failing else "no image pull failures in " + ", ".join(self.cfg.get("image_pull_namespaces", [])))

    # -- output ------------------------------------------------------------

    def areas(self) -> Dict[str, str]:
        out = {}
        for area in AREAS:
            ranks = [RANK[c.status] for c in self.checks if c.area == area]
            out[area] = COLOR[max(ranks)] if ranks else "grey"
        return out

    def report(self) -> dict:
        areas = self.areas()
        overall = "red" if "red" in areas.values() else "yellow" if "yellow" in areas.values() else "green"
        return {"cluster": self.cfg.get("cluster_name"), "overall": overall, "areas": areas,
                "checks": [c.as_dict() for c in self.checks], "facts": self.facts}

    def summary(self) -> str:
        areas = self.areas()
        icon = {"green": "GREEN ", "yellow": "YELLOW", "red": "RED   ", "grey": "n/a   "}
        lines = [f"{self.cfg.get('cluster_name')}: {self.report()['overall'].upper()}"]
        for area in AREAS:
            lines.append(f"  {icon[areas[area]]}  {area}")
            for c in self.checks:
                if c.area == area and c.status in (FAIL, WARN, UNKNOWN):
                    lines.append(f"            {c.status.upper():7} {c.name}: {c.message}")
        return "\n".join(lines)

    def metrics(self) -> Snapshot:
        snap = Snapshot()
        value = {"green": 0, "yellow": 1, "red": 2, "grey": -1}
        for area, color in self.areas().items():
            snap.add("ptk_health_area_status", value[color], {"area": area},
                     help="-1 not checked, 0 green, 1 yellow, 2 red")
        for c in self.checks:
            if c.status != SKIP:
                snap.add("ptk_health_check_status", RANK[c.status], {"area": c.area, "check": c.name},
                         help="0 ok, 1 warn/unknown, 2 fail")
        e = self.facts.get("etcd")
        if isinstance(e, dict):
            members = e["members"]
            snap.add("ptk_etcd_members", len(members), help="etcd members (voters and learners)")
            snap.add("ptk_etcd_unexpected_members", sum(1 for m in members if not m["expected"]),
                     help="etcd members whose IP is not a configured control-plane node")
            snap.add("ptk_etcd_failure_tolerance", e["failure_tolerance"],
                     help="healthy voters minus quorum; 0 means one more failure stops etcd")
            snap.add("ptk_etcd_has_leader", 1 if e["leader"] else 0)
            snap.add("ptk_etcd_raft_term", e["raft_term"], help="increases on every leader election")
            for m in members:
                snap.add("ptk_etcd_member_healthy", 1 if m["healthy"] else 0,
                         {"member_id": m["id"], "host": m["host"], "expected": str(m["expected"]).lower()})
        cil = self.facts.get("cilium")
        if isinstance(cil, dict):
            snap.add("ptk_cilium_agents_desired", cil["desired"])
            snap.add("ptk_cilium_agents_ready", cil["ready"])
            for node, v in cil["per_node"].items():
                snap.add("ptk_cilium_agent_ready", 1 if v["ready"] else 0, {"node": node})
                snap.add("ptk_cilium_agent_restarts", v["restarts"], {"node": node})
        snap.add("ptk_health_last_run_timestamp_seconds", time.time())
        snap.add("ptk_health_duration_seconds", float(self.facts.get("duration_seconds", 0)))
        return snap
