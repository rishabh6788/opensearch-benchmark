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

import asyncio
import json


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
