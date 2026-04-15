# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.

"""
OpenSearch engine module for opensearch-benchmark.

Wraps the existing OsClientFactory, runners, and wait_for_rest_layer
to conform to the engine registry interface.
"""

from osbenchmark import client
from osbenchmark.worker_coordinator import runner


client_factory_class = client.OsClientFactory


def create_client_factory(hosts, client_options):
    """Create an OpenSearch client factory."""
    return client.OsClientFactory(hosts, client_options)


def create_async_client_factory(hosts, client_options, cfg=None):
    """Create an OpenSearch async client factory (REST + optional gRPC)."""
    from osbenchmark.utils import opts
    rest_client_factory = client.OsClientFactory(hosts, client_options)
    grpc_hosts = None
    if cfg:
        grpc_hosts = cfg.opts("client", "grpc_hosts", mandatory=False)
    if not grpc_hosts or not grpc_hosts.all_hosts:
        grpc_hosts = opts.TargetHosts("localhost:9400")
    return client.UnifiedClientFactory(rest_client_factory, grpc_hosts)


def register_runners(register_runner_fn):
    """Register all OpenSearch-specific runners.

    Note: register_runner_fn is accepted for interface compatibility but
    register_default_runners() uses the module-level register_runner directly.
    """
    runner.register_default_runners()


def wait_for_client(os_client, max_attempts=40):
    """Wait for OpenSearch REST API to become available."""
    return client.wait_for_rest_layer(os_client, max_attempts=max_attempts)


def on_execute_error(e):
    """
    Handle OpenSearch-specific exceptions from execute_single.

    :param e: The exception
    :return: (total_ops, total_ops_unit, request_meta_data, fatal) or None if not handled
    """
    import opensearchpy
    if isinstance(e, opensearchpy.TransportError):
        fatal_error = type(e) is opensearchpy.ConnectionError
        request_meta_data = {
            "success": False,
            "error-type": "transport"
        }
        if isinstance(e.status_code, int):
            request_meta_data["http-status"] = e.status_code
        if isinstance(e, opensearchpy.ConnectionTimeout):
            request_meta_data["error-description"] = "network connection timed out"
        elif e.info:
            request_meta_data["error-description"] = f"{e.error} ({e.info})"
        else:
            error_description = e.error.decode("utf-8") if isinstance(e.error, bytes) else str(e.error)
            request_meta_data["error-description"] = error_description
        return 0, "ops", request_meta_data, fatal_error
    return None


def close_clients(opensearch_clients):
    """Close OpenSearch async transport connections."""
    import asyncio
    async def _close():
        for s in opensearch_clients.values():
            await s.transport.close()
    asyncio.get_event_loop().run_until_complete(_close())
