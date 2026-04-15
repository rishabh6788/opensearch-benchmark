# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.

"""
Vespa-specific runners for opensearch-benchmark.
"""

import json
import logging

logger = logging.getLogger(__name__)


class VespaBulkFeed:
    """
    Feeds documents to Vespa using pyvespa's feed_async_iterable.

    Uses asyncio + HTTP/2 for non-blocking parallel feeding, which
    cooperates properly with OSB's asyncio event loop.

    Expects the same param dict as OpenSearch's BulkIndex runner:
    - body: list of lines (alternating action-metadata + doc JSON when action-metadata-present is True)
    - bulk-size: number of documents in this bulk
    - unit: "docs"
    - action-metadata-present: bool
    - index: used as the Vespa schema name

    Additional Vespa-specific optional params:
    - vespa-namespace: Vespa namespace (defaults to schema name)
    - vespa-max-connections: HTTP/2 connections (default 1, HTTP/2 multiplexes)
    - vespa-max-workers: concurrent async feed tasks (default 64)
    - vespa-max-queue-size: max queue size (default 4000)
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
        max_queue_size = params.get("vespa-max-queue-size", 4000)

        body = params["body"]
        docs = self._extract_docs(body, with_action_metadata)

        success_count = 0
        error_count = 0
        error_details = set()

        def callback(response, doc_id):
            nonlocal success_count, error_count
            if response.is_successful():
                success_count += 1
            else:
                error_count += 1
                error_details.add((response.status_code, str(response.get_json())))

        def doc_iterable():
            for i, doc in enumerate(docs):
                if isinstance(doc, (str, bytes)):
                    doc = json.loads(doc)
                doc_id = doc.pop("_id", None) or doc.get("id", str(i))
                yield {"id": str(doc_id), "fields": doc}

        # feed_async_iterable is synchronous at the call site but internally
        # runs its own asyncio event loop. Run it in a thread to avoid
        # blocking OSB's event loop.
        await asyncio.to_thread(
            vespa_app.feed_async_iterable,
            iter=doc_iterable(),
            schema=schema,
            namespace=namespace,
            callback=callback,
            max_queue_size=max_queue_size,
            max_workers=max_workers,
            max_connections=max_connections,
        )

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
        if isinstance(body, bytes):
            lines = body.split(b"\n")
        elif isinstance(body, str):
            lines = body.split("\n")
        elif isinstance(body, list):
            lines = body
        else:
            lines = [body]

        if with_action_metadata:
            return [lines[i] for i in range(1, len(lines), 2) if lines[i]]
        return [line for line in lines if line]

    def __repr__(self):
        return "vespa-bulk-feed"
