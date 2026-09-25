# ptk: Pure Storage toolkit for RKE2

One small container image with three jobs, built for an air-gapped RKE2 cluster on RHEL 9 VMs in vCenter that uses a Pure FlashArray for persistent storage:

| Mode | Runs as | What it answers |
|---|---|---|
| `ptk node-check` | DaemonSet on all 16 nodes | Is this node's multipath / iSCSI / NVMe-TCP stack set up the way Pure expects? Are all paths to every attached volume up? Can this VM run Kata Containers? |
| `ptk audit` | Deployment (1 replica) | Which namespace/PVC owns each array volume, and how well does it reduce? Which array volumes have no PV any more (orphans)? Which PVs point at a missing or destroyed volume? |
| `ptk health` | Deployment on a control-plane node (host network), or podman + systemd timer on c01 | Red/yellow/green for etcd, API reachability, Cilium, node roles, Rancher, ingress, HAProxy and Harbor, with the reason for every non-green result. |
| `ptk bench` | Job | What IOPS, throughput and p99 latency does each StorageClass deliver, and how much does running the pod under Kata cost? |

It fills gaps the stack you already have doesn't cover. Rancher sees PVCs but not the array. The FlashArray UI sees volumes but not namespaces. Neither checks the node settings in between, and those settings are where most FlashArray-on-Linux problems come from.

It does not replace:
- **The CSI driver.** Use Portworx with FlashArray Direct Access (FADA). Pure has retired Pure Service Orchestrator (PSO) in favor of Portworx.
- **Pure's array exporter** ([pure-fa-openmetrics-exporter](https://github.com/PureStorage-OpenConnect/pure-fa-openmetrics-exporter)) for array-wide capacity and performance metrics. Run it alongside ptk; ptk only adds the Kubernetes-side view.

## Why these checks matter on vSphere + RHEL 9

- **Cloned VMs share an iSCSI IQN.** If the 13 workers came from one vCenter template, they may all have the same `/etc/iscsi/initiatorname.iscsi`. The array then sees one host, and a volume can end up attached to two nodes at once, which corrupts data. Each node exposes its IQN as a metric, and the `PtkDuplicateIscsiInitiator` alert fires when two nodes share one. The same check covers NVMe host NQNs and `/etc/machine-id`.
- **The default `mpathconf --enable` config is wrong for Pure + Portworx.** It sets `user_friendly_names yes`, has no Pure device stanza, and doesn't blacklist VMware virtual disks. That last gap means multipathd can claim the VM's own OS disk.
- **Degraded paths are silent.** A volume on one of two paths keeps working until the second path fails. `ptk_multipath_paths_running` is joined to the PVC through the volume's WWID, so the alert names the affected namespace/PVC, not just `dm-7`.
- **Block-device scheduler.** The FlashArray schedules I/O itself, so its paths should use the `none` scheduler.
- **Kata needs nested virtualization.** Each Kata pod is a small VM inside your RHEL VM. That only works if vCenter exposes VT-x/AMD-V to the guest. The check reports it per node, and an alert fires if a node has Kata installed but can't start VMs.

## Notes for this cluster (morpheus.net)

Layout: three controllers `c01`–`c03` (10.5.5.140–142) running `rke2-server` with embedded etcd, and workers `w01`–`w13` running `rke2-agent`. RKE2 has `cni: none`, and Cilium, managed separately, is the only CNI. `lb01` (10.5.5.150, HAProxy) fronts `rke2-api.morpheus.net:9345` and `rancher.morpheus.net`. Images come from Harbor at `valhalla.morpheus.net`.

### Cluster health (`ptk health`)

`ptk health` answers "what's broken and where" for this cluster. It checks eight areas:

- **etcd:** members vs. c01–c03, health, leader, quorum and failure tolerance, peer port 2380.
- **API:** each controller's `:6443`, the supervisor through lb01, the `kubernetes` Service endpoints, and `10.43.0.1:443` from the host.
- **Cilium:** agents per node, the operator, and Canal/Calico/Flannel artifacts. It also covers per-node pod routing to `10.43.0.1:443`, measured by node-check.
- **Node roles:** role labels vs. RKE2 mode vs. etcd membership.
- **Rancher:** the Rancher pods, rancher-webhook endpoints, `:9443` probe failures, webhook-call errors in events, and `https://rancher.morpheus.net/ping`.
- **Ingress:** the controller DaemonSet.
- **HAProxy:** backends, from its stats page if configured, otherwise each controller's 9345.
- **Harbor:** `/api/v2.0/health`, plus image-pull failures in `kube-system` and `cattle-system`.

It never depends on the Service network it's diagnosing:
- it uses the host network on a controller;
- it reaches the API at `127.0.0.1:6443` and etcd at `127.0.0.1:2379`;
- it resolves names through the node's own DNS, not CoreDNS.

Every check has a timeout, and one broken check doesn't stop the others.

Two ways to run it:

- **In-cluster, continuously:** `kubectl apply -f deploy/70-health.yaml` (pinned to c01–c03). Metrics on `:9112` feed the `ptk.health` alerts; `/report` returns the full JSON.
- **Out of band, when the cluster is too broken to schedule pods:** the podman service and timer in [`deploy/health/`](deploy/health/) run the same checks on c01 every 5 minutes. They write `/var/lib/ptk/health.json` and put the summary in `journalctl -u ptk-health`. For an immediate check, run the `ExecStart` line by hand. The exit code is 0 for green, 1 for yellow and 2 for red.

Both modes use etcd's client certificate, which grants full etcd access. Keep `ptk-system` restricted to cluster admins.

Per-node Service routing comes from node-check. Each node-check pod probes `10.43.0.1:443` and `kubernetes.default.svc.cluster.local` from the pod network and reports NotReady when either fails. `ptk health` reads that readiness from the API, so it sees which nodes are affected without scraping across the broken dataplane.

Runbooks:
- [Cilium agents not Ready → Service routing → Rancher down](docs/runbooks/cilium-service-routing.md)
- [Removing the stale w08 etcd member](docs/runbooks/etcd-remove-stale-member.md)

### etcd membership and node roles

- **Before any control-plane maintenance**, run [`scripts/etcd-members.sh`](scripts/etcd-members.sh) on a controller. It's read-only. It lists every etcd member, flags any whose IP isn't one of the three controllers, and shows how many more failures etcd can survive.
- **Removing a stale member** (the current w08 exception): follow [`docs/runbooks/etcd-remove-stale-member.md`](docs/runbooks/etcd-remove-stale-member.md). Remove the membership first, verify it, and only then clean up the node's labels.
- node-check reports what each host actually runs (`rke2 server` or `rke2 agent`, and whether etcd is running). Two alerts build on that:
  - `PtkEtcdProcessCount` fires when the number of nodes running an etcd process isn't 3.
  - `PtkNodeRoleMismatch` fires when a node carries etcd or control-plane labels but runs `rke2-agent`. That describes w08 until the runbook is finished.
- The host view can't see an etcd member that exists but runs nowhere. That's exactly w08's case, which is why the script exists alongside the alerts.

### Cilium

- node-check (`"cni": "cilium"` in the policy) fails a node whose active CNI config in `/etc/cni/net.d` isn't Cilium's, and warns about leftover configs from another CNI. The usual culprit is Canal from a node that once started without `cni: none`. containerd uses the first file by name, so a leftover can take over after a reboot.
- **Istio CNI with Cilium:** Istio's CNI plugin chains itself into Cilium's config. Cilium must be installed with `cni.exclusive=false`, or it rewrites its config and drops Istio's plugin. node-check reports whether `istio-cni` is chained (the `plugins` label on `ptk_node_cni_config_info`).
- **Cilium's kube-proxy replacement with Istio:** set `socketLB.hostNamespaceOnly=true`. Without it, Cilium rewrites Service addresses at the socket, before the sidecar sees the connection. See Cilium's Istio integration docs for your version.
- **Cilium with Kata:** a Kata guest has its own kernel, so Cilium's socket-level load balancing never sees its connections. Service traffic from Kata pods depends on Cilium translating addresses per packet instead, and the setting above covers that too. The Kata smoke test in `deploy/kata/istio-smoke.yaml` calls a ClusterIP Service from a Kata pod, so it exercises this path. Run it after any Cilium upgrade.
- **Cilium host firewall:** if host policies are enabled, allow node traffic to the FlashArray data ports (iSCSI 3260, NVMe/TCP 4420) and the audit's HTTPS (443) to the array management address. Blocked storage traffic shows up in node-check as degraded or missing paths.
- If the cluster uses default-deny policies, [`deploy/60-cilium-policy.yaml`](deploy/60-cilium-policy.yaml) is a starting point:
  - it lets Rancher Monitoring scrape ptk;
  - it lets ptk-audit reach the API server, DNS and the array (replace the FlashArray address placeholder).

## Getting images into Harbor

The runtime uses only the Python standard library, so you don't need a PyPI mirror. The build needs only the UBI 9 minimal base image and the `python3` and `fio` RPMs.

`scripts/airgap-bundle.sh` carries ptk and every image in [`images.txt`](images.txt) (kata-deploy, Pure's exporter, Portworx) into Harbor:

```bash
# Connected side (needs podman or docker, plus skopeo)
scripts/airgap-bundle.sh build            # ptk -> dist/ptk-0.1.0.tar
scripts/airgap-bundle.sh save             # images.txt -> dist/mirror/ (all architectures, digests kept)

# Air-gapped side, with a Harbor robot account that can push
export HARBOR_USER='robot$ci-push' HARBOR_PASSWORD=...
scripts/airgap-bundle.sh push        valhalla.morpheus.net          # -> valhalla.morpheus.net/platform/ptk:0.1.0
scripts/airgap-bundle.sh push-mirror valhalla.morpheus.net          # quay.io/x/y -> valhalla.morpheus.net/quay/x/y
```

`push-mirror` puts each source registry in its own Harbor project: `dockerhub`, `quay`, `k8s` and `ghcr`. Create those projects (plus `platform`) first. [`deploy/rke2/registries.yaml`](deploy/rke2/registries.yaml) then rewrites pulls to them on every node. Upstream Helm charts and manifests, kata-deploy's included, pull through Harbor without any image overrides. Nodes authenticate with a pull-only robot account there, so pods need no `imagePullSecrets`.

Other Harbor settings worth turning on:

- **Signing.** Set `COSIGN_KEY=cosign.key` when pushing to sign every image. The script skips the Rekor transparency log, which you can't reach from inside an air gap. Once everything is signed, enable cosign enforcement on the Harbor projects so unsigned images can't be pulled.
- **Trivy scanning.** Harbor's Trivy can't download its vulnerability database from inside the gap. Either configure it for offline use and carry the database in with each image batch, or turn scanning off. Otherwise scans fail.
- **Harbor on the FlashArray.** If Harbor's own PVCs live on the FlashArray, `ptk audit` shows their usage like any other namespace. Expect about 1:1 data reduction on the registry volume, because image layers are already compressed.

## Preflight one node before you install anything

Run this on a RHEL node with podman, before or after you install the CSI driver:

```bash
podman run --rm --pid=host --user 0 --security-opt label=disable \
  -v /etc:/host/etc:ro -v /sys:/host/sys:ro -v /opt:/host/opt:ro \
  -v /var/lib/rancher/rke2/agent/etc/containerd:/host/var/lib/rancher/rke2/agent/etc/containerd:ro \
  -e PTK_PROTOCOL=iscsi \
  valhalla.morpheus.net/platform/ptk:0.1.0 node-check --once
```

Notes on the flags:

- `label=disable` lets the container read host files under SELinux enforcing. Never use `:z` on these mounts: it would relabel the host's `/etc`.
- Drop the containerd line if RKE2 isn't installed yet.

It prints a JSON report. The exit code is 0 for ok or warnings (1 for warnings if you add `--strict`) and 2 if any check failed.

## Deploy

1. Install [`deploy/rke2/registries.yaml`](deploy/rke2/registries.yaml) on each node, or check that the image path in `deploy/kustomization.yaml` matches your Harbor.
2. Set `protocol` in the `ptk-policy` ConfigMap in `deploy/20-node-check.yaml`.
3. Fill in `deploy/30-audit.yaml` with your array endpoints and StorageClasses.

```bash
# Read-only array user for the audit (on the FlashArray CLI):
#   pureadmin create --role readonly ptk-audit ; pureadmin create --api-token ptk-audit
kubectl create namespace ptk-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ptk-system create secret generic ptk-array-tokens \
  --from-literal=fa01-token=<api-token> --from-file=ca.pem=./array-ca.pem

kubectl apply -k deploy/                  # namespace, RBAC, DaemonSet, audit, PodMonitor + alerts
kubectl apply -f deploy/bench-job.yaml    # on demand, per StorageClass
```

`40-monitoring.yaml` expects Rancher Monitoring (Prometheus Operator CRDs). If you don't run it, remove that file from the kustomization.

### Istio

`ptk-system` is labelled `istio-injection: disabled`:
- The DaemonSet uses `hostPID`, and a sidecar on each of 16 nodes buys nothing.
- A sidecar would keep a bench Job pod running after fio exits.
- Nothing in the namespace serves mesh traffic.

If mesh policy requires sidecars, apply `deploy/50-istio-optional.yaml`. It adds two things:
- A `ServiceEntry` so the audit can reach the array under `REGISTRY_ONLY` egress.
- A `PeerAuthentication` that leaves the metrics port `PERMISSIVE`, so Rancher Monitoring can still scrape it under STRICT mTLS.

For Istio with Kata pods, see [Kata Containers](#kata-containers) below.

### SELinux

RKE2 on RHEL 9 usually runs with SELinux enforcing. node-check only reads from the host. If you see AVC denials for the `ptk-node-check` pods (`ausearch -m avc -ts recent`), add `seLinuxOptions: {type: spc_t}` to the container's `securityContext`.

node-check runs as uid 0 with all capabilities dropped and read-only mounts. It needs uid 0 to read root-only files such as RKE2's containerd `config.toml`, which is where it looks for Kata runtime handlers.

## Kata Containers

Each Kata pod runs in its own lightweight VM. That gives stronger isolation for untrusted or multi-tenant workloads, at some cost in startup time, memory and I/O. On this cluster, Kata VMs run nested inside the RHEL VMs, so work through these steps in order.

**1. Enable nested virtualization on the workers that will run Kata.**
You don't need to use all 13; a labelled pool of 3 or more is enough. For each VM:
1. Drain the node and shut the VM down.
2. In vCenter, open Edit Settings > CPU and enable "Expose hardware assisted virtualization to the guest OS".
3. Power the VM on and uncordon the node.

Then run a preflight check on it: `ptk node-check --once` should report `kata.cpu_virtualization` and `kata.kvm` as ok.

**2. Install Kata with kata-deploy.**
1. Add `quay.io/kata-containers/kata-deploy:<version>` to `images.txt` and mirror it. With the Harbor registry mapping above, the Helm chart pulls it unchanged.
2. Run `helm show values` for that chart version and set:
   - the Kubernetes distribution to `rke2`, so it edits RKE2's containerd config template instead of `/etc/containerd`;
   - a `nodeSelector` for your Kata pool;
   - the shims to only the ones you need (for example `qemu`).
3. Expect kata-deploy to restart the RKE2 service on each node it installs to. Roll it out gradually.

The `PtkKataNodeNotCapable` and `PtkKataCoverageLow` alerts catch a node that got Kata without nested virtualization, and a pool too small to drain a node from.

**3. Pods that must stay on runc.**
Anything that uses `hostPID`, `hostNetwork` or host devices cannot run inside a Kata VM:
- ptk node-check;
- the Portworx/CSI node plugins;
- the Istio CNI node agent;
- Rancher's own agents.

Kata is opt-in per pod through `runtimeClassName`. Set it on workloads only, never cluster-wide.

**4. Storage under Kata.**
Multipath stays on the host. The Kata guest sees one device, and node-check keeps monitoring its paths.

How a PVC gets into the guest depends on its volume mode:
- **Filesystem PVCs** are shared into the guest through virtio-fs (a shared-filesystem layer).
- **Block PVCs** (`volumeMode: Block`) are handed to the guest as a block device, so they usually lose less performance.

Measure the difference on your hardware rather than guessing:

```bash
scripts/bench-matrix.sh -s px-fada-block -r runc,kata-qemu -m Filesystem,Block -n worker-07
# storageclass           runtime    profile        iops          MiB/s          p99 ms
# px-fada-block          runc       randread-4k    ...
# px-fada-block          kata-qemu  randread-4k    ... (-NN%)    ... (-NN%)     ... (+NN%)
# px-fada-block [Block]  runc       ...
```

The script runs one Job at a time, pinned to one node, so runs don't compete for the array, and shows each Kata result as a change from runc. If fio fails under Kata with "O_DIRECT rejected", add `-D` for buffered I/O on the Filesystem runs.

**5. Istio under Kata.**
The sidecar and its traffic redirection have to work inside the Kata guest, and whether they do depends on how Istio is installed (the `istio-init` container or the Istio CNI plugin). [`deploy/kata/istio-smoke.yaml`](deploy/kata/istio-smoke.yaml) checks this under STRICT mTLS. It includes a control client that must fail; if the control passes, STRICT isn't being enforced and the other results can't be trusted. Run it before you put meshed workloads on Kata.

## Useful queries (Grafana / Prometheus)

```promql
# Paths per PVC, across all nodes
ptk_multipath_paths_running * on (wwid) group_left (namespace, pvc) ptk_pvc_volume_info

# Physical FlashArray capacity consumed per namespace (after data reduction)
sum by (namespace) (ptk_pvc_physical_bytes)

# Namespaces whose data reduces worst (good candidates for app-level compression review)
bottomk(10, avg by (namespace) (ptk_pvc_data_reduction_ratio))

# Nodes not at Pure recommended settings, by check
ptk_node_check_status > 0

# Nodes that can run Kata VMs right now
ptk_node_kata_capable == 1 and on (node) ptk_node_kata_runtime_info
```

## Endpoints

node-check listens on `:9110` and audit on `:9111`. Both serve:
- `/metrics`: Prometheus metrics
- `/report`: the latest full JSON report
- `/healthz`: health check

## Tuning the policy

The expected multipath values in [`ptk/policy.py`](ptk/policy.py) follow Pure's Portworx FADA guidance for RHEL. Pure revises these between releases, so check them against the docs for the Portworx version you deploy.

Override any value in the `ptk-policy` ConfigMap. Keys you leave out keep their defaults. `kata` takes one of three values:

- `auto` (the default): Kata problems fail only on nodes whose containerd has a Kata handler. Elsewhere they're reported as information.
- `required`: every node is expected to run Kata.
- `off`: skip the Kata checks.

For example:

```json
{"protocol": "nvme-tcp", "min_paths": 4, "kata": "auto", "pure_nvme_device": {"dev_loss_tmo": "60"}}
```

## How the audit maps PVs to array volumes

CSI drivers put the PV name (`pvc-<uid>`) in the array volume name. Portworx FADA uses `px_<cluster-id>-pvc-<uid>`, and PSO used `<prefix>-pvc-<uid>`. ptk matches on that UID:

- A PV with no matching array volume is reported as **missing**.
- A PV whose volume is in the array's destroyed bucket is reported as **destroyed**. You can still recover it until the array eradicates it.
- A `pvc-<uid>` array volume with no PV is reported as an **orphan**.

Array volumes without a `pvc-<uid>` name, such as vSphere datastores, are ignored. If any array is unreachable, the audit doesn't report missing or orphan volumes for that cycle rather than raise false alarms.

With Portworx, set `PTK_STORAGECLASSES` to your FADA classes only. PX-native volumes live inside Portworx storage pools, not as one array volume per PV.

## Development

```bash
python3 -m unittest discover -s tests -t .      # Python 3.9+ (RHEL 9's python3)
scripts/test-rules.sh                            # promtool lint + unit tests for the alerts
python3 -m ptk node-check --once --host-root /   # on a real RHEL host, as root
```
