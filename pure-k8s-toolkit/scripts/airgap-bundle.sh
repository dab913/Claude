#!/usr/bin/env bash
# Move the ptk image across the air gap.
#
# Connected side:   scripts/airgap-bundle.sh build            -> dist/ptk-<ver>.tar + .sha256
# Air-gapped side:  scripts/airgap-bundle.sh push <registry>  e.g. harbor.lab.local/platform
#
# RKE2 nodes pull from <registry> through /etc/rancher/rke2/registries.yaml,
# the same way they pull the RKE2, Rancher and Istio images.
set -euo pipefail

VERSION="${VERSION:-0.1.0}"
IMAGE="ptk:${VERSION}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${HERE}/dist"
ENGINE="${ENGINE:-$(command -v podman || command -v docker)}"

case "${1:-}" in
  build)
    mkdir -p "$DIST"
    "$ENGINE" build -f "$HERE/Containerfile" --build-arg VERSION="$VERSION" \
      ${BASE_IMAGE:+--build-arg BASE_IMAGE="$BASE_IMAGE"} -t "$IMAGE" "$HERE"
    "$ENGINE" run --rm "$IMAGE" --version
    "$ENGINE" save -o "$DIST/ptk-${VERSION}.tar" "$IMAGE"
    (cd "$DIST" && sha256sum "ptk-${VERSION}.tar" > "ptk-${VERSION}.tar.sha256")
    echo "Carry $DIST/ptk-${VERSION}.tar and its .sha256 across the gap."
    ;;
  push)
    REGISTRY="${2:?usage: $0 push <registry/project>}"
    (cd "$DIST" && sha256sum -c "ptk-${VERSION}.tar.sha256")
    if command -v skopeo >/dev/null; then
      skopeo copy "docker-archive:$DIST/ptk-${VERSION}.tar" "docker://${REGISTRY}/ptk:${VERSION}"
    else
      "$ENGINE" load -i "$DIST/ptk-${VERSION}.tar"
      "$ENGINE" tag "$IMAGE" "${REGISTRY}/ptk:${VERSION}"
      "$ENGINE" push "${REGISTRY}/ptk:${VERSION}"
    fi
    echo "Pushed ${REGISTRY}/ptk:${VERSION}. Set it in deploy/kustomization.yaml (images.newName)."
    ;;
  *)
    sed -n '2,8p' "$0"; exit 1 ;;
esac
