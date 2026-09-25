# Triage: Cilium agents not Ready → Service routing → Rancher down

This matches the failure chain in the morpheus.net monitoring context pack:

1. The `cilium` DaemonSet shows 16 desired but only 7 Ready.
2. Pods on the other 9 nodes can't reach Services, including the API at `10.43.0.1:443`.
3. The `rancher-webhook` pod (on w13) can't reach `https://10.43.0.1/version`, so it never binds :9443. Its startup probe fails and its Service has no endpoints.
4. Rancher's calls to its validating webhook fail, and Rancher crash-loops.

Fix from the bottom up. Nothing in Rancher needs changing until step 2 is healthy again.

Every command below only reads, except the one marked **changes state**.

## 0. Current picture

On c01:

```bash
journalctl -u ptk-health -n 80            # if the ptk-health timer is installed
# or run it once by hand (see deploy/health/ptk-health.service for the full podman command)
```

Without ptk:

```bash
kubectl -n kube-system get ds cilium -o wide
kubectl -n kube-system get pods -l k8s-app=cilium -o wide | sort -k7
```

The nodes whose agent is not `1/1 Running` are the nodes where pods lose Service routing.

## 1. Why are those agents not Ready?

Pick one failing node (for example w13):

```bash
POD=$(kubectl -n kube-system get pods -l k8s-app=cilium --field-selector spec.nodeName=w13.morpheus.net -o name)
kubectl -n kube-system describe "$POD" | sed -n '/Events/,$p'
kubectl -n kube-system logs "$POD" -c cilium-agent --tail=100
kubectl -n kube-system logs "$POD" -c cilium-agent --previous --tail=100
```

Sort what you find into one of these:

| Symptom in events or logs | Points to |
|---|---|
| `ErrImagePull` / `ImagePullBackOff` | Harbor (`valhalla.morpheus.net`) or `registries.yaml` on that node |
| Timeouts or connection refused talking to the API server | The agent's own path to the API (step 2) |
| Errors about CNI config, `cni.exclusive`, or another CNI | CNI conflict (step 3) |
| Health, BPF or datapath errors | Cilium itself. Compare with a Ready agent: `kubectl -n kube-system exec ds/cilium -c cilium-agent -- cilium-dbg status --verbose` (older releases: `cilium status`) |

## 2. The agent's path to the API server

With kube-proxy replacement, Cilium implements Services itself. That means its agents can't reach the API server through `10.43.0.1`. They need a direct address.

```bash
kubectl -n kube-system get cm cilium-config -o yaml | grep -E 'kube-proxy-replacement|k8s-service-host|k8s-service-port'
```

- If `kube-proxy-replacement` is enabled, `k8s-service-host` must be reachable **without** Services from every node. Two options on RKE2:
  - `127.0.0.1` port `6443`: the RKE2 agent's local load balancer, which runs on every node;
  - the controllers directly.
- A ClusterIP, a stale address from before the etcd restore, or one controller's IP (c01 had the peer-connectivity problem) would explain agents that can't start on some nodes.

Check from a failing node:

```bash
# on w13
curl -sk https://127.0.0.1:6443/version -o /dev/null -w '%{http_code}\n'   # any HTTP code = reachable
for ip in 10.5.5.140 10.5.5.141 10.5.5.142; do
  curl -sk --max-time 3 https://$ip:6443/version -o /dev/null -w "$ip %{http_code}\n"
done
```

## 3. Canal (or another CNI) trying to come up

On **each** of c01, c02 and c03:

```bash
grep -nE '^\s*cni:' /etc/rancher/rke2/config.yaml /etc/rancher/rke2/config.yaml.d/*.yaml 2>/dev/null   # expect: cni: none
ls /var/lib/rancher/rke2/server/manifests/ | grep -Ei 'canal|calico|flannel|cilium'
```

In the cluster:

```bash
kubectl get ds -A | grep -Ei 'canal|calico|flannel'
kubectl -n kube-system get helmcharts.helm.cattle.io | grep -Ei 'canal|calico|flannel|rke2-cilium'
kubectl -n kube-system get pods | grep -Ei 'canal|calico|flannel'
```

The cluster-reset restore on c02 involved editing c02's `config.yaml`, so check c02 first. A server that started once without `cni: none` deploys `rke2-canal`.

On each node:

```bash
ls -l /etc/cni/net.d/        # the only active file should be Cilium's; *.cilium_bak files are fine
```

node-check reports this per node as `cni.config`.

## 4. After the agents are Ready

```bash
kubectl -n kube-system get ds cilium          # READY should equal DESIRED (16)
kubectl -n ptk-system get pods -l app.kubernetes.io/name=ptk-node-check -o wide   # all Ready = routes OK
```

**Changes state:** if the webhook pod doesn't recover on its own within a few minutes, recreate it:

```bash
kubectl -n cattle-system delete pod -l app=rancher-webhook
kubectl -n cattle-system get endpointslices -l kubernetes.io/service-name=rancher-webhook
kubectl -n cattle-system rollout status deploy/rancher
curl -sk https://rancher.morpheus.net/ping     # expect: pong
```

## 5. The etcd topology

The w08 etcd member is a separate problem. It doesn't cause the routing failure, but while it exists the cluster can't survive a controller restart. So avoid rebooting c01–c03 while you do steps 1–4. Remove the member afterwards with [etcd-remove-stale-member.md](etcd-remove-stale-member.md).
