"""The morpheus.net incident from the monitoring context pack, as API/etcd fixtures."""

import json

from ptk.kube import KubeError

CP = [{"name": f"c0{i}.morpheus.net", "ip": f"10.5.5.14{i - 1}"} for i in (1, 2, 3)]
WORKERS = [f"w{i:02d}.morpheus.net" for i in range(1, 14)]
W08_ID = int("911dd1653e70620", 16)

CONFIG = {
    "cluster_name": "morpheus-net-rke2",
    "node_count_total": 16,
    "control_plane_nodes": CP,
    "kubernetes_service_ip": "10.43.0.1",
    "etcd": {"endpoint": "https://127.0.0.1:2379", "cert_file": "x", "key_file": "x", "ca_file": "x",
             "known_unintended_members": [{"id": "911dd1653e70620", "name": "w08.morpheus.net"}]},
    "api_lb_url": "https://rke2-api.morpheus.net:9345",
    "rancher": {"url": "https://rancher.morpheus.net", "namespace": "cattle-system"},
    "harbor_url": "https://valhalla.morpheus.net",
    "runbooks": {"etcd_stale_member": "docs/runbooks/etcd-remove-stale-member.md"},
    "timeout": 1,
}


def _cond(ready, when="2026-09-25T20:00:00Z"):
    return {"conditions": [{"type": "Ready", "status": "True" if ready else "False", "lastTransitionTime": when}]}


def node(name, mode, roles=()):
    labels = {"kubernetes.io/hostname": name}
    labels.update({f"node-role.kubernetes.io/{r}": "true" for r in roles})
    return {"metadata": {"name": name, "labels": labels,
                         "annotations": {"rke2.io/node-args": json.dumps([mode, "--config", "/etc/rancher/rke2/config.yaml"])}},
            "status": _cond(True)}


def pod(ns, name, node_name, ready, labels=None, restarts=0, waiting=None, owner=None):
    cs = {"name": "c", "image": "valhalla.morpheus.net/x:1", "restartCount": restarts, "state": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    meta = {"name": name, "namespace": ns, "labels": labels or {}}
    if owner:
        meta["ownerReferences"] = [{"kind": "DaemonSet", "name": owner}]
    return {"metadata": meta, "spec": {"nodeName": node_name},
            "status": dict(_cond(ready), phase="Running", containerStatuses=[cs])}


def build(fixed=False):
    """fixed=False: the state described in the pack. fixed=True: after recovery."""
    nodes = [node(n["name"], "server", ("control-plane", "etcd", "master")) for n in CP]
    for w in WORKERS:
        roles = ("etcd", "control-plane") if (w.startswith("w08") and not fixed) else ()
        nodes.append(node(w, "agent", roles))
    names = [n["metadata"]["name"] for n in nodes]
    ready_cilium = names if fixed else names[:7]  # 7/16 Ready in the pack
    cilium_pods = [pod("kube-system", f"cilium-{i:02d}", n, n in ready_cilium, {"k8s-app": "cilium"},
                       restarts=0 if n in ready_cilium else 12, owner="cilium") for i, n in enumerate(names)]
    canal = [] if fixed else [pod("kube-system", "helm-install-rke2-canal-x7k2p", "c01.morpheus.net", False)]
    webhook_ready = fixed
    objs = {
        "/api/v1/nodes": nodes,
        "/api/v1/namespaces/default/endpoints/kubernetes":
            {"subsets": [{"addresses": [{"ip": n["ip"]} for n in CP]}]},
        "/apis/apps/v1/namespaces/kube-system/daemonsets/cilium":
            {"status": {"desiredNumberScheduled": 16, "currentNumberScheduled": 16, "numberReady": len(ready_cilium),
                        "numberAvailable": len(ready_cilium), "updatedNumberScheduled": 16}},
        "/api/v1/namespaces/kube-system/pods?labelSelector=k8s-app%3Dcilium": cilium_pods,
        "/apis/apps/v1/namespaces/kube-system/deployments/cilium-operator":
            {"spec": {"replicas": 2}, "status": {"readyReplicas": 2}},
        "/apis/apps/v1/daemonsets": [{"metadata": {"name": "cilium", "namespace": "kube-system"}, "status": {}},
                                     {"metadata": {"name": "rke2-ingress-nginx-controller", "namespace": "kube-system"}, "status": {}}],
        "/api/v1/namespaces/kube-system/pods": cilium_pods + canal,
        "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts":
            [{"metadata": {"name": "rke2-ingress-nginx"}}, {"metadata": {"name": "rke2-coredns"}}],
        "/api/v1/namespaces/ptk-system/pods?labelSelector=app.kubernetes.io%2Fname%3Dptk-node-check":
            [pod("ptk-system", f"ptk-node-check-{i}", n, n in ready_cilium) for i, n in enumerate(names)],
        "/apis/apps/v1/namespaces/cattle-system/deployments/rancher":
            {"spec": {"replicas": 3}, "status": {"readyReplicas": 3 if fixed else 0}},
        "/api/v1/namespaces/cattle-system/pods?labelSelector=app%3Drancher":
            [pod("cattle-system", f"rancher-abc{i}", n, fixed, restarts=0 if fixed else 9)
             for i, n in enumerate(["w10.morpheus.net", "w12.morpheus.net", "w01.morpheus.net"])],
        "/apis/apps/v1/namespaces/cattle-system/deployments/rancher-webhook":
            {"spec": {"replicas": 1}, "status": {"readyReplicas": 1 if webhook_ready else 0}},
        "/apis/discovery.k8s.io/v1/namespaces/cattle-system/endpointslices?labelSelector=kubernetes.io%2Fservice-name%3Drancher-webhook":
            [{"endpoints": [{"addresses": ["10.42.13.7"], "conditions": {"ready": webhook_ready}}]}],
        "/api/v1/namespaces/cattle-system/pods?labelSelector=app%3Drancher-webhook":
            [pod("cattle-system", "rancher-webhook-5d8f-qx2", "w13.morpheus.net", webhook_ready)],
        "/api/v1/namespaces/cattle-system/events": [] if fixed else [
            {"reason": "Unhealthy", "count": 41, "involvedObject": {"name": "rancher-webhook-5d8f-qx2"},
             "message": 'Startup probe failed: Get "https://10.42.13.7:9443/healthz": dial tcp 10.42.13.7:9443: connect: connection refused'},
            {"reason": "FailedCreate", "count": 3, "involvedObject": {"name": "rancher"},
             "message": 'Internal error occurred: failed calling webhook "rancher.cattle.io.namespace.create-non-kubesystem": '
                        'no endpoints available for service "rancher-webhook"'}],
        "/apis/apps/v1/namespaces/kube-system/daemonsets/rke2-ingress-nginx-controller":
            {"status": {"desiredNumberScheduled": 16, "numberReady": 16 if fixed else 7}},
        "/api/v1/namespaces/cattle-system/pods": [],
    }
    return FakeKube(objs), FakeEtcd(fixed)


class FakeKube:
    def __init__(self, objs):
        self.objs = objs

    def get(self, path):
        if path not in self.objs:
            raise KubeError(404, f"{path} not found")
        return self.objs[path]

    def list(self, path):
        v = self.get(path)
        return v if isinstance(v, list) else v.get("items", [])


class FakeEtcd:
    def __init__(self, fixed):
        members = [{"ID": str(1000 + i), "name": f"{n['name']}-a1b2c3d{i}",
                    "peerURLs": [f"https://{n['ip']}:2380"], "clientURLs": [f"https://{n['ip']}:2379"]}
                   for i, n in enumerate(CP)]
        if not fixed:
            members.append({"ID": str(W08_ID), "name": "w08.morpheus.net-5d3a9e01",
                            "peerURLs": ["https://10.5.5.158:2380"], "clientURLs": ["https://10.5.5.158:2379"]})
        self.members = members

    def __call__(self, url, path, method):
        def resp(body, status=200):
            return {"reachable": True, "status": status, "body": json.dumps(body), "error": "", "tls_untrusted": False}
        if path == "/v3/cluster/member/list":
            return resp({"header": {"cluster_id": "1"}, "members": self.members})
        if "10.5.5.158" in url:
            return {"reachable": False, "status": 0, "body": "", "error": "timed out", "tls_untrusted": False}
        if path == "/health":
            return resp({"health": "true", "reason": ""})
        if path == "/v3/maintenance/status":
            return resp({"header": {"member_id": "1001"}, "leader": "1001", "raftTerm": "57"})
        raise AssertionError(path)
