#!/usr/bin/env bash
set -euo pipefail

############################################
# Config (adapt if your paths differ)
############################################
XAPP_NAME="oranslice-xapp"
XAPP_NAMESPACE="ricxapp"

REGISTRY_CONTAINER_NAME="registry"
REGISTRY_ADDR="myregistry.local:5000"

LOCAL_IMAGE="${XAPP_NAME}:latest"
REMOTE_IMAGE="${REGISTRY_ADDR}/${XAPP_NAME}:latest"

XAPP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE_PATH="${XAPP_ROOT}/Dockerfile"

ONBOARDER_DIR="$(cd /root && find . -type d -path "*/ric-plt-appmgr/xapp_orchestrater/dev/xapp_onboarder" 2>/dev/null | head -n 1)"
ONBOARDER_DIR="/root/${ONBOARDER_DIR#./}"
CONFIG_FILE="${XAPP_ROOT}/xapp_bs_connector/init/config-file.json"
SCHEMA_FILE="${XAPP_ROOT}/xapp_bs_connector/init/schema.json"

CHARTS_DIR="${ONBOARDER_DIR}/charts"
CHART_PACKAGE="oranslice-xapp-1.0.0.tgz"
CHARTMUSEUM_CONTAINER_NAME="chartmuseum"
CHART_REPO_URL="http://0.0.0.0:8090"

############################################
# Helpers
############################################
log() {
  echo
  echo "===== $* ====="
}

############################################
# 1. Uninstall Helm release and clean pods
############################################
log "Uninstalling existing Helm release (if any)"

if helm status "${XAPP_NAME}" -n "${XAPP_NAMESPACE}" >/dev/null 2>&1; then
  helm uninstall "${XAPP_NAME}" -n "${XAPP_NAMESPACE}"
else
  echo "Helm release ${XAPP_NAME} not found in namespace ${XAPP_NAMESPACE}, skipping uninstall."
fi

log "Cleaning leftover pods (if any)"

kubectl delete pods -n "${XAPP_NAMESPACE}" -l "app=${XAPP_NAME}" --ignore-not-found=true || true
PODS=$(kubectl get pods -n "${XAPP_NAMESPACE}" --no-headers 2>/dev/null | awk "/${XAPP_NAME}/ {print \$1}" || true)
if [ -n "${PODS}" ]; then
  echo "${PODS}" | xargs -r kubectl delete pod -n "${XAPP_NAMESPACE}" --force --grace-period=0 || true
fi

############################################
# 2. Stop and remove Docker containers using oranslice-xapp
############################################
log "Stopping and removing Docker containers using image ${XAPP_NAME}"

CNT_IDS_1=$(docker ps -aq --filter "ancestor=${LOCAL_IMAGE}" || true)
CNT_IDS_2=$(docker ps -aq --filter "ancestor=${XAPP_NAME}" || true)

CNT_IDS=$(printf "%s\n%s\n" "${CNT_IDS_1:-}" "${CNT_IDS_2:-}" | sort -u | sed '/^$/d' || true)

if [ -n "${CNT_IDS}" ]; then
  echo "Containers using ${XAPP_NAME}:"
  echo "${CNT_IDS}"
  echo "${CNT_IDS}" | xargs -r docker stop
  echo "${CNT_IDS}" | xargs -r docker rm
else
  echo "No containers using ${XAPP_NAME} found."
fi

############################################
# 3. Remove local Docker images for oranslice-xapp
############################################
log "Removing local Docker images for ${XAPP_NAME}"

IMAGE_IDS=$(docker images | awk '/oranslice-xapp/ {print $3}' | sort -u || true)
if [ -n "${IMAGE_IDS}" ]; then
  echo "Images to remove:"
  echo "${IMAGE_IDS}"
  echo "${IMAGE_IDS}" | xargs -r docker rmi -f
else
  echo "No local images matching oranslice-xapp found."
fi

############################################
# 4. Ensure local registry is running
############################################
log "Ensuring local Docker registry is running"

if ! docker ps --format '{{.Names}}' | grep -q "^${REGISTRY_CONTAINER_NAME}\$"; then
  if docker ps -a --format '{{.Names}}' | grep -q "^${REGISTRY_CONTAINER_NAME}\$"; then
    docker rm -f "${REGISTRY_CONTAINER_NAME}"
  fi

  docker run -d -p 5000:5000 --name "${REGISTRY_CONTAINER_NAME}" registry:2
else
  echo "Registry container '${REGISTRY_CONTAINER_NAME}' is already running."
fi

############################################
# 5. Rebuild Docker image from scratch
############################################
log "Rebuilding Docker image from scratch"

cd "${XAPP_ROOT}"
docker build -t "${LOCAL_IMAGE}" -f "${DOCKERFILE_PATH}" .

############################################
# 6. Tag and push to local registry
############################################
log "Tagging and pushing image to ${REGISTRY_ADDR}"

docker tag "${LOCAL_IMAGE}" "${REMOTE_IMAGE}"
docker push "${REMOTE_IMAGE}"

############################################
# 7. Ensure ChartMuseum is running
############################################
log "Ensuring ChartMuseum is running"

if ! docker ps --format '{{.Names}}' | grep -q "^${CHARTMUSEUM_CONTAINER_NAME}\$"; then
  if docker ps -a --format '{{.Names}}' | grep -q "^${CHARTMUSEUM_CONTAINER_NAME}\$"; then
    docker rm -f "${CHARTMUSEUM_CONTAINER_NAME}"
  fi

  docker run --rm -u 0 -it -d \
    -p 8090:8080 \
    --name "${CHARTMUSEUM_CONTAINER_NAME}" \
    -e DEBUG=1 \
    -e STORAGE=local \
    -e STORAGE_LOCAL_ROOTDIR=/charts \
    -v "${CHARTS_DIR}:/charts" \
    chartmuseum/chartmuseum:latest
else
  echo "ChartMuseum container '${CHARTMUSEUM_CONTAINER_NAME}' is already running."
fi

export CHART_REPO_URL="${CHART_REPO_URL}"

############################################
# 8. Re-onboard xApp with dms_cli
############################################
log "Re-onboarding xApp with dms_cli"

cd "${ONBOARDER_DIR}"

if [ ! -x ".venv/bin/dms_cli" ]; then
  echo "ERROR: .venv/bin/dms_cli not found or not executable in ${ONBOARDER_DIR}"
  exit 1
fi

.venv/bin/dms_cli onboard \
  "${CONFIG_FILE}" \
  --shcema_file_path="${SCHEMA_FILE}"

############################################
# 9. Reinstall Helm chart
############################################
log "Installing Helm chart for ${XAPP_NAME}"

cd "${CHARTS_DIR}"
helm install "${XAPP_NAME}" "${CHART_PACKAGE}" -n "${XAPP_NAMESPACE}"

log "Deployment triggered. Current pods in namespace ${XAPP_NAMESPACE}:"
kubectl get pods -n "${XAPP_NAMESPACE}" | grep "${XAPP_NAME}" || true

echo
echo "✅ Rebuild + push + redeploy of ${XAPP_NAME} completed."
