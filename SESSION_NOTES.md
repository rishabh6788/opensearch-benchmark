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
  9. `47391306` — Update session notes
  10. `a413fc63` — Fix bytes body handling in _extract_docs
  11. `129a5b49` — Use UUID for document IDs instead of loop index

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
- Handles `bytes` body (produced by `b"".join(bulk)` in param source)
- Uses `feed_async_iterable()` via `asyncio.to_thread()` for non-blocking async feeding
- HTTP/2 multiplexing, configurable concurrency (`vespa-max-workers`, `vespa-max-connections`, `vespa-max-queue-size`)
- Document ID resolution: `_id` field → `id` field → `uuid.uuid4()` (globally unique across all workers)
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

### Document ID generation
- Prefers `_id` (OpenSearch convention, popped from fields) or `id` from the document
- Falls back to `uuid.uuid4()` — globally unique across all workers/clients with no coordination
- UUID4 uses 128 bits of randomness from OS `/dev/urandom`, independent per process

## Bugs Found and Fixed

### Bug 1: bytes body not handled (Critical)
`BulkIndexParamSource` produces body as `bytes` via `b"".join(bulk)`. Original `_extract_docs` only checked `str` and `list`, causing the entire bytes blob to become one list element. With `action-metadata-present=True`, `range(1, 1, 2)` yielded nothing — zero docs extracted, silent false success.
**Fix**: Added `isinstance(body, bytes)` check, splitting on `b"\n"`.

### Bug 2: Loop index as fallback doc ID (Critical)
Using `str(i)` (loop index within a bulk) as fallback ID meant the same IDs (0, 1, 2...) were reused across every bulk, causing document overwrites.
**Fix**: Changed fallback to `uuid.uuid4()`.

### Bug 3: ImportError guard not triggering (Minor)
The `try/except ImportError` in `engine/__init__.py` for Vespa registration wouldn't trigger because `vespa/__init__.py` had no top-level pyvespa import — the import only happened at runtime in `VespaClientFactory.__init__`.
**Fix**: Added `import vespa.application` at top of `vespa/__init__.py`.

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

### Vespa schema vs OpenSearch index
- OpenSearch: Create index at runtime via `PUT /my-index` with mappings
- Vespa: Define schema in application package, deploy via config server API (static, not runtime)
- Schema must be deployed before benchmarking starts

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

## Vespa Python Client (pyvespa 1.1.2) API Summary

| Operation | Method | Notes |
|---|---|---|
| Connect | `Vespa(url="http://host:8080")` | |
| Feed single doc | `app.feed_data_point(schema, data_id, fields)` | Sync, within `syncio()` context |
| Feed batch (threaded) | `app.feed_iterable(iter, schema, callback, ...)` | Thread pool, sync HTTP |
| Feed batch (async) | `app.feed_async_iterable(iter, schema, callback, ...)` | asyncio + HTTP/2, own event loop |
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
