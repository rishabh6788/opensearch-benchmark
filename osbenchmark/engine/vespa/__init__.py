# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.

"""
Vespa engine module for opensearch-benchmark (ingestion-only).

Wraps pyvespa to conform to the engine registry interface.
Documents are fed individually via the Document v1 API with
async parallelism provided by httpx/HTTP2.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

class VespaClientFactory:
    """
    Wraps pyvespa's Vespa application object.

    Conforms to the factory interface expected by the engine registry:
    - create()       → sync client (VespaSync context-managed wrapper)
    - create_async() → the Vespa app itself (used with app.asyncio())
    """

    def __init__(self, hosts, client_options):
        from vespa.application import Vespa

        # hosts is a TargetHosts object; grab the first host string
        if hasattr(hosts, "all_hosts"):
            host_list = hosts.all_hosts
            host = host_list[list(host_list.keys())[0]][0] if isinstance(host_list, dict) else host_list[0]
        elif isinstance(hosts, list):
            host = hosts[0]
        else:
            host = str(hosts)

        # Normalise to a URL
        if not host.startswith("http"):
            host = f"http://{host}"

        self.url = host
        self.client_options = client_options
        self._app = Vespa(url=self.url)

    def create(self):
        """Return the Vespa app for sync usage."""
        return self._app

    def create_async(self):
        """Return the Vespa app for async usage."""
        return self._app


def create_client_factory(hosts, client_options):
    """Create a Vespa client factory."""
    return VespaClientFactory(hosts, client_options)


def create_async_client(hosts, client_options, cfg=None):
    """Create an async Vespa client."""
    return VespaClientFactory(hosts, client_options).create_async()


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

class VespaBulkFeed:
    """
    Feeds documents to Vespa one-by-one via the async Document v1 API.

    Expects the same param dict as OpenSearch's BulkIndex runner:
    - body: list of lines (alternating action-metadata + doc JSON when action-metadata-present is True)
    - bulk-size: number of documents in this bulk
    - unit: "docs"
    - action-metadata-present: bool
    - index: used as the Vespa schema name

    Additional Vespa-specific optional params:
    - vespa-namespace: Vespa namespace (defaults to schema name)
    - vespa-max-connections: httpx connections for this bulk (default 1)
    - vespa-max-workers: concurrent async feed tasks (default 64)
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def __call__(self, vespa_app, params):
        import asyncio

        schema = params.get("index", "doc")
        namespace = params.get("vespa-namespace", schema)
        bulk_size = params["bulk-size"]
        unit = params.get("unit", "docs")
        with_action_metadata = params.get("action-metadata-present", True)
        max_connections = params.get("vespa-max-connections", 1)
        max_workers = params.get("vespa-max-workers", 64)

        body = params["body"]
        docs = self._extract_docs(body, with_action_metadata)

        success_count = 0
        error_count = 0
        error_details = set()

        semaphore = asyncio.Semaphore(max_workers)

        async def _feed_one(app, doc_id, fields):
            nonlocal success_count, error_count
            async with semaphore:
                resp = await app.feed_data_point(
                    schema=schema,
                    namespace=namespace,
                    data_id=doc_id,
                    fields=fields,
                )
                if resp.is_successful():
                    success_count += 1
                else:
                    error_count += 1
                    error_details.add((resp.status_code, str(resp.get_json())))

        async with vespa_app.asyncio(connections=max_connections) as async_app:
            tasks = []
            for i, doc in enumerate(docs):
                if isinstance(doc, (str, bytes)):
                    doc = json.loads(doc)
                doc_id = doc.pop("_id", None) or doc.get("id", str(i))
                tasks.append(asyncio.create_task(_feed_one(async_app, str(doc_id), doc)))
            await asyncio.gather(*tasks)

        meta_data = {
            "index": schema,
            "weight": bulk_size,
            "unit": unit,
            "success": error_count == 0,
            "success-count": success_count,
            "error-count": error_count,
        }
        if error_count > 0:
            meta_data["error-type"] = "bulk"
            descriptions = []
            for status, reason in error_details:
                descriptions.append(f"HTTP status: {status}, message: {reason}")
            meta_data["error-description"] = "; ".join(descriptions)
        return meta_data

    @staticmethod
    def _extract_docs(body, with_action_metadata):
        """Extract document lines from the bulk body."""
        if isinstance(body, str):
            lines = body.split("\n")
        elif isinstance(body, list):
            lines = body
        else:
            lines = [body]

        if with_action_metadata:
            # Every other line is a document (odd-indexed lines: 1, 3, 5, ...)
            return [lines[i] for i in range(1, len(lines), 2) if lines[i]]
        return [line for line in lines if line]

    def __repr__(self):
        return "vespa-bulk-feed"


def register_runners(register_runner_fn):
    """Register Vespa-specific runners for ingestion."""
    register_runner_fn("bulk", VespaBulkFeed())


# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------

def wait_for_client(vespa_app, max_attempts=40):
    """Wait for Vespa application to become ready via /ApplicationStatus."""
    for attempt in range(max_attempts):
        try:
            status = vespa_app.get_application_status()
            if status is not None:
                logger.info("Vespa application is ready (attempt %d/%d).", attempt + 1, max_attempts)
                return True
        except Exception as e:
            logger.debug("Vespa not ready yet (attempt %d/%d): %s", attempt + 1, max_attempts, e)
        time.sleep(3)
    return False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def on_execute_error(e):
    """
    Handle Vespa-specific exceptions from execute_single.

    pyvespa uses httpx underneath, so we catch httpx exceptions
    and vespa.exceptions.VespaError.

    :return: (total_ops, unit, request_meta_data, fatal) or None if not handled
    """
    try:
        import httpx
        if isinstance(e, httpx.ConnectError):
            return 0, "ops", {
                "success": False,
                "error-type": "transport",
                "error-description": f"Vespa connection error: {e}",
            }, True
        if isinstance(e, httpx.TimeoutException):
            return 0, "ops", {
                "success": False,
                "error-type": "transport",
                "error-description": "Vespa connection timed out",
            }, False
        if isinstance(e, httpx.HTTPStatusError):
            return 0, "ops", {
                "success": False,
                "error-type": "transport",
                "http-status": e.response.status_code,
                "error-description": str(e),
            }, False
    except ImportError:
        pass

    try:
        from vespa.exceptions import VespaError
        if isinstance(e, VespaError):
            return 0, "ops", {
                "success": False,
                "error-type": "vespa",
                "error-description": str(e),
            }, False
    except ImportError:
        pass

    return None
