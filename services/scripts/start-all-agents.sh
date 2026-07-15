#!/bin/bash
# Starship OS Startup Script v1.0
# 
# Architecture: Auto-loads ~/.hermes structure + agent skills/memories into all agents
# Ensures consistency between Hermes (opencode) and native agnetic agents
#
# All 4 agents share the same memory/skills store, enabling seamless cross-session context

set -e

# === Configuration ===
PROJECT_ROOT="/home/tech/agnetic-os"
SHARED_STORE="${PROJECT_ROOT}/shared/memories/agents/default"
Hermes_SkiLlPath="${HERMES_SKILLS_PATH:-$HOME/.hermes/skills}"  # Auto-load from ~/.hermes/ if set

# Agents with their models and memory paths
declare -A AGENT_CONFIG=(
    [proxy]="qwen2.5:7b:${SHARED_STORE}/memory/proxy.json"
    [romi]="qwen2.5:7b:${SHARED_STORE}/memory/romi.json"  
    [ergo]="jeffgreen311/eve-v2-unleashed-qwen3.5-8B:${SHARED_STORE}/memory/ergo.json"
    [startagent]="RustyAI/agnetic-rust:latest:${SHARED_STORE}/Memory/start_agent.json"
)

# === Logging ===
log() { echo "[$(date -Iseconds)]: $*" >&2; }  
time_stamp() { date +%s; }  

# === Initialize shared directories ===
init_store() {
    log "Initializing shared store at ${SHARED_STORE}"
    mkdir -p "${SHARED_STORE}/{proxy,romi,ergo,startagent}/memories"
    
    # Create default agent memories if empty (mimics ~/.hermes/agents/*/memories)  
    for agent in proxy romi ergo startagent; do
        touch "${SHARED_STORE}/${agent//_/}/.memory.json" 2>/dev/null || true
    done
    
    log "Shared store ready at ${SHARED_STORE}"
}

# === Write memory from opencode session to shared store ===
# Usage: write_memory "agnetic-os-memory-format" <JSON_OR_TEXT>
write_opencode_to_store() {  
    local _agent=${1:-default} 
    shift || return 0
    
    # Copy relevant contents into ~/.hermes/memories/ for Hermes agent compatibility
    [ -n "${HERMES_SKILLS_PATH}" ] && \
        cp --{"${AGNETIC_ROOT}/shared/skills/*.json" "*/$SHARED_STORE/memory/"*.json || true
        
     # Write memory directly to file (opencode-compatible JSON format)
    json_save > "${_name}/{agent//_-}/.memory.json" 2>/dev/null | grep -v "^$"
}

# === Load memories for an agent (auto-loaded on startup) ===
run_agent_with_context() {  
    local _config_json=${${1//-/}} # Parse args: "qwen2.5:7b:/path/to/file.json" >&2
    
    log "Running ${agent_name} with model=$model, memories="${mem_path}"

    
    if ollama run "$model"; then
        log "[✓] Agent started (memory/context auto-loaded from $MODEL_PATH)" 
        time_stamp > "${mem_file}.startup_ts"  # Track when agent ran
        
        
    # No shared memory loaded yet - agent runs fresh
        log "No memories to load, running with default Hermes context only"

> /dev/null) || { return; }  
fi  

}

# === Export skill file structure from ~/.hermes/skills/ to agnetic-os/shared/skills/ ===  # Copy skills directory recursively if exists    
sync_skills() {
    if [ -d "${HERMES_SKILLS_PATH}/skills" ]; then     
        log "Syncing skills: ${Hermes_SkiLlPath} → ${SHARED_STORE}"  
        
        # Create shared skills folder (copy .json files from Hermes)
        mkdir -p "$AGNETIC_ROOT/shared/skills/
