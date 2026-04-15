# Multi-Engine Support for OpenSearch Benchmark

## Goal

Add an engine abstraction layer to OpenSearch Benchmark so it can benchmark databases other than OpenSearch (e.g., Vespa). The engine registry pattern allows plugging in new engines without modifying core coordinator logic.

## Branch / Base

- Branch: `feat/multi-engine-support`
- Base commit: `1288c835`
- Previous commit: `1904adac` (engine abstraction layer + OpenSearch engine)

## Engine Interface Contract

Each engine module must provide:

```python
def create_client_factory(hosts, client_options):
    """Return a factory with .create() and .create_async() methods."""

def create_async_client(hosts, client_options, cfg=None):
    """Return an async client directly (called by AsyncIoAdapter)."""

def register_runners(register_runner_fn):
    """Register engine-specific runners using the provided registration function."""

def wait_for_client(client, max_attempts=40) -> bool:
    """Return True if the target engine is reachable."""

def on_execute_error(e):
    """Handle engine-specific errors. Return (total_ops, unit, meta, fatal) or None."""
```

## What's Been Done

### 1. Engine Registry Module (`osbenchmark/engine/`)

**`osbenchmark/engine/__init__.py`** — Engine registry with:
- `register_engine(name, module)` — Register an engine by name
- `get_engine(name)` — Retrieve engine module (lazy-loads built-in engines)
- `available_engines()` — List registered engine names
- Built-in `"opensearch"` and `"vespa"` engines auto-registered on first access
- Vespa registration guarded by ImportError (pyvespa optional dependency)

### 2. OpenSearch Engine (`osbenchmark/engine/opensearch/__init__.py`)

Wraps existing infrastructure into the engine interface:
- `create_client_factory(hosts, client_options)` — Wraps `client.OsClientFactory`
- `create_async_client(hosts, client_options, cfg)` — Creates `UnifiedClientFactory` (REST + gRPC), returns async client directly
- `register_runners(register_runner_fn)` — Calls `runner.register_default_runners()`
- `wait_for_client(os_client, max_attempts)` — Calls `client.wait_for_rest_layer()`
- `on_execute_error(e)` — Handles `opensearchpy.TransportError`

### 3. Vespa Engine (`osbenchmark/engine/vespa/__init__.py`) — Ingestion Only

**VespaClientFactory**:
- Wraps `vespa.application.Vespa` app object
- Extracts host URL from OSB's TargetHosts format
- `create()` / `create_async()` both return the Vespa app

**VespaBulkFeed runner**:
- Registered as `"bulk"` (same operation name as OpenSearch's BulkIndex)
- Extracts document lines from the standard bulk body format (handles action-metadata interleaving)
- Feeds documents individually via async Document v1 API (`app.asyncio()` + `feed_data_point`)
- Uses `asyncio.Semaphore` for concurrency control (`vespa-max-workers`, default 64)
- Configurable HTTP/2 connections (`vespa-max-connections`, default 1)
- Returns same metadata format as BulkIndex: weight, unit, success-count, error-count, etc.

**wait_for_client**: Polls `get_application_status()` with 3s intervals

**on_execute_error**: Handles `httpx.ConnectError` (fatal), `httpx.TimeoutException`, `httpx.HTTPStatusError`, and `vespa.exceptions.VespaError`

### 4. CLI Argument (`osbenchmark/benchmark.py`)

- Added `--engine-type` argument (default: `"opensearch"`) to `test_run_parser`
- Stored in config as `("engine", "type")`

### 5. Worker Coordinator Changes (`osbenchmark/worker_coordinator/worker_coordinator.py`)

- **Import**: `from osbenchmark.engine import get_engine`
- **Config loading**: Added `"engine"` to config sections in `load_local_config()`
- **Client creation** (`WorkerCoordinator`): Uses engine registry for sync clients
- **REST readiness** (`wait_for_rest_api`): Delegates to `engine.wait_for_client()`
- **Runner registration** (`Worker`): Delegates to `engine.register_runners()`
- **Async client creation** (`AsyncIoAdapter.run`): Fully engine-agnostic — calls `engine.create_async_client(hosts, options, cfg=self.cfg)`
- **Async cleanup**: Generic close logic (checks `transport` then `close()`)
- **Error handling** (`execute_single`): Generic `except Exception` delegates to `engine.on_execute_error()`
- **Engine type propagation**: Passed through `AsyncExecutor` → `execute_single`

## Key Design Decisions

1. **Shared AsyncIoAdapter/Executor**: All engines share the same scheduling, timing, and metrics infrastructure. Engine-specific behavior lives in runners and the engine module.
2. **Vespa feeds doc-by-doc**: No bulk endpoint in Vespa. Throughput comes from async parallelism (semaphore-bounded), not batch size.
3. **Same param source**: Vespa reuses the existing BulkIndexParamSource — the runner just extracts doc lines from the standard body format.
4. **Same runner name**: Vespa registers its runner as `"bulk"` so existing workloads work without modification.

## Files Modified

| File | Status |
|------|--------|
| `osbenchmark/engine/__init__.py` | New |
| `osbenchmark/engine/opensearch/__init__.py` | New |
| `osbenchmark/engine/vespa/__init__.py` | New |
| `osbenchmark/benchmark.py` | Modified |
| `osbenchmark/worker_coordinator/worker_coordinator.py` | Modified |

## What's Left To Do

- [ ] Write unit tests for engine registry, OpenSearch engine, Vespa engine
- [ ] Integration test with a real Vespa instance
- [ ] Add `pyvespa` as optional dependency in setup.py/pyproject.toml
- [ ] (Future) Add Vespa query/search runner
- [ ] (Future) Add more engines
