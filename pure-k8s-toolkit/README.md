# ptk: Pure Storage toolkit for RKE2

One small container image with three jobs, built for an air-gapped RKE2 cluster on RHEL 9 VMs in vCenter that uses a Pure FlashArray for persistent storage:

| Mode | Runs as | What it answers |
|---|---|---|
| `ptk node-check` | DaemonSet on all 16 nodes | Is this node's multipath / iSCSI / NVMe-TCP stack set up the way Pure expects? Are all paths to every attached volume up? |
| `ptk audit` | Deployment (1 replica) | Which namespace/PVC owns each array volume, and how well does it reduce? Which array volumes have no PV any more (orphans)? Which PVs point at a missing or destroyed volume? |
| `ptk bench` | Job | What IOPS, throughput and p99 latency does each StorageClass actually deliver? |

It fills gaps the stack you already have doesn't cover. Rancher sees PVCs but not the array. The FlashArray UI sees volumes but not namespaces. Neither checks the node settings in between, and those settings are where most FlashArray-on-Linux problems come from.

It does not replace:
- **The CSI driver.** Use Portworx with FlashArray Direct Access (FADA). Pure has retired Pure Service Orchestrator (PSO) in favor of Portworx.
- **Pure's array exporter** ([pure-fa-openmetrics-exporter](https://github.com/PureStorage-OpenConnect/pure-fa-openmetrics-exporter)) for array-wide capacity and performance metrics. Run it alongside ptk; ptk only adds the Kubernetes-side view.

## Why these checks matter on vSphere + RHEL 9

- **Cloned VMs share an iSCSI IQN.** If the 13 workers came from one vCenter template, they may all have the same `/etc/iscsi/initiatorname.iscsi`. The array then sees one host, and a volume can end up attached to two nodes at once, which corrupts data. Each node exposes its IQN as a metric, and the `PtkDuplicateIscsiInitiator` alert fires when two nodes share one. The same check covers NVMe host NQNs and `/etc/machine-id`.
- **The default `mpathconf --enable` config is wrong for Pure + Portworx.** It sets `user_friendly_names yes`, has no Pure device stanza, and doesn't blacklist VMware virtual disks. That last gap means multipathd can claim the VM's own OS disk.
- **Degraded paths are silent.** A volume on one of two paths keeps working until the second path fails. `ptk_multipath_paths_running` is joined to the PVC through the volume's WWID, so the alert names the affected namespace/PVC, not just `dm-7`.
- **Block-device scheduler.** The FlashArray schedules I/O itself, so its paths should use the `none` scheduler.

## Build and move it into the air gap

The runtime uses only the Python standard library, so you don't need a PyPI mirror. The build needs only the UBI 9 minimal base image and the `python3` and `fio` RPMs.

```bash
# Connected build host (podman on a subscribed RHEL host gets full RHEL repos automatically)
scripts/airgap-bundle.sh build            # -> dist/ptk-0.1.0.tar + .sha256

# Inside the air gap, to the registry your RKE2 registries.yaml points at
scripts/airgap-bundle.sh push harbor.lab.local/platform
```

## Preflight one node before you install anything

Run this on a RHEL node with podman, before or after you install the CSI driver:

```bash
podman run --rm --pid=host \
  -v /etc:/host/etc:ro -v /sys:/host/sys:ro \
  -e PTK_PROTOCOL=iscsi \
  harbor.lab.local/platform/ptk:0.1.0 node-check --once
```

It prints a JSON report. The exit code is 0 for ok or warnings (1 for warnings if you add `--strict`) and 2 if any check failed.

## Deploy

1. Set the image in `deploy/kustomization.yaml`.
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

### SELinux

RKE2 on RHEL 9 usually runs with SELinux enforcing. node-check only reads `/etc` and `/sys`. If you see AVC denials for the `ptk-node-check` pods (`ausearch -m avc -ts recent`), add `seLinuxOptions: {type: spc_t}` to the container's `securityContext`.

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
```

## Endpoints

node-check listens on `:9110` and audit on `:9111`. Both serve:
- `/metrics`: Prometheus metrics
- `/report`: the latest full JSON report
- `/healthz`: health check

## Tuning the policy

The expected multipath values in [`ptk/policy.py`](ptk/policy.py) follow Pure's Portworx FADA guidance for RHEL. Pure revises these between releases, so check them against the docs for the Portworx version you deploy.

Override any value in the `ptk-policy` ConfigMap. Keys you leave out keep their defaults:

```json
{"protocol": "nvme-tcp", "min_paths": 4, "pure_nvme_device": {"dev_loss_tmo": "60"}}
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
python3 -m unittest discover -s tests -t .   # Python 3.9+ (RHEL 9's python3)
python3 -m ptk node-check --once --host-root /  # on a real RHEL host, as root
```
