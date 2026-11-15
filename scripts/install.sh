#!/usr/bin/env bash
# One-click installer AND runner for Critical Thinking Enhancer (Linux / macOS).
# Idempotent: re-running skips what is installed and just starts the app.
#
#   ./scripts/install.sh            install everything missing, then run
#   ./scripts/install.sh --yes --no-run
#
# Options: --yes  --no-run  --skip-models  --vision  --no-vision  --no-firewall
# Models only ever run on the GPU (vLLM: NVIDIA CUDA or AMD ROCm); without a usable GPU the coder is not started.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ENV_FILE="$ROOT/.env"
YES=0; NORUN=0; SKIP_MODELS=0; VISION=auto; NO_FIREWALL=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --no-run) NORUN=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --vision) VISION=on ;;
    --no-vision) VISION=off ;;
    --no-firewall) NO_FIREWALL=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m[ok]\033[0m %s\n' "$1"; }
warn() { printf '    \033[33m[!]\033[0m  %s\n' "$1"; }
info() { printf '         %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
ask()  { [ "$YES" = 1 ] && return 0; local a=""; read -r -p "    $1 [Y/n] " a || true; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }
secret() { ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-40}" ); }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"
OS="$(uname -s)"

pkg_install() {
  if [ "$OS" = Darwin ]; then
    have brew || { warn "Homebrew is required on macOS: https://brew.sh"; return 1; }
    brew install "$@"
  elif have apt-get; then $SUDO apt-get update -y && $SUDO apt-get install -y "$@"
  elif have dnf; then $SUDO dnf install -y "$@"
  elif have pacman; then $SUDO pacman -Sy --noconfirm "$@"
  elif have zypper; then $SUDO zypper install -y "$@"
  else warn "no supported package manager found - please install: $*"; return 1
  fi
}

set_env() {
  local key="$1" val="$2"
  if [ -f "$ENV_FILE" ] && grep -qE "^${key}=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}
env_has() { [ -f "$ENV_FILE" ] && grep -qE "^$1=" "$ENV_FILE"; }

echo
printf '  \033[35mCritical Thinking Enhancer - one-click installer & runner\033[0m\n'
echo "  Student-grade project. Not a medical tool, not an IQ test. MIT licensed, no warranty, no liability."
echo "  Everything runs and is stored locally on this machine."

# ---------------------------------------------------------------- 1. prerequisites
step "Checking prerequisites"
have curl || pkg_install curl
have git || pkg_install git || true
if have tesseract; then ok "Tesseract OCR (layer 1)"; else
  if ask "Install Tesseract OCR (first OCR layer)?"; then
    if [ "$OS" = Darwin ]; then pkg_install tesseract
    elif have apt-get; then pkg_install tesseract-ocr
    elif have pacman; then pkg_install tesseract tesseract-data-eng
    else pkg_install tesseract; fi || warn "Tesseract install failed - RapidOCR still works"
  fi
fi
if ! have uv; then
  info "installing uv (Python manager) from https://astral.sh/uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
have uv && ok "uv" || { echo "uv could not be installed - see https://docs.astral.sh/uv/"; exit 1; }
if ! have docker; then
  if [ "$OS" = Darwin ]; then
    ask "Install Docker Desktop (brew --cask docker)?" && brew install --cask docker
  else
    ask "Install Docker Engine with the official script (https://get.docker.com)?" && curl -fsSL https://get.docker.com | $SUDO sh
    $SUDO systemctl enable --now docker 2>/dev/null || true
    [ -n "${USER:-}" ] && $SUDO usermod -aG docker "$USER" 2>/dev/null || true
  fi
fi
have docker || { echo "Docker is required."; exit 1; }

step "Starting Docker"
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  [ "$OS" = Darwin ] && open -a Docker 2>/dev/null || true
  [ "$OS" = Linux ] && $SUDO systemctl start docker 2>/dev/null || true
  for _ in $(seq 1 80); do
    docker info >/dev/null 2>&1 && break
    if [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"; break; fi
    sleep 3
  done
fi
$DOCKER info >/dev/null 2>&1 || { echo "Docker is not running - start it and re-run this script."; exit 1; }
ok "Docker is running"

# ---------------------------------------------------------------- 2. hardware
step "Detecting hardware"
GPU_VENDOR=none; GPU_NAME=""; VRAM_GB=0; GFX=""
if have nvidia-smi && nvidia-smi >/dev/null 2>&1; then
  line="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | head -n1)"
  GPU_NAME="${line%%,*}"; mem="${line##*,}"; mem="${mem// /}"
  VRAM_GB=$(( (${mem%.*} + 512) / 1024 )); GPU_VENDOR=nvidia  # round: an "8 GB" card reports ~8188 MiB
elif [ "$OS" = Linux ] && { [ -e /dev/kfd ] || have rocm-smi; }; then
  GPU_VENDOR=amd; best=0
  for f in /sys/class/drm/card*/device/mem_info_vram_total; do
    [ -r "$f" ] || continue; v="$(cat "$f")"; [ "$v" -gt "$best" ] && best="$v"
  done
  VRAM_GB=$(( (best + 536870912) / 1024 / 1024 / 1024 ))  # rounded to the nearest GB
  have rocminfo && GFX="$(rocminfo 2>/dev/null | grep -o 'gfx[0-9a-f]*' | head -n1 || true)"
  GPU_NAME="AMD GPU ${GFX}"
elif [ "$OS" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
  GPU_VENDOR=apple; GPU_NAME="Apple Silicon"
fi
if [ "$OS" = Darwin ]; then RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )); else RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 )); fi
info "RAM: ${RAM_GB} GB"
info "GPU: ${GPU_NAME:-none} (${GPU_VENDOR}, ${VRAM_GB} GB VRAM)"

CODER_MODEL=""; CODER_LEN=0
if   [ "$VRAM_GB" -ge 40 ]; then CODER_MODEL="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"; CODER_LEN=32768
elif [ "$VRAM_GB" -ge 20 ]; then CODER_MODEL="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"; CODER_LEN=32768
elif [ "$VRAM_GB" -ge 12 ]; then CODER_MODEL="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"; CODER_LEN=16384
elif [ "$VRAM_GB" -ge 8 ];  then CODER_MODEL="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"; CODER_LEN=8192
elif [ "$VRAM_GB" -ge 6 ];  then CODER_MODEL="Qwen/Qwen2.5-Coder-3B-Instruct-AWQ"; CODER_LEN=8192
fi

COMPOSE_FILES="docker-compose.yml"; PROFILES=""
if [ "$SKIP_MODELS" = 1 ]; then
  info "model containers skipped (--skip-models)"
elif [ -n "$CODER_MODEL" ] && { [ "$GPU_VENDOR" = nvidia ] || [ "$GPU_VENDOR" = amd ]; }; then
  COMPOSE_FILES="$COMPOSE_FILES:docker/compose.${GPU_VENDOR}.yml"; PROFILES="coder"
  info "${GPU_VENDOR} GPU -> ${CODER_MODEL} with ${CODER_LEN} tokens of context"
  # Linux has no OS-native OCR, so the MiniCPM-V vision model is the last OCR layer - only when it fits.
  if [ "$OS" = Linux ] && { [ "$VISION" = on ] || { [ "$VISION" = auto ] && [ "$VRAM_GB" -ge 20 ]; }; }; then
    PROFILES="coder,vision"
  elif [ "$OS" = Linux ] && [ "$VISION" = auto ]; then
    warn "Not enough VRAM for MiniCPM-V next to the coder: OCR will use Tesseract + RapidOCR (override with --vision)."
  fi
  if [ "$GPU_VENDOR" = amd ] && [[ "$GFX" == gfx120* ]]; then
    warn "RDNA4 GPU (${GFX}): set VLLM_ROCM_IMAGE in .env to a 'gfx120X-all' tag from https://hub.docker.com/r/rocm/vllm/tags"
  fi
else
  warn "The Qwen coder cannot run locally on this device (needs an NVIDIA or Linux/ROCm AMD GPU with >= 6 GB VRAM) - models never run on the CPU."
  info "Options: point CTE_CODER_URL in .env at a GPU machine running the coder / your AI server, or add a Claude / ChatGPT key in Settings."
fi
[ "$RAM_GB" -lt 8 ] && warn "Less than 8 GB RAM - your device is low-end for local AI; consider a Claude / ChatGPT key."

if [ "$GPU_VENDOR" = nvidia ] && [ "$OS" = Linux ] && [ -n "$PROFILES" ] && ! have nvidia-ctk; then
  if ask "Install the NVIDIA Container Toolkit (lets Docker use your GPU)?"; then
    if have apt-get; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | $SUDO gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
      $SUDO apt-get update -y && $SUDO apt-get install -y nvidia-container-toolkit
    elif have dnf; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | $SUDO tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null
      $SUDO dnf install -y nvidia-container-toolkit
    fi
    have nvidia-ctk && $SUDO nvidia-ctk runtime configure --runtime=docker && $SUDO systemctl restart docker
  fi
fi

# ---------------------------------------------------------------- 3. .env
step "Writing .env (private, git-ignored)"
FRESH=0
if [ ! -f "$ENV_FILE" ]; then
  FRESH=1
  ( umask 077; echo "# Generated by scripts/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Private - never commit this file." > "$ENV_FILE" )
fi
chmod 600 "$ENV_FILE"  # database password + the key protecting stored API keys: readable by you only
# a copied .env.example still holds public placeholders ("change-me...") - never keep those as real secrets
env_placeholder() { local v; v="$( (grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true) | head -n1 | cut -d= -f2-)"; [ -z "$v" ] || [[ "$v" == change-me* || "$v" == changeme* || "$v" == your-* || "$v" == \<* ]]; }
VOLUME_EXISTS=0
$DOCKER volume inspect cte_cte_pgdata >/dev/null 2>&1 && VOLUME_EXISTS=1
{ env_has CTE_SECRET_KEY && ! env_placeholder CTE_SECRET_KEY; } || set_env CTE_SECRET_KEY "$(secret 48)"
env_has CTE_DB_USER      || set_env CTE_DB_USER cte
env_has CTE_DB_NAME      || set_env CTE_DB_NAME cte
env_has CTE_DB_PORT      || set_env CTE_DB_PORT 5440
env_has CTE_REDIS_PORT   || set_env CTE_REDIS_PORT 6380
if ! env_has CTE_DB_PASSWORD; then
  if [ "$VOLUME_EXISTS" = 1 ]; then set_env CTE_DB_PASSWORD cte_local_dev; warn "existing database volume found - keeping its original password"
  else set_env CTE_DB_PASSWORD "$(secret 24)"; fi
elif env_placeholder CTE_DB_PASSWORD; then
  if [ "$VOLUME_EXISTS" = 1 ]; then warn "CTE_DB_PASSWORD is a placeholder but the database already uses it - change it in PostgreSQL first"
  else set_env CTE_DB_PASSWORD "$(secret 24)"; warn "CTE_DB_PASSWORD was a placeholder - replaced with a random password"; fi
fi
{ env_has CTE_VLLM_API_KEY && ! env_placeholder CTE_VLLM_API_KEY; } || set_env CTE_VLLM_API_KEY "$(secret 24)"
env_has CTE_GATEWAY_URL     || set_env CTE_GATEWAY_URL "http://127.0.0.1:4000/v1"
env_has CTE_GATEWAY_API_KEY || set_env CTE_GATEWAY_API_KEY ""
env_has CTE_GATEWAY_MODEL   || set_env CTE_GATEWAY_MODEL gateway-default
env_has CTE_CODER_URL       || set_env CTE_CODER_URL "http://127.0.0.1:8011/v1"
env_has CTE_CODER_MODEL     || set_env CTE_CODER_MODEL qwen-coder
env_has CTE_VISION_URL      || set_env CTE_VISION_URL "http://127.0.0.1:8012/v1"
env_has CTE_VISION_MODEL    || set_env CTE_VISION_MODEL minicpm-v
env_has VISION_MODEL        || set_env VISION_MODEL "openbmb/MiniCPM-V-2_6"
env_has HF_TOKEN            || set_env HF_TOKEN ""
set_env COMPOSE_PATH_SEPARATOR ":"
set_env COMPOSE_FILE "$COMPOSE_FILES"
set_env COMPOSE_PROFILES "$PROFILES"
set_env CTE_GPU_VENDOR "$GPU_VENDOR"
if [ -n "$PROFILES" ] && [ -n "$CODER_MODEL" ]; then
  set_env CTE_CODER_ENGINE "vLLM (Docker, ${GPU_VENDOR} GPU)"
  set_env QWEN_CODER_MODEL "$CODER_MODEL"; set_env CODER_MAX_MODEL_LEN "$CODER_LEN"
  if [[ "$PROFILES" == *vision* ]]; then set_env CODER_GPU_UTIL 0.55; set_env VISION_GPU_UTIL 0.35; else set_env CODER_GPU_UTIL 0.85; fi
fi
[ "$FRESH" = 1 ] && ok ".env created with random secrets" || ok ".env updated (secrets kept)"

# ---------------------------------------------------------------- 4. Python
step "Python 3.12 environment (uv)"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
PY="$ROOT/.venv/bin/python"
ok "packages installed"

# ---------------------------------------------------------------- 5. assets
step "Downloading Tailwind CSS, Icons8 icons and fonts locally"
"$PY" scripts/fetch_assets.py && ok "assets ready" || warn "some assets could not be downloaded - fallbacks will be used"
step "Pre-downloading the local embedding model (pgvector memory)"
"$PY" -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='.runtime/models/fastembed')" >/dev/null 2>&1 \
  && ok "BAAI/bge-small-en-v1.5 cached" || warn "embedding model download failed - hashing embeddings will be used for now"
step "Pre-downloading the avatar voice (Piper neural text-to-speech)"
"$PY" -c "from app.services.tts import tts, DEFAULT_VOICE; tts.download(DEFAULT_VOICE)" >/dev/null 2>&1 \
  && ok "default voice ready" || warn "voice download failed - the app retries on first use (or uses the browser voice)"

# ---------------------------------------------------------------- 6. AI server repo
step "AI gateway (your AI server)"
REPO="$("$PY" -c "import json;print(json.load(open('config.json'))['app'].get('ai_server_repo',''))")"
PORT="$("$PY" -c "import json;print(json.load(open('config.json'))['app'].get('port',8010))")"
if [ -z "$REPO" ] || [[ "$REPO" == \<* ]]; then
  warn "config.json -> app.ai_server_repo is not set yet - add your AI server repo link there."
else
  info "AI server repo: $REPO"
  if [ ! -d ai-server ] && have git && ask "Download (git clone) the AI server repo into ./ai-server?"; then git clone --depth 1 "$REPO" ai-server || warn "clone failed"; fi
  info "Start it as its README describes, then put its URL / key / model into .env (CTE_GATEWAY_*)."
fi
info "Note: locally-run Gemma models are not supported - use the AI server repo instead."

# ---------------------------------------------------------------- 7. Docker services
step "Starting Docker services"
$DOCKER compose up -d postgres redis
PG_HEALTH=""
for _ in $(seq 1 60); do
  PG_HEALTH="$($DOCKER inspect -f '{{.State.Health.Status}}' cte-postgres 2>/dev/null || true)"
  [ "$PG_HEALTH" = healthy ] && break
  sleep 2
done
if [ "$PG_HEALTH" = healthy ]; then ok "PostgreSQL + pgvector and Redis are up"
else warn "PostgreSQL is not healthy yet (status: ${PG_HEALTH:-unknown}) - see: docker compose logs postgres"; fi
if [ -n "$PROFILES" ]; then
  mkdir -p .runtime
  info "Starting the model container(s) in the background (first run downloads the image + weights; log: .runtime/compose-models.log)"
  nohup $DOCKER compose up -d > .runtime/compose-models.log 2>&1 &
fi

# ---------------------------------------------------------------- 8. firewall
if [ "$NO_FIREWALL" = 0 ] && [ "$OS" = Linux ]; then
  if have ufw && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
    ask "Allow other devices on your network to open the app (ufw allow ${PORT}/tcp)?" && $SUDO ufw allow "${PORT}/tcp" comment "Critical Thinking Enhancer" || true
  elif have firewall-cmd && $SUDO firewall-cmd --state >/dev/null 2>&1; then
    ask "Allow other devices on your network to open the app (firewalld, ${PORT}/tcp)?" && { $SUDO firewall-cmd --add-port="${PORT}/tcp" --permanent && $SUDO firewall-cmd --reload; } || true
  fi
fi

# ---------------------------------------------------------------- 9. run
step "Ready"
info "This computer:  http://localhost:${PORT}"
if [ "$OS" = Darwin ]; then IPS="$(ipconfig getifaddr en0 2>/dev/null || true)"; else IPS="$(hostname -I 2>/dev/null || true)"; fi
for ip in $IPS; do case "$ip" in 127.*|172.17.*|*:*) ;; *) info "Your network:   http://${ip}:${PORT}" ;; esac; done
if [ "$NORUN" = 1 ]; then ok "Installed. Run ./scripts/install.sh again to start the app."; exit 0; fi
( sleep 6; if have xdg-open; then xdg-open "http://localhost:${PORT}" >/dev/null 2>&1; elif have open; then open "http://localhost:${PORT}"; fi ) &
info "Press Ctrl+C to stop the app."
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
