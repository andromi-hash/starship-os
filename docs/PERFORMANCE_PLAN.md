# Starship OS — C++ Performance Layer Plan

## Goal
Replace Python bottlenecks with native C++ modules for 10-100x speedup on critical hot paths, reducing CPU/GPU/RAM usage while maintaining the same Python API surface.

## Priority 1: Vector Search (LanceDB → Custom C++)

**Current**: LanceDB (Python) — embedding → index → search. 10-50ms per query.
**Target**: Custom C++ vector index using HNSW or IVF-PQ. <1ms per query.

- Replace LanceDB dependency with a lightweight C++ HNSW library (hnswlib or faiss C API)
- Embedding computation stays in Python (Ollama API call)
- Search + ranking moves to C++ via `pybind11` or `ctypes`
- Memory: ~80% reduction (no Arrow/Pandas overhead)
- Files: `src/vector_index.cpp`, `src/vector_index.h`, `wrappers/py_vector_index.py`

## Priority 2: Tool Sandbox Execution

**Current**: Python subprocess with JSON serialization. 5-50ms per tool call.
**Target**: C++ fork+exec with pipes. <0.5ms overhead.

- Replace `CommandExecutor` class with C++ sandbox process
- Implement `seccomp` BPF filtering (Linux) for security
- `clone()` with CLONE_NEWPID + CLONE_NEWNS namespaces per call
- Memory: eliminates Python's subprocess module overhead (~20MB per subprocess)
- Files: `src/sandbox.cpp`, `wrappers/py_sandbox.py`

## Priority 3: Policy Engine — Regex Matching

**Current**: Python re.match on every tool call. 0.1-1ms per check.
**Target**: C++ hybrid NFA/DFA regex engine. <0.01ms per check.

- Compile all policy patterns into a single DFA at load time
- Use `re2` library (by Google) for linear-time matching
- Path resolution via `shutil.which` stays in Python (infrequent)
- Files: `src/policy_matcher.cpp`, `wrappers/py_policy.py`

## Priority 4: Telemetry Serialization

**Current**: Python dict → json.dumps → httpx.post. 1-10ms per export.
**Target**: C++ protobuf/flatbuffers serialization. <0.1ms per export.

- Serialize TelemetryPoint structs to flatbuffers in C++
- Batch flush via shared memory ring buffer
- Files: `src/telemetry_export.cpp`, `schemas/telemetry.fbs`

## Priority 5: Memory Store Compaction

**Current**: Python JSON merge for memory consolidation. 10-100ms.
**Target**: C++ LSM-tree compaction. <5ms.

- Implement log-structured merge tree for memory persistence
- Automatic compaction in background thread
- Files: `src/lsm_store.cpp`

## Build System

```cmake
cmake_minimum_required(VERSION 3.20)
project(agnetic-core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
find_package(pybind11 REQUIRED)
find_package(fmt REQUIRED)

pybind11_add_module(vector_index src/vector_index.cpp)
target_link_libraries(vector_index PRIVATE fmt::fmt)

pybind11_add_module(sandbox src/sandbox.cpp)
target_link_libraries(sandbox PRIVATE seccomp fmt::fmt)
```

## Dependencies (C++)

| Library | Size | Purpose |
|---------|------|---------|
| pybind11 | ~1MB header-only | Python bindings |
| hnswlib | ~0.5MB header-only | Vector search |
| re2 | ~2MB | Safe regex |
| flatbuffers | ~1MB | Zero-copy serialization |
| libseccomp | ~0.2MB | Sandbox security |
| fmt | ~0.5MB header-only | String formatting |

**Total added disk**: ~5MB. **Total RAM savings**: ~200MB+ (from eliminating Python overhead in hot paths).

## Integration Strategy

1. Pure Python fallback always available (no C++ dependency at install time)
2. `try: from _vector_index import search` / `except ImportError: use LanceDB`
3. Build with `python3 setup.py build_ext --inplace` if compiler available
4. Pre-built wheels for x86_64 + aarch64 on PyPI

## Expected Gains

| Component | Before | After | Speedup |
|-----------|--------|-------|---------|
| Vector search (10K items) | 50ms | 0.5ms | 100x |
| Tool sandbox exec | 10ms | 0.5ms | 20x |
| Policy check | 1ms | 0.01ms | 100x |
| Telemetry batch export | 10ms | 0.1ms | 100x |
| Memory compaction | 100ms | 5ms | 20x |
| **Total pipeline** | **~170ms** | **~6ms** | **~28x** |
