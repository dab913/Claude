# Remove a stale etcd member (w08)

**Cluster:** RKE2 v1.33.3+rke2r1, embedded etcd.
**Intended etcd members:** `c01` (`10.5.5.140`), `c02` (`10.5.5.141`), `c03` (`10.5.5.142`).
**Stale member:** `911dd1653e70620`, which belongs to `w08.morpheus.net` (`10.5.5.158`). That host now runs `rke2-agent` only.

Order matters. Remove the etcd membership first. Clean up w08's server-role labels only after the removal has been verified. The labels are the last trace of the problem, not its cause.

## Why this is urgent

With 4 voting members, quorum is 3. w08 runs no etcd, so only 3 members vote, which is exactly quorum. **The cluster tolerates zero further etcd failures right now.**

If c01, c02 or c03 reboots, gets patched or restarts `rke2-server`, etcd loses quorum and the Kubernetes API stops accepting writes. After the removal there are 3 members, quorum is 2, and one controller can fail safely.

Until step 5 is done, pause every change to the control plane: OS patching, `rke2-server` restarts, and vCenter maintenance on c01–c03.

## 1. Snapshot first (on c01)

```bash
rke2 etcd-snapshot save --name pre-remove-w08
ls -l /var/lib/rancher/rke2/server/db/snapshots/ | tail -3
```

Copy the new snapshot **and** `/var/lib/rancher/rke2/server/token` off the node. A restore needs both.

## 2. Confirm the membership (on c01)

```bash
scripts/etcd-members.sh
```

Expected output (IDs other than w08's will differ):

```
MEMBER ID         NAME                   PEER IP     HEALTHY  EXPECTED  ROLE
...               c01...                 10.5.5.140  yes      yes       voter
...               c02...                 10.5.5.141  yes      yes       voter
...               c03...                 10.5.5.142  yes      yes       voter
911dd1653e70620   w08...                 10.5.5.158  NO       NO        voter

voting members: 4   quorum: 3   healthy voters: 3   further failures tolerated: 0
```

**Stop here** unless all of these are true:
- Exactly one unexpected member.
- Its ID is `911dd1653e70620` **and** its peer IP is `10.5.5.158`. Match both, not the ID alone.
- c01, c02 and c03 are all `HEALTHY yes`. Removing a member needs quorum: 3 of the current 4.

## 3. Confirm w08 is not running etcd (on w08)

```bash
systemctl is-active rke2-server        # expect: inactive (or "unknown" if not installed)
systemctl is-active rke2-agent         # expect: active
pgrep -a etcd                          # expect: no output
ss -ltnp | grep -E ':(2379|2380)\b'    # expect: no output
```

Then make sure w08 can never come back as a server with its old data:

```bash
systemctl disable rke2-server 2>/dev/null; systemctl mask rke2-server
```

Check `/etc/rancher/rke2/config.yaml` on w08 while you're there. It should point at `server: https://rke2-api.morpheus.net:9345` and contain agent settings only.

## 4. Remove the member (on c01)

Use the exact command that `scripts/etcd-members.sh` printed. It has the container ID and certificate paths filled in, and ends with:

```
... etcdctl ... member remove 911dd1653e70620
```

## 5. Verify (on c01)

```bash
scripts/etcd-members.sh                           # expect exit 0, "further failures tolerated: 1"
kubectl get --raw='/readyz?verbose' | grep etcd   # expect: [+]etcd ok
kubectl get nodes -o wide
```

Once the output shows 3 voters, all healthy and all expected, control-plane changes are safe again.

## 6. Clean up w08's stale server-role labels (only now)

Look at what is actually on the node object first:

```bash
NODE=w08.morpheus.net        # use the exact name from `kubectl get nodes`
kubectl get node "$NODE" --show-labels | tr ',' '\n' | grep -E 'node-role|etcd'
kubectl get node "$NODE" -o json | python3 -c \
  'import json,sys; [print(k) for k in json.load(sys.stdin)["metadata"]["annotations"] if "etcd" in k]'
```

Remove only the server-role entries you saw:

```bash
kubectl label node "$NODE" node-role.kubernetes.io/etcd- node-role.kubernetes.io/control-plane- node-role.kubernetes.io/master-
kubectl annotate node "$NODE" etcd.rke2.cattle.io/node-name- etcd.rke2.cattle.io/node-address-
```

Once ptk is deployed, the `PtkNodeRoleMismatch` alert should clear within a few minutes. It compares the node's role labels with what the host actually runs.

Don't delete and re-register the Node object as a shortcut for this cleanup. How RKE2's etcd controller reacts to a deleted node is outside what this runbook controls, and the explicit removal in step 4 already did the work.

## 7. Retire w08's stale etcd data (on w08)

If `/var/lib/rancher/rke2/server/` still exists on w08, it holds an old copy of etcd. That copy contains every Kubernetes Secret as of when w08 was a server. Move it out of RKE2's path now, and destroy it once you're confident you won't need it:

```bash
mv /var/lib/rancher/rke2/server /root/rke2-server-stale-$(date +%F)
chmod 700 /root/rke2-server-stale-*
```

## If something goes wrong

- **`member remove` fails with a quorum or timeout error.** A controller was unhealthy. Don't retry in a loop. Rerun step 2 and bring the unhealthy controller back first.
- **The API loses etcd quorum.** Restore from the step 1 snapshot using RKE2's documented cluster-reset restore (`rke2 server --cluster-reset --cluster-reset-restore-path=...` on one server, then rejoin the others). Use the token you saved.

## Related checks this cluster should keep passing

- `scripts/etcd-members.sh`: run it before any control-plane maintenance.
- `PtkEtcdMemberCount`: fires when the number of nodes running etcd isn't 3.
- `PtkNodeRoleMismatch`: fires when a node's role labels don't match what the host runs.
