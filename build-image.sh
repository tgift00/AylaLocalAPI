#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
IMAGE_NAME="aylalocalapi-ayla-bridge:latest"
FULL_IMAGE_NAME="docker.io/library/${IMAGE_NAME}"
OUTPUT_DIR="${SCRIPT_DIR}/image"
OUTPUT_TAR="${OUTPUT_DIR}/aylalocalapi-ayla-bridge.tar"

mkdir -p "${OUTPUT_DIR}"

# 1. Select builder (Podman on k3s nodes, Docker on Docker nodes)
if command -v podman >/dev/null 2>&1; then
  BUILDER="podman"
elif command -v docker >/dev/null 2>&1; then
  BUILDER="docker"
else
  echo "ERROR: Neither podman nor docker is installed." >&2
  exit 1
fi

echo "=== 1. Building ${FULL_IMAGE_NAME} with ${BUILDER} ==="
${BUILDER} build -t "${FULL_IMAGE_NAME}" "${SCRIPT_DIR}"

echo "=== 2. Saving archive to ${OUTPUT_TAR} ==="
rm -f "${OUTPUT_DIR}/ayla-bridge.tar" "${OUTPUT_TAR}"
${BUILDER} save "${FULL_IMAGE_NAME}" -o "${OUTPUT_TAR}"
chown 1000:1000 "${OUTPUT_TAR}"

# 3. Load directly into k3s containerd (if k3s is installed) and restart pod
if command -v k3s >/dev/null 2>&1; then
  echo "=== 3. Importing ${FULL_IMAGE_NAME} into k3s containerd ==="
  sudo k3s ctr images import "${OUTPUT_TAR}"

  if kubectl get deployment ayla-bridge -n smart-home >/dev/null 2>&1; then
    echo "=== 4. Restarting ayla-bridge deployment in k3s ==="
    kubectl rollout restart deployment/ayla-bridge -n smart-home
  fi
fi

echo "Done! Saved $(du -h "${OUTPUT_TAR}" | cut -f1) -> ${OUTPUT_TAR}"
