# Multi-Engine Support for OpenSearch Benchmark

## Goal

Add an engine abstraction layer to OpenSearch Benchmark so it can benchmark databases other than OpenSearch (e.g., Vespa, Elasticsearch). The engine registry pattern allows plugging in new engines without modifying core coordinator logic.

## Branch / Base

- Base commit: `1288c835` (HEAD detached)
- All changes are currently **uncommitted**

## What's Been Done

### 1. Engine Registry Module (`osbenchmark/engine/`)

**`osbenchmark/engine/__init__.py`** — Engine registry with:
- `register_engine(name, module)` — Register an engine by name
- `get_engine(name)` — Retrieve engine module (lazy-loads built-in engines)
- `available_engines()` — List registered engine names
- Built-in `"opensearch"` engine is auto-registered on first access

**`osbenchmark/engine/opensearch/__init__.py`** — OpenSearch engine conforming to the interface:
- `create_client_factory(hosts, client_options)` — Wraps `client.OsClientFactory`
- `create_async_client_factory(hosts, client_options, cfg)` — Wraps `client.UnifiedClientFactory` (REST + gRPC)
- `register_runners(register_runner_fn)` — Calls `runner.register_default_runners()`
- `wait_for_client(os_client, max_attempts)` — Calls `client.wait_for_rest_layer()`
- `on_execute_error(e)` — Handles `opensearchpy.TransportError` specifically
- `close_clients(opensearch_clients)` — Closes async transports

### 2. CLI Argument (`osbenchmark/benchmark.py`)

- Added `--engine-type` argument (default: `"opensearch"`) to `test_run_parser`
- Stored in config as `("engine", "type")` via `configure_test()`

### 3. Worker Coordinator Changes (`osbenchmark/worker_coordinator/worker_coordinator.py`)

- **Import**: Added `from osbenchmark.engine import get_engine`
- **Config loading**: Added `"engine"` to config sections in `load_local_config()`
- **Client creation** (`WorkerCoordinator`): Uses engine registry instead of hardcoded `OsClientFactory` (preserves injected factory for tests)
- **REST readiness** (`wait_for_rest_api`): Delegates to `engine.wait_for_client()`
- **Runner registration** (`Worker`): Delegates to `engine.register_runners()`
- **Async client creation** (`AsyncIoAdapter.run`): OpenSearch path uses `UnifiedClientFactory`; other engines use `engine.create_client_factory()`
- **Async cleanup** (`AsyncIoAdapter.run`): Generic close logic — checks `transport`, then `close()` method
- **Error handling** (`execute_single`): Replaced hardcoded `opensearchpy.TransportError` catch with generic `Exception` that delegates to `engine.on_execute_error()`; unhandled errors re-raise
- **Engine type propagation**: `engine_type` passed through `AsyncExecutor` → `execute_single`

## Engine Interface Contract

Each engine module must provide:

```python
def create_client_factory(hosts, client_options):
    """Return a factory with .create() and .create_async() methods."""

def register_runners(register_runner_fn):
    """Register engine-specific runners using the provided registration function."""

def wait_for_client(client, max_attempts=40) -> bool:
    """Return True if the target engine is reachable."""

def on_execute_error(e):
    """Handle engine-specific errors. Return (total_ops, unit, meta, fatal) or None."""
```

## What's Left To Do

- [ ] Write unit tests for the engine registry (`engine/__init__.py`)
- [ ] Write unit tests for the OpenSearch engine module
- [ ] Update existing worker_coordinator tests to account for engine abstraction
- [ ] Verify full test suite passes (`pytest`)
- [ ] Commit changes
- [ ] (Future) Implement a second engine (e.g., Vespa) to validate the abstraction

## Files Modified

| File | Status |
|------|--------|
| `osbenchmark/engine/__init__.py` | **New** |
| `osbenchmark/engine/opensearch/__init__.py` | **New** |
| `osbenchmark/benchmark.py` | Modified |
| `osbenchmark/worker_coordinator/worker_coordinator.py` | Modified |
