# Multi-Engine Support for OpenSearch Benchmark

## Goal

Add an engine abstraction layer to OpenSearch Benchmark so it can benchmark databases other than OpenSearch (e.g., Vespa). The engine registry pattern allows plugging in new engines without modifying core coordinator logic.

## Branch / Base

- Branch: `feat/multi-engine-support`
- Base commit: `1288c835`
- Commits (in order):
  1. `1904adac` — Engine abstraction layer + OpenSearch engine
  2. `ccf12f11` — Vespa engine module for ingestion
  3. `ddebdf10` — Move VespaBulkFeed runner to runners.py
  4. `0eefd7dc` — Add pyvespa as optional dependency (extras_require)
  5. `e0270dcb` — Add pyvespa to main deps for local testing (temporary)
  6. `1ff5bc94` — Fail fast on import if pyvespa not installed
  7. `3e3509f4` — Use feed_iterable instead of manual async tasks
  8. `9694af32` — Use feed_async_iterable via asyncio.to_thread

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

### 1. Engine Registry Module (`osbenchmark/engine/__init__.py`)

- `register_engine(name, module)` / `get_engine(name)` / `available_engines()`
- Lazy registration of built-in engines (opensearch, vespa)
- Vespa registration guarded by ImportError (pyvespa optional)

### 2. OpenSearch Engine (`osbenchmark/engine/opensearch/__init__.py`)

Wraps existing infrastructure:
- `create_client_factory` → `client.OsClientFactory`
- `create_async_client` → `UnifiedClientFactory` (REST + gRPC), returns client directly
- `register_runners` → `runner.register_default_runners()`
- `wait_for_client` → `client.wait_for_rest_layer()`
- `on_execute_error` → handles `opensearchpy.TransportError`

### 3. Vespa Engine (`osbenchmark/engine/vespa/`)

**`__init__.py`** — Engine interface:
- `import vespa.application` at top level for fail-fast ImportError
- `VespaClientFactory` wrapping pyvespa `Vespa` app
- `create_client_factory` / `create_async_client`
- `register_runners` — registers `VespaBulkFeed` as `"bulk"`
- `wait_for_client` — polls `get_application_status()` with 3s intervals
- `on_execute_error` — handles `httpx.ConnectError` (fatal), `httpx.TimeoutException`, `httpx.HTTPStatusError`, `VespaError`

**`runners.py`** — VespaBulkFeed runner:
- Extracts doc lines from standard bulk body (strips OpenSearch action-metadata)
- Uses `feed_async_iterable()` via `asyncio.to_thread()` for non-blocking async feeding
- HTTP/2 multiplexing, configurable concurrency (`vespa-max-workers`, `vespa-max-connections`, `vespa-max-queue-size`)
- Returns same metadata format as BulkIndex: weight, unit, success-count, error-count

### 4. CLI Argument (`osbenchmark/benchmark.py`)

- `--engine-type` argument (default: `"opensearch"`)
- Stored in config as `("engine", "type")`

### 5. Worker Coordinator (`osbenchmark/worker_coordinator/worker_coordinator.py`)

- `from osbenchmark.engine import get_engine`
- `"engine"` added to config sections in `load_local_config()`
- Sync client creation via engine registry (preserves injected factory for tests)
- `wait_for_rest_api` delegates to `engine.wait_for_client()`
- Runner registration delegates to `engine.register_runners()`
- **Async client creation fully engine-agnostic** — `engine.create_async_client(hosts, options, cfg=self.cfg)`
- Generic async cleanup (checks `transport` then `close()`)
- `execute_single` error handling delegates to `engine.on_execute_error()`

### 6. Dependencies (`setup.py`)

- `pyvespa==1.1.2` added to `install_requires` (temporary for testing)
- Also in `extras_require["vespa"]` for production use: `pip install opensearch-benchmark[vespa]`
- TODO: Remove from `install_requires` before release

## Key Design Decisions

### Shared AsyncIoAdapter/Executor for all engines
All engines share the same scheduling, timing, and metrics infrastructure. Engine-specific behavior lives in runners and the engine module. Reasons:
- The adapter/executor are just scheduling + timing infrastructure (~95% identical across engines)
- Differences are already abstracted: client creation, runner execution, error handling, cleanup
- Duplicating would create maintenance burden for any fix to scheduling/throttling/metrics

### Vespa feeds doc-by-doc (no bulk endpoint)
Vespa has no `/_bulk` equivalent. Throughput comes from async parallelism, not batch size.
- `feed_async_iterable()` uses asyncio + HTTP/2 for concurrent feeding
- Wrapped in `asyncio.to_thread()` to avoid blocking OSB's event loop
- Configurable via `vespa-max-workers` (default 64) and `vespa-max-connections` (default 1, HTTP/2 multiplexes)

### Same param source, strip metadata in runner
The existing `BulkIndexParamSource` always injects OpenSearch action-metadata lines. The Vespa runner strips them via `_extract_docs()`. This is wasteful but:
- Overhead is negligible (~microseconds of string processing vs milliseconds of HTTP I/O)
- Avoids modifying core param source code that affects all engines
- Lets existing workloads work unchanged with `--engine-type vespa`
- Can revisit with engine-aware param source if profiling shows it matters

### Runner registered as "bulk"
Vespa registers its runner as `"bulk"` (same operation name as OpenSearch's BulkIndex) so existing workloads work without modification.

## Performance Considerations (Vespa vs OpenSearch)

| Aspect | OpenSearch | Vespa |
|---|---|---|
| HTTP requests per bulk | 1 (NDJSON POST) | N (one per doc, HTTP/2 multiplexed) |
| Throughput lever | Bigger bulk size | More concurrent workers/connections |
| Event loop behavior | `await bulk()` yields control | `asyncio.to_thread()` yields control |
| Measurement granularity | Per-bulk aggregate | Per-bulk aggregate (per-doc via callback) |
| Backpressure | Server blocks on slow response | `max_queue_size` + `max_workers` |
| Memory | Body sent as-is | JSON parse + re-serialize per doc |

## OpenSearch Bulk Indexing Flow (8 clients, bulk-size 5000, 100K docs)

```
100K docs → 4 workers (2 clients each) → 25K docs/worker
  → 5 bulks/worker (5000 docs each)
  → Each bulk = 1 HTTP POST /_bulk with 10K NDJSON lines
  → Clients within worker interleave via shared generator
  → All workers run in parallel (OS processes)
  → Total: 20 HTTP requests
```

For Vespa with same config: same partitioning, but each bulk of 5000 docs = 5000 individual HTTP/2 requests via `feed_async_iterable`.

## Vespa Test Environment

- Docker container: `podman run --name vespa --publish 8080:8080 --publish 19071:19071 vespaengine/vespa`
- Vespa version: 8.673.18
- Schema `post` deployed in content cluster `soposts`
- Fields: user, tags, questionId, creationDate, title, acceptedAnswerId, type, body
- Endpoint: `http://localhost:8080`
- Document API: `/document/v1/soposts/post/docid/{id}`
- Test document successfully fed and retrieved

### Schema definition (deployed via config server API)
```
schema post {
    document post {
        field user type string { indexing: attribute | summary }
        field tags type array<string> { indexing: attribute | summary }
        field questionId type string { indexing: attribute | summary }
        field creationDate type string { indexing: attribute | summary }
        field title type string { indexing: index | summary  index: enable-bm25 }
        field acceptedAnswerId type string { indexing: attribute | summary }
        field type type string { indexing: attribute | summary }
        field body type string { indexing: index | summary  index: enable-bm25 }
    }
    fieldset default { fields: title, body }
    rank-profile default { first-phase { expression: bm25(title) + bm25(body) } }
}
```

## Vespa Python Client (pyvespa) API Summary

| Operation | Method | Notes |
|---|---|---|
| Connect | `Vespa(url="http://host:8080")` | |
| Feed single doc | `app.feed_data_point(schema, data_id, fields)` | Sync, within `syncio()` context |
| Feed batch (threaded) | `app.feed_iterable(iter, schema, callback, ...)` | Thread pool, sync HTTP |
| Feed batch (async) | `app.feed_async_iterable(iter, schema, callback, ...)` | asyncio + HTTP/2 |
| Query | `app.query(body={"yql": "..."})` | YQL query language |
| Get doc | `app.get_data(schema, data_id)` | |
| Update doc | `app.update_data(schema, data_id, fields)` | Partial updates supported |
| Delete doc | `app.delete_data(schema, data_id)` | |
| Readiness | `app.get_application_status()` | `/ApplicationStatus` |
| Sync context | `with app.syncio() as sync_app:` | |
| Async context | `async with app.asyncio() as async_app:` | |

## Files Modified

| File | Status |
|------|--------|
| `osbenchmark/engine/__init__.py` | New |
| `osbenchmark/engine/opensearch/__init__.py` | New |
| `osbenchmark/engine/vespa/__init__.py` | New |
| `osbenchmark/engine/vespa/runners.py` | New |
| `osbenchmark/benchmark.py` | Modified |
| `osbenchmark/worker_coordinator/worker_coordinator.py` | Modified |
| `setup.py` | Modified |

## What's Left To Do

- [ ] End-to-end test with local Vespa instance (schema deployed, ready to test)
- [ ] Write unit tests for engine registry, OpenSearch engine, Vespa engine
- [ ] Move pyvespa from install_requires back to extras_require before release
- [ ] (Future) Engine-aware param source to skip action-metadata generation for non-OpenSearch engines
- [ ] (Future) Add Vespa query/search runner
- [ ] (Future) Per-document latency metrics for Vespa (currently only aggregate per-bulk)
