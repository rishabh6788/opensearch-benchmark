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

import logging
import time

from osbenchmark.engine.vespa.runners import VespaBulkFeed

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
