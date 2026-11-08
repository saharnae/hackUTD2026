#!/usr/bin/env bash
# Run ON the Brev GPU instance (after `brev shell <instance>`).
# Starts the Nemotron 3 Nano NIM on port 8000 with tool calling enabled.
# Usage:  export NGC_API_KEY=nvapi-...   &&   bash start_nim.sh
set -euo pipefail

IMG="${IMG:-nvcr.io/nim/nvidia/nemotron-3-nano:latest}"
: "${NGC_API_KEY:?Set NGC_API_KEY first (ngc.nvidia.com -> Setup -> API Key)}"

echo "$NGC_API_KEY" | docker login nvcr.io --username '$oauthtoken' --password-stdin
mkdir -p ~/.cache/nim

docker rm -f nemotron 2>/dev/null || true
docker run -d --name nemotron --restart unless-stopped \
  --runtime=nvidia --gpus all --shm-size=16GB \
  -e NGC_API_KEY \
  -e NIM_ENABLE_AUTO_TOOL_CHOICE=1 \
  -e NIM_MAX_MODEL_LEN=32768 \
  -v ~/.cache/nim:/opt/nim/.cache \
  -u "$(id -u)" \
  -p 8000:8000 \
  "$IMG"

echo "Pulling weights + starting (first run: ~5-15 min). Watching readiness..."
until curl -sf localhost:8000/v1/health/ready >/dev/null; do sleep 10; printf '.'; done
echo; echo "READY. Served models:"; curl -s localhost:8000/v1/models; echo
echo "Logs: docker logs -f nemotron"
