#!/usr/bin/env bash
# Starship OS — GPU Detection & Ollama Configuration
# Detects GPU vendor (NVIDIA/AMD/None) and configures Ollama accordingly.
# Supports: bare metal, WSL2, Proxmox passthrough, Docker.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[GPU]${NC} $*"; }
warn() { echo -e "${YELLOW}[GPU]${NC} $*"; }
err()  { echo -e "${RED}[GPU]${NC} $*" >&2; }
info() { echo -e "${CYAN}[GPU]${NC} $*"; }

STATE_FILE="/tmp/agnetic-gpu-state.json"
OLLAMA_SERVICE="ollama"

# Determine config path: root → /etc, user → ~/.config/agnetic
if [[ "$(id -u)" == "0" ]]; then
    OLLAMA_ENV="/etc/systemd/system/ollama.service.d/override.conf"
    SYSTEM_MODE=true
else
    OLLAMA_ENV="$HOME/.config/agnetic/ollama-override.conf"
    SYSTEM_MODE=false
fi

# ─── Detection ───────────────────────────────────────────────────────
detect_gpu_vendor() {
    local vendor="none"

    # NVIDIA: nvidia-smi or /dev/nvidia*
    if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
        vendor="nvidia"
    elif ls /dev/nvidia* &>/dev/null 2>&1; then
        vendor="nvidia"
    fi

    # AMD: /dev/kfd or rocm-smi
    if [[ "$vendor" == "none" ]]; then
        if [[ -e /dev/kfd ]]; then
            vendor="amd"
        elif command -v rocm-smi &>/dev/null && rocm-smi &>/dev/null 2>&1; then
            vendor="amd"
        fi
    fi

    echo "$vendor"
}

detect_nvidia_info() {
    local name driver compute cuda_ver arch vram

    if ! command -v nvidia-smi &>/dev/null; then
        err "nvidia-smi not found"
        return 1
    fi

    name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    compute=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1)
    vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)

    # CUDA version from nvidia-smi output
    cuda_ver=$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version:\s+\K[0-9.]+' || echo "unknown")

    # Architecture from compute capability
    case "$compute" in
        8.9) arch="ada-lovelace" ;;
        8.6) arch="ampere" ;;
        8.0|8.1|8.2) arch="ampere" ;;
        7.5) arch="turing" ;;
        7.0) arch="volta" ;;
        6.1) arch="pascal" ;;
        *)   arch="unknown" ;;
    esac

    # Is this WSL?
    local is_wsl="false"
    if grep -qi microsoft /proc/version 2>/dev/null; then
        is_wsl="true"
    fi

    cat <<EOF
{
    "vendor": "nvidia",
    "name": "$name",
    "driver_version": "$driver",
    "compute_capability": "$compute",
    "cuda_version": "$cuda_ver",
    "architecture": "$arch",
    "vram": "$vram",
    "wsl2": $is_wsl
}
EOF
}

detect_amd_info() {
    local name driver vram arch

    if command -v rocm-smi &>/dev/null; then
        name=$(rocm-smi --showproductname 2>/dev/null | head -1 || echo "AMD GPU")
        driver=$(rocm-smi --showdriverversion 2>/dev/null | grep -oP 'Driver version:\s+\K\S+' || echo "unknown")
        vram=$(rocm-smi --showmeminfo vram 2>/dev/null | grep -oP 'Total Memory.*?:\s+\K[0-9]+' || echo "unknown")
    else
        name="AMD GPU (rocm-smi not available)"
        driver="unknown"
        vram="unknown"
    fi

    # ROCm version
    local rocm_ver="unknown"
    if [[ -f /opt/rocm/.info/version ]]; then
        rocm_ver=$(cat /opt/rocm/.info/version)
    elif [[ -d /opt/rocm ]]; then
        rocm_ver=$(basename /opt/rocm 2>/dev/null | sed 's/rocm-//')
    fi

    # Architecture from GPU ID
    local gpu_id
    gpu_id=$(rocm-smi --showid 2>/dev/null | grep -oP 'GPU\[\K[0-9]+' | head -1 || echo "0")

    case "$gpu_id" in
        0x7400|0x7401|0x7402|0x7403) arch="gfx1010" ;;  # RDNA 1
        0x7408|0x740B|0x740C) arch="gfx1030" ;;          # RDNA 2
        0x740D|0x740E|0x740F) arch="gfx1036" ;;          # RDNA 3
        *) arch="unknown" ;;
    esac

    cat <<EOF
{
    "vendor": "amd",
    "name": "$name",
    "driver_version": "$driver",
    "rocm_version": "$rocm_ver",
    "architecture": "$arch",
    "vram": "$vram"
}
EOF
}

detect_intel_info() {
    # Intel Arc / integrated
    local name="Intel GPU"
    if [[ -d /dev/dri ]]; then
        name=$(ls /dev/dri/ 2>/dev/null | grep render | head -1 || echo "Intel GPU")
    fi
    cat <<EOF
{
    "vendor": "intel",
    "name": "$name",
    "note": "No Ollama GPU support for Intel. CPU-only inference."
}
EOF
}

# ─── Ollama Configuration ────────────────────────────────────────────
configure_ollama_nvidia() {
    log "Configuring Ollama for NVIDIA GPU..."

    if [[ "$SYSTEM_MODE" == "true" ]]; then
        mkdir -p /etc/systemd/system/ollama.service.d
    else
        mkdir -p "$(dirname "$OLLAMA_ENV")"
    fi

    cat > "$OLLAMA_ENV" <<'EOF'
[Service]
Environment="OLLAMA_GPU_LAYERS=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
EOF

    log "Ollama override written to $OLLAMA_ENV"

    if [[ "$SYSTEM_MODE" == "true" ]] && systemctl is-active --quiet "$OLLAMA_SERVICE" 2>/dev/null; then
        systemctl daemon-reload
        systemctl restart "$OLLAMA_SERVICE"
        sleep 2
        if ollama list 2>/dev/null | grep -q .; then
            log "Ollama restarted and models available"
        else
            warn "Ollama restarted but no models found"
        fi
    elif [[ "$SYSTEM_MODE" == "false" ]]; then
        info "User mode — config saved. Apply manually:"
        info "  export OLLAMA_GPU_LAYERS=-1"
        info "  export OLLAMA_NUM_PARALLEL=2"
        info "  export OLLAMA_MAX_LOADED_MODELS=2"
    else
        warn "Ollama service not running — start it after installation"
    fi
}

configure_ollama_amd() {
    log "Configuring Ollama for AMD GPU (ROCm)..."

    if [[ "$SYSTEM_MODE" == "true" ]]; then
        mkdir -p /etc/systemd/system/ollama.service.d
    else
        mkdir -p "$(dirname "$OLLAMA_ENV")"
    fi

    # Detect ROCm version for library path
    local rocm_path="/opt/rocm"
    if [[ -d "$rocm_path" ]]; then
        cat > "$OLLAMA_ENV" <<EOF
[Service]
Environment="OLLAMA_GPU_LAYERS=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="ROCR_VISIBLE_DEVICES=0"
Environment="LD_LIBRARY_PATH=${rocm_path}/lib"
EOF
    else
        cat > "$OLLAMA_ENV" <<'EOF'
[Service]
Environment="OLLAMA_GPU_LAYERS=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
EOF
    fi

    log "Ollama override written to $OLLAMA_ENV"

    if [[ "$SYSTEM_MODE" == "true" ]] && systemctl is-active --quiet "$OLLAMA_SERVICE" 2>/dev/null; then
        systemctl daemon-reload
        systemctl restart "$OLLAMA_SERVICE"
        sleep 2
    elif [[ "$SYSTEM_MODE" == "false" ]]; then
        info "User mode — config saved to $OLLAMA_ENV"
    fi
}

configure_ollama_cpu() {
    log "No GPU detected — Ollama will use CPU-only inference"

    if [[ "$SYSTEM_MODE" == "true" ]]; then
        mkdir -p /etc/systemd/system/ollama.service.d
    else
        mkdir -p "$(dirname "$OLLAMA_ENV")"
    fi

    cat > "$OLLAMA_ENV" <<'EOF'
[Service]
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF

    if [[ "$SYSTEM_MODE" == "true" ]] && systemctl is-active --quiet "$OLLAMA_SERVICE" 2>/dev/null; then
        systemctl daemon-reload
        systemctl restart "$OLLAMA_SERVICE"
    fi
}

# ─── Health Check ────────────────────────────────────────────────────
check_ollama_health() {
    local status="unknown"
    local gpu_used="false"

    if ! command -v ollama &>/dev/null; then
        echo '{"ollama": "not_installed"}'
        return
    fi

    # Check if Ollama is running
    if pgrep -x ollama &>/dev/null || systemctl is-active --quiet "$OLLAMA_SERVICE" 2>/dev/null; then
        status="running"
    else
        status="stopped"
    fi

    # Check GPU usage
    if [[ "$VENDOR" == "nvidia" ]]; then
        gpu_used=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null | head -1 || echo "0")
        if [[ "$gpu_used" != "0" ]]; then
            gpu_used="true"
        else
            gpu_used="false"
        fi
    fi

    # Count loaded models
    local model_count
    model_count=$(ollama list 2>/dev/null | tail -n +2 | wc -l || echo "0")

    cat <<EOF
{
    "ollama_status": "$status",
    "gpu_active": $gpu_used,
    "models_installed": $model_count,
    "endpoint": "http://localhost:11434"
}
EOF
}

# ─── Main ────────────────────────────────────────────────────────────
main() {
    local cmd="${1:-detect}"

    case "$cmd" in
        detect)
            echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
            echo -e "${BLUE}║  Starship OS — GPU Detect   ║${NC}"
            echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"
            echo ""

            VENDOR=$(detect_gpu_vendor)
            info "GPU Vendor: $VENDOR"

            case "$VENDOR" in
                nvidia)
                    detect_nvidia_info | tee "$STATE_FILE"
                    ;;
                amd)
                    detect_amd_info | tee "$STATE_FILE"
                    ;;
                intel)
                    detect_intel_info | tee "$STATE_FILE"
                    ;;
                *)
                    warn "No supported GPU detected"
                    echo '{"vendor":"none"}' | tee "$STATE_FILE"
                    ;;
            esac

            echo ""
            info "State saved to $STATE_FILE"
            ;;

        configure)
            VENDOR=$(detect_gpu_vendor)

            case "$VENDOR" in
                nvidia) configure_ollama_nvidia ;;
                amd)    configure_ollama_amd ;;
                *)      configure_ollama_cpu ;;
            esac
            ;;

        health)
            VENDOR=$(detect_gpu_vendor)
            check_ollama_health
            ;;

        full)
            main detect
            echo ""
            main configure
            echo ""
            main health
            ;;

        *)
            echo "Usage: $0 {detect|configure|health|full}"
            exit 1
            ;;
    esac
}

main "$@"
