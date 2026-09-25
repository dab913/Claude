#!/usr/bin/env bash
# Benchmark every StorageClass x runtime x volume-mode combination, one Job at a
# time (parallel runs would compete for the same array and skew each other),
# then print a table with each runtime's change vs. runc.
#
#   scripts/bench-matrix.sh -s px-fada-block -r runc,kata-qemu -m Filesystem,Block -n worker-07
#
#   -s  StorageClasses (comma-separated)                        required
#   -r  runtimes: "runc" or RuntimeClass names                  default runc,kata-qemu
#   -m  volume modes: Filesystem, Block                         default Filesystem,Block
#   -n  node to pin every run to (strongly recommended)         default: scheduler picks
#   -t  seconds per fio profile                                 default 60
#   -p  fio profiles (comma-separated, see ptk/bench.py)        default all
#   -i  ptk image                                               default harbor.lab.local/platform/ptk:0.1.0
#   -D  buffered I/O for Filesystem runs (if virtio-fs under Kata rejects O_DIRECT)
#
# Needs kubectl and python3 on the machine you run it from. Results are kept
# in bench-results/<timestamp>/ (one log per run).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
NS=ptk-system
CLASSES="" RUNTIMES="runc,kata-qemu" MODES="Filesystem,Block" NODE="" SECS=60 PROFILES=""
IMAGE="harbor.lab.local/platform/ptk:0.1.0" DIRECT=1
while getopts "s:r:m:n:t:p:i:D" opt; do
  case "$opt" in
    s) CLASSES="$OPTARG" ;; r) RUNTIMES="$OPTARG" ;; m) MODES="$OPTARG" ;; n) NODE="$OPTARG" ;;
    t) SECS="$OPTARG" ;; p) PROFILES="$OPTARG" ;; i) IMAGE="$OPTARG" ;; D) DIRECT=0 ;;
    *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
  esac
done
[[ -n "$CLASSES" ]] || { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

nprof=6; [[ -n "$PROFILES" ]] && nprof=$(tr ',' '\n' <<<"$PROFILES" | wc -l)
TIMEOUT=$(( nprof * (SECS + 30) + 600 ))
OUT="bench-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

manifest() {  # name storageclass runtime mode
  local name="$1" sc="$2" rt="$3" mode="$4" rc="" node="" user=65532 vol env
  [[ "$rt" != runc ]] && rc="runtimeClassName: $rt"
  [[ -n "$NODE" ]] && node="nodeSelector: {kubernetes.io/hostname: $NODE}"
  if [[ "$mode" == Block ]]; then
    # Raw device: fsGroup does not apply, so run fio as root (no capabilities).
    user=0
    vol="volumeDevices: [{name: data, devicePath: /dev/ptk-bench}]"
    env="- {name: PTK_BENCH_DEVICE, value: /dev/ptk-bench}"
  else
    vol="volumeMounts: [{name: data, mountPath: /data}]"
    env="- {name: PTK_BENCH_DIR, value: /data}
            - {name: PTK_BENCH_DIRECT, value: \"$DIRECT\"}"
  fi
  cat <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $name, namespace: $NS, labels: {app.kubernetes.io/name: ptk-bench}}
spec:
  accessModes: [ReadWriteOnce]
  volumeMode: $mode
  storageClassName: $sc
  resources: {requests: {storage: 50Gi}}
---
apiVersion: batch/v1
kind: Job
metadata: {name: $name, namespace: $NS, labels: {app.kubernetes.io/name: ptk-bench}}
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations: {sidecar.istio.io/inject: "false"}
    spec:
      $rc
      $node
      restartPolicy: Never
      securityContext: {runAsUser: $user, fsGroup: 65532, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: bench
          image: $IMAGE
          args: ["bench"]
          env:
            $env
            - {name: PTK_BENCH_SIZE, value: 8g}
            - {name: PTK_BENCH_RUNTIME, value: "$SECS"}
            - {name: PTK_BENCH_PROFILES, value: "$PROFILES"}
            - {name: PTK_STORAGECLASS, value: $sc}
            - {name: PTK_RUNTIME_CLASS, value: $rt}
            - name: NODE_NAME
              valueFrom: {fieldRef: {fieldPath: spec.nodeName}}
          resources: {requests: {cpu: "2", memory: 1Gi}, limits: {memory: 2Gi}}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
          $vol
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: $name}
YAML
}

cleanup() { kubectl -n "$NS" delete job,pvc -l app.kubernetes.io/name=ptk-bench --ignore-not-found --wait=true >/dev/null; }
trap cleanup EXIT

i=0
for sc in ${CLASSES//,/ }; do
  for mode in ${MODES//,/ }; do
    for rt in ${RUNTIMES//,/ }; do
      i=$((i + 1))
      name="ptk-bench-$i"
      log="$OUT/$sc-$mode-$rt.log"
      echo "[$i] $sc / $mode / $rt"
      manifest "$name" "$sc" "$rt" "$mode" > "$OUT/$sc-$mode-$rt.yaml"
      kubectl apply -f "$OUT/$sc-$mode-$rt.yaml" >/dev/null
      if kubectl -n "$NS" wait --for=condition=complete "job/$name" --timeout="${TIMEOUT}s" >/dev/null 2>&1; then
        kubectl -n "$NS" logs "job/$name" > "$log"
      else
        kubectl -n "$NS" logs "job/$name" > "$log" 2>&1 || true
        echo "    failed or timed out; see $log" >&2
        kubectl -n "$NS" describe "job/$name" >> "$log" 2>&1 || true
      fi
      cleanup
    done
  done
done

echo
PYTHONPATH="$HERE" python3 -m ptk bench-report "$OUT"/*.log | tee "$OUT/report.txt"
