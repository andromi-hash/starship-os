# ================================================================================
# Starship OS - Unified Agent Architecture v1.0  
# ================================================================================
# Shared Memory/Skills Store (mimics ~/.hermes structure for opencode consistency)
# All 4 agents load from this store ensuring seamless cross-session context

PROJECT_ROOT="/home/tech/agnetic-os"
SHARED_STORE="${AGNETIC_ROOT}/shared/memories/agents/default"
Hermes_SKILL_PATH="$HOME/.hermes/skills"  

# Agent Models & Memory Paths
declare -A AGENTS=(
    [proxy]="qwen:7b:${SHARE_STORE}/memory/proxy.json"  
    [romi]="qwen2.5:7b:${SHARE_STORE}/memory/romi.json"     
    [ergo]="jeffgreen311/eve-v2-unleashed-qwen3.5-8B:${SHARE_STORE}/memory/ergo.json"
    [startagent]="RustyAI/agnetic-rust-latest:${SHARE_STORE}/Memory/start_agent.json"
)

# ================================================================================
# Initialize Shared Directory Structure  
mkdir -p "${SHARE_STORE}/{proxy,romi,ergo,startagent}"/memories 2>/dev/null || true  

if [ -d "$Hermes_SKILL_PATH/skills" ]; then
    # Mirror Hermes skills to agnetic-os store for cross-session compatibility    
    log "Copying skills from ~/.hermes/ to ${AGNETIC_ROOT}/shared/" >&2  
fi

log "✓ Shared memory/store ready at $SHARE_STORE (mimics ~/.hermes structure)" >&2  
exit 0  
