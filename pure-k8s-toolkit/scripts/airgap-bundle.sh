#!/usr/bin/env bash
# Move ptk and its companion images (kata-deploy, Portworx, exporters) into Harbor.
#
# Connected side:
#   scripts/airgap-bundle.sh build                 build ptk -> dist/ptk-<ver>.tar
#   scripts/airgap-bundle.sh save  [images.txt]    pull every listed image -> dist/mirror/
#   (carry dist/ across the gap)
# Air-gapped side:
#   scripts/airgap-bundle.sh push        <harbor-host>[/project]   ptk -> <harbor>/platform/ptk
#   scripts/airgap-bundle.sh push-mirror <harbor-host>             dist/mirror -> Harbor projects
#
# push-mirror keeps upstream paths under one Harbor project per source registry
# (quay.io/x/y -> <harbor>/quay/x/y), matching deploy/rke2/registries.yaml, so
# upstream manifests and Helm charts need no image overrides.
#
# Harbor auth: set HARBOR_USER / HARBOR_PASSWORD (a robot account with push on
# the target projects). Optional: COSIGN_KEY=cosign.key signs what you push, for
# Harbor projects that enforce cosign signatures.
set -euo pipefail

VERSION="${VERSION:-0.1.0}"
IMAGE="ptk:${VERSION}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${HERE}/dist"
ENGINE="${ENGINE:-$(command -v podman || command -v docker || true)}"

# Source registry -> Harbor project. Keep in sync with deploy/rke2/registries.yaml.
project_for() {
  case "$1" in
    docker.io)       echo dockerhub ;;
    quay.io)         echo quay ;;
    registry.k8s.io) echo k8s ;;
    ghcr.io)         echo ghcr ;;
    *)               echo "${1%%:*}" | tr '.' '-' ;;   # e.g. gcr.io -> gcr-io
  esac
}

# Split "quay.io/kata-containers/kata-deploy:3.x" into registry and path:tag,
# expanding Docker Hub short names ("nginx" -> docker.io/library/nginx).
split_ref() {
  local ref="$1" first="${1%%/*}"
  if [[ "$ref" != */* ]]; then echo "docker.io library/$ref"
  elif [[ "$first" == *.* || "$first" == *:* || "$first" == localhost ]]; then echo "$first ${ref#*/}"
  else echo "docker.io $ref"
  fi
}

# Sets CREDS to a skopeo credentials flag (--dest-creds / --creds) when HARBOR_USER is set.
# An array, so passwords with spaces or glob characters pass through intact.
creds() {
  CREDS=()
  if [[ -n "${HARBOR_USER:-}" ]]; then
    CREDS=("--${1:-dest-creds}=${HARBOR_USER}:${HARBOR_PASSWORD:?HARBOR_PASSWORD not set}")
  fi
}

sign() {
  [[ -z "${COSIGN_KEY:-}" ]] && return 0
  # No Rekor transparency log inside an air gap.
  local ref="$1" repo digest
  if [[ "$ref" == *@* ]]; then
    repo="${ref%@*}"; digest="${ref#*@}"
  else
    repo="${ref%:*}"
    creds creds
    digest="$(skopeo inspect "${CREDS[@]}" --format '{{.Digest}}' "docker://$ref")"
  fi
  cosign sign --yes --key "$COSIGN_KEY" --tlog-upload=false "${repo}@${digest}"
}

case "${1:-}" in
  build)
    mkdir -p "$DIST"
    "$ENGINE" build -f "$HERE/Containerfile" --build-arg VERSION="$VERSION" \
      ${BASE_IMAGE:+--build-arg BASE_IMAGE="$BASE_IMAGE"} -t "$IMAGE" "$HERE"
    "$ENGINE" run --rm "$IMAGE" --version
    "$ENGINE" save -o "$DIST/ptk-${VERSION}.tar" "$IMAGE"
    (cd "$DIST" && sha256sum "ptk-${VERSION}.tar" > "ptk-${VERSION}.tar.sha256")
    echo "Carry $DIST across the gap."
    ;;
  save)
    LIST="${2:-$HERE/images.txt}"
    mkdir -p "$DIST/mirror"
    : > "$DIST/mirror/index.txt"
    grep -vE '^\s*(#|$)' "$LIST" | while read -r ref; do
      dir="$(echo "$ref" | tr '/:@' '___')"
      echo "saving $ref"
      # dir: keeps manifests and blobs byte-for-byte, so pinned digests still match after the push.
      skopeo copy --all --preserve-digests --retry-times 3 "docker://$ref" "dir:$DIST/mirror/$dir"
      echo "$dir $ref" >> "$DIST/mirror/index.txt"
    done
    (cd "$DIST/mirror" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
    ;;
  push)
    TARGET="${2:?usage: $0 push <harbor-host>[/project]}"
    [[ "$TARGET" == */* ]] || TARGET="$TARGET/platform"
    (cd "$DIST" && sha256sum -c "ptk-${VERSION}.tar.sha256")
    creds
    skopeo copy "${CREDS[@]}" "docker-archive:$DIST/ptk-${VERSION}.tar" "docker://${TARGET}/ptk:${VERSION}"
    sign "${TARGET}/ptk:${VERSION}"
    echo "Pushed ${TARGET}/ptk:${VERSION}. Set it in deploy/kustomization.yaml (images.newName)."
    ;;
  push-mirror)
    HARBOR="${2:?usage: $0 push-mirror <harbor-host>}"
    (cd "$DIST/mirror" && sha256sum -c SHA256SUMS)
    while read -r dir ref; do
      read -r registry path <<<"$(split_ref "$ref")"
      dest="$HARBOR/$(project_for "$registry")/$path"
      echo "$ref -> $dest"
      creds
      skopeo copy --all --preserve-digests "${CREDS[@]}" "dir:$DIST/mirror/$dir" "docker://$dest"
      sign "$dest"
    done < "$DIST/mirror/index.txt"
    ;;
  *)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
