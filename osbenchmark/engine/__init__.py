# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.

"""
Engine registry for multi-database support.

Each engine module must provide:
- client_factory_class: A class with __init__(hosts, client_options) and create()/create_async() methods
- register_runners(runner_registry_fn): Registers engine-specific runners
- wait_for_client(client, max_attempts=40) -> bool: Checks if the target is reachable
"""

import logging

from osbenchmark import exceptions

_ENGINE_REGISTRY = {}

logger = logging.getLogger(__name__)


def register_engine(name, engine_module):
    """
    Register an engine module.

    :param name: Engine name (e.g. "opensearch", "vespa")
    :param engine_module: Module providing client_factory_class, register_runners, wait_for_client
    """
    _ENGINE_REGISTRY[name] = engine_module
    logger.info("Registered engine [%s].", name)


def _ensure_builtin_engines():
    """Lazily register built-in engines on first access."""
    if "opensearch" not in _ENGINE_REGISTRY:
        from osbenchmark.engine import opensearch as opensearch_engine
        register_engine("opensearch", opensearch_engine)


def get_engine(name):
    """
    Retrieve a registered engine module by name.

    :param name: Engine name
    :return: The engine module
    :raises SystemSetupError: If the engine is not registered
    """
    _ensure_builtin_engines()
    if name not in _ENGINE_REGISTRY:
        available = ", ".join(sorted(_ENGINE_REGISTRY.keys())) or "(none)"
        raise exceptions.SystemSetupError(
            f"Unknown engine type [{name}]. Available engines: [{available}]."
        )
    return _ENGINE_REGISTRY[name]


def available_engines():
    """Return list of registered engine names."""
    return sorted(_ENGINE_REGISTRY.keys())
