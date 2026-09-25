#!/usr/bin/env bash
# Read-only etcd membership check for RKE2. Run as root on a control-plane node
# that is currently running etcd (for example c01).
#
#   scripts/etcd-members.sh
#   EXPECTED_ETCD_IPS=10.5.5.140,10.5.5.141,10.5.5.142 scripts/etcd-members.sh
#
# It runs only `member list` and `endpoint health`. It never changes membership;
# for members that should not be there it prints the removal command for you
# to review and run (see docs/runbooks/etcd-remove-stale-member.md).
#
# Exit codes: 0 healthy and as expected, 1 unexpected members or no failure
# tolerance left, 2 could not query etcd.
set -euo pipefail

EXPECTED="${EXPECTED_ETCD_IPS:-10.5.5.140,10.5.5.141,10.5.5.142}"
RKE2_DIR="${RKE2_DIR:-/var/lib/rancher/rke2}"
CRICTL="${CRICTL:-$RKE2_DIR/bin/crictl}"
export CRI_CONFIG_FILE="${CRI_CONFIG_FILE:-$RKE2_DIR/agent/etc/crictl.yaml}"
TLS="$RKE2_DIR/server/tls/etcd"

cid="$("$CRICTL" ps --label io.kubernetes.container.name=etcd --quiet 2>/dev/null | head -n1 || true)"
if [[ -z "$cid" ]]; then
  echo "No running etcd container on $(hostname). Run this on a healthy control-plane node." >&2
  exit 2
fi

# etcdctl ships inside RKE2's etcd image; the TLS paths are the same inside the container.
etcdctl() {
  "$CRICTL" exec "$cid" etcdctl \
    --endpoints=https://127.0.0.1:2379 \
    --cacert="$TLS/server-ca.crt" --cert="$TLS/server-client.crt" --key="$TLS/server-client.key" \
    "$@"
}

members="$(etcdctl member list -w json)" || { echo "etcdctl member list failed" >&2; exit 2; }
# endpoint health exits non-zero when any member is unhealthy; that is data, not an error.
health="$(etcdctl endpoint health --cluster -w json 2>/dev/null || true)"

REMOVE_CMD="CRI_CONFIG_FILE=$CRI_CONFIG_FILE $CRICTL exec $cid etcdctl --endpoints=https://127.0.0.1:2379 --cacert=$TLS/server-ca.crt --cert=$TLS/server-client.crt --key=$TLS/server-client.key" \
MEMBERS_JSON="$members" HEALTH_JSON="$health" EXPECTED_IPS="$EXPECTED" python3 - <<'PY'
import json, os, sys
from urllib.parse import urlparse

members = json.loads(os.environ["MEMBERS_JSON"]).get("members", [])
try:
    health = json.loads(os.environ["HEALTH_JSON"] or "[]")
except ValueError:
    health = []
expected = {ip.strip() for ip in os.environ["EXPECTED_IPS"].split(",") if ip.strip()}

def host(url):
    return urlparse(url).hostname or ""

healthy_ips = {host(h.get("endpoint", "")) for h in health if h.get("health")}

rows, unexpected, missing = [], [], set(expected)
for m in members:
    mid = format(int(m["ID"]), "x")
    peer_ips = sorted({host(u) for u in m.get("peerURLs", [])})
    client_ips = sorted({host(u) for u in m.get("clientURLs", [])})
    ips = set(peer_ips) | set(client_ips)
    is_expected = bool(ips & expected)
    missing -= ips
    ok = bool(ips & healthy_ips)
    learner = m.get("isLearner", False)
    name = m.get("name") or "(never started)"
    rows.append((mid, name, ",".join(peer_ips) or "-", "yes" if ok else "NO",
                 "yes" if is_expected else "NO", "learner" if learner else "voter"))
    if not is_expected:
        unexpected.append((mid, name, peer_ips))

hdr = ("MEMBER ID", "NAME", "PEER IP", "HEALTHY", "EXPECTED", "ROLE")
widths = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
for r in [hdr] + rows:
    print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))

voters = [r for r in rows if r[5] == "voter"]
n = len(voters)
quorum = n // 2 + 1
up = sum(1 for r in voters if r[3] == "yes")
tolerance = up - quorum
print()
print(f"voting members: {n}   quorum: {quorum}   healthy voters: {up}   "
      f"further failures tolerated: {tolerance if tolerance >= 0 else 'NONE (quorum already lost)'}")

problems = False
if tolerance < 1:
    problems = True
    print("\n!! etcd cannot survive losing one more member. Do not reboot, patch or restart any "
          "control-plane node until membership is fixed.")
if missing:
    problems = True
    print(f"\n!! expected control-plane IPs with no etcd member: {', '.join(sorted(missing))}")
for mid, name, ips in unexpected:
    problems = True
    print(f"\n!! unexpected member {mid} ({name}, peer {', '.join(ips) or 'unknown'}).")
    print("   Before removing it, confirm on that host that rke2-server is inactive and nothing")
    print("   listens on :2379/:2380, and take a snapshot. Then, from this node:")
    print(f"     {os.environ['REMOVE_CMD']} member remove {mid}")
    print("   Full procedure: docs/runbooks/etcd-remove-stale-member.md")
if up < quorum and unexpected:
    print("\n!! Healthy voters are below quorum, so member remove will fail. Recover quorum first.")
sys.exit(1 if problems else 0)
PY
