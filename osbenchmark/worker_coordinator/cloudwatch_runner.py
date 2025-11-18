# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.

"""
CloudWatch Logs runner implementation for OpenSearch Benchmark.

This module provides runners for executing CloudWatch Logs Insights queries
through the AWS CloudWatch Logs API using the two-step query process:
1. StartQuery - Initiates the query and returns a queryId
2. GetQueryResults - Polls for results until the query completes
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from osbenchmark import exceptions
from osbenchmark.utils.parse import parse_string_parameter, parse_int_parameter, parse_float_parameter


logger = logging.getLogger(__name__)


# Base Runner class definition to avoid circular import
class Runner:
    """
    Base class for all operations.
    """
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def __aenter__(self):
        return self

    async def __call__(self, opensearch, params):
        """
        Runs the actual method that should be benchmarked.
        """
        raise NotImplementedError("abstract operation")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class CloudWatchLogsQuery(Runner):
    """
    Executes CloudWatch Logs Insights queries against AWS CloudWatch Logs.

    This runner implements the two-step CloudWatch Logs query process:
    1. Calls StartQuery API to initiate the query (returns queryId)
    2. Polls GetQueryResults API until query status is "Complete"

    Expected parameters:

    Mandatory:
    * `log-group-name` or `log-group-names`: Single log group name (string) or list of log group names
    * `query-string`: CloudWatch Logs query string
    * `start-time`: Query start time (Unix epoch seconds or relative like "-1h", "-30m")
    * `end-time`: Query end time (Unix epoch seconds, relative time, or "now")

    Optional:
    * `query-language`: Query language - "CWLI" (CloudWatch Logs Insights), "SQL", or "PPL" (Piped Processing Language) (default: "CWLI")
    * `region`: AWS region (default: from AWS config/environment)
    * `poll-interval`: Seconds between result polling attempts (default: 2)
    * `query-timeout`: Maximum seconds to wait for query completion (default: 300)
    * `limit`: Maximum number of log events to return (default: 10000, max: 10000)

    AWS Credentials:
    Credentials should be configured via environment variables, AWS config files,
    or IAM roles. Required permissions: logs:StartQuery, logs:GetQueryResults

    Returns:
    * `weight`: Number of log records returned
    * `unit`: "docs"
    * `query_id`: CloudWatch query ID
    * `status`: Final query status
    * `records_matched`: Total number of log records that matched the query
    * `records_scanned`: Total number of log records scanned
    * `bytes_scanned`: Total bytes scanned during query execution
    * `query_duration`: Time taken for query to complete (seconds)
    * `total_duration`: Total time including polling (seconds)

    Example workload operation:
    ```json
    {
      "operation": {
        "operation-type": "cloudwatch-logs-query",
        "log-group-name": "/aws/opensearch/my-domain",
        "query-string": "fields @timestamp, @message | sort @timestamp desc | limit 20",
        "start-time": "-1h",
        "end-time": "now",
        "region": "us-east-1"
      }
    }
    ```
    """

    def __init__(self):
        super().__init__()
        if not BOTO3_AVAILABLE:
            raise exceptions.SystemSetupError(
                "boto3 is required for CloudWatch Logs operations. "
                "Install it with: pip install boto3"
            )
        self._clients_cache: Dict[str, Any] = {}

    def __str__(self):
        return "cloudwatch-logs-query"

    async def __call__(self, opensearch, params):
        """
        Execute CloudWatch Logs query.

        :param opensearch: OpenSearch client (not used for CloudWatch operations)
        :param params: Operation parameters
        :return: Dictionary with query results and metadata
        """
        # Parse required parameters
        log_group_name = params.get("log-group-name")
        log_group_names = params.get("log-group-names")

        if not log_group_name and not log_group_names:
            raise exceptions.DataError(
                "Either 'log-group-name' or 'log-group-names' must be specified for cloudwatch-logs-query operation"
            )

        # Normalize to list format
        if log_group_name:
            log_groups = [parse_string_parameter("log-group-name", params)]
        else:
            log_groups = log_group_names if isinstance(log_group_names, list) else [log_group_names]

        query_string = parse_string_parameter("query-string", params)
        start_time = params.get("start-time")
        end_time = params.get("end-time")

        if not query_string:
            raise exceptions.DataError("'query-string' is required for cloudwatch-logs-query operation")
        if start_time is None:
            raise exceptions.DataError("'start-time' is required for cloudwatch-logs-query operation")
        if end_time is None:
            raise exceptions.DataError("'end-time' is required for cloudwatch-logs-query operation")

        # Parse optional parameters
        query_language = params.get("query-language", "CWLI")
        region = params.get("region", None)
        poll_interval = parse_float_parameter("poll-interval", params, 0.1)
        query_timeout = parse_float_parameter("query-timeout", params, 300.0)
        limit = parse_int_parameter("limit", params, 10000)

        # Validate parameters
        valid_languages = ["CWLI", "SQL", "PPL"]
        if query_language not in valid_languages:
            raise exceptions.DataError(
                f"'query-language' must be one of {valid_languages}, got: {query_language}"
            )
        if poll_interval <= 0:
            raise exceptions.DataError(f"'poll-interval' must be positive, got: {poll_interval}")
        if query_timeout <= 0:
            raise exceptions.DataError(f"'query-timeout' must be positive, got: {query_timeout}")
        if limit < 1 or limit > 10000:
            raise exceptions.DataError(f"'limit' must be between 1 and 10000, got: {limit}")

        # Convert start_time and end_time to Unix epoch
        start_epoch = self._parse_time(start_time)
        end_epoch = self._parse_time(end_time)

        # Get or create CloudWatch Logs client
        client = self._get_client(region)

        # Execute query
        total_start_time = time.time()

        try:
            result = await self._execute_query(
                client=client,
                log_groups=log_groups,
                query_string=query_string,
                query_language=query_language,
                start_time=start_epoch,
                end_time=end_epoch,
                limit=limit,
                poll_interval=poll_interval,
                query_timeout=query_timeout
            )

            total_duration = time.time() - total_start_time
            result["total_duration"] = total_duration

            self.logger.info(
                "CloudWatch Logs query completed: query_id=%s, status=%s, records=%d, duration=%.2fs",
                result.get("query_id"),
                result.get("status"),
                result.get("weight", 0),
                total_duration
            )

            return result

        except (ClientError, BotoCoreError) as e:
            self.logger.error("CloudWatch Logs query failed: %s", str(e))
            raise exceptions.BenchmarkError(
                f"CloudWatch Logs query failed: {str(e)}"
            ) from e

    def _get_client(self, region: Optional[str]):
        """
        Get or create a CloudWatch Logs client for the specified region.

        :param region: AWS region name (None for default)
        :return: boto3 CloudWatch Logs client
        """
        cache_key = region or "default"

        if cache_key not in self._clients_cache:
            try:
                if region:
                    self._clients_cache[cache_key] = boto3.client('logs', region_name=region)
                    self.logger.debug("Created CloudWatch Logs client for region: %s", region)
                else:
                    self._clients_cache[cache_key] = boto3.client('logs')
                    self.logger.debug("Created CloudWatch Logs client with default region")
            except Exception as e:
                raise exceptions.SystemSetupError(
                    f"Failed to create CloudWatch Logs client: {str(e)}"
                ) from e

        return self._clients_cache[cache_key]

    def _parse_time(self, time_value) -> int:
        """
        Parse time value into Unix epoch timestamp.

        Supports:
        - Unix epoch (int): 1609459200
        - Relative time strings: "-1h", "-30m", "-1d"
        - "now" keyword

        :param time_value: Time value to parse
        :return: Unix epoch timestamp (seconds)
        """
        if isinstance(time_value, int):
            return time_value

        if isinstance(time_value, str):
            if time_value == "now":
                return int(time.time())

            # Parse relative time strings like "-1h", "-30m", "-1d"
            if time_value.startswith("-"):
                import re
                match = re.match(r'^-(\d+)([smhd])$', time_value)
                if match:
                    value, unit = match.groups()
                    value = int(value)

                    # Convert to seconds
                    multipliers = {
                        's': 1,
                        'm': 60,
                        'h': 3600,
                        'd': 86400
                    }

                    offset_seconds = value * multipliers[unit]
                    return int(time.time() - offset_seconds)

        raise exceptions.DataError(
            f"Invalid time format: {time_value}. "
            "Expected Unix epoch (int), 'now', or relative time like '-1h', '-30m', '-1d'"
        )

    async def _execute_query(
        self,
        client,
        log_groups: list,
        query_string: str,
        query_language: str,
        start_time: int,
        end_time: int,
        limit: int,
        poll_interval: float,
        query_timeout: float
    ) -> Dict[str, Any]:
        """
        Execute CloudWatch Logs query with polling.

        :param client: boto3 CloudWatch Logs client
        :param log_groups: List of log group names
        :param query_string: CloudWatch Logs query string
        :param query_language: Query language (CWLI, SQL, or PPL)
        :param start_time: Start time (Unix epoch seconds)
        :param end_time: End time (Unix epoch seconds)
        :param limit: Maximum number of results
        :param poll_interval: Seconds between polling attempts
        :param query_timeout: Maximum seconds to wait for completion
        :return: Query results and metadata
        """
        # Step 1: Start the query
        self.logger.debug(
            "Starting CloudWatch Logs query: log_groups=%s, language=%s, query='%s', start=%d, end=%d",
            log_groups, query_language, query_string, start_time, end_time
        )

        query_start_time = time.time()

        start_response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.start_query(
                logGroupNames=log_groups,
                startTime=start_time,
                endTime=end_time,
                queryString=query_string,
                queryLanguage=query_language,
                limit=limit
            )
        )

        query_id = start_response['queryId']
        self.logger.debug("Query started with queryId: %s", query_id)

        # Step 2: Poll for results
        status = 'Running'
        results = None
        statistics = None
        poll_count = 0

        deadline = query_start_time + query_timeout

        while status in ['Running', 'Scheduled']:
            # Check timeout
            if time.time() >= deadline:
                raise exceptions.BenchmarkError(
                    f"CloudWatch Logs query timed out after {query_timeout} seconds. "
                    f"Query ID: {query_id}, Status: {status}"
                )

            # Wait before polling
            await asyncio.sleep(poll_interval)
            poll_count += 1

            # Get query results
            get_response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.get_query_results(queryId=query_id)
            )

            status = get_response['status']
            self.logger.debug(
                "Poll #%d: queryId=%s, status=%s", poll_count, query_id, status
            )

            if status == 'Complete':
                results = get_response.get('results', [])
                statistics = get_response.get('statistics', {})
                break
            elif status == 'Failed':
                raise exceptions.BenchmarkError(
                    f"CloudWatch Logs query failed. Query ID: {query_id}"
                )
            elif status == 'Cancelled':
                raise exceptions.BenchmarkError(
                    f"CloudWatch Logs query was cancelled. Query ID: {query_id}"
                )

        query_duration = time.time() - query_start_time

        # Extract statistics
        records_matched = statistics.get('recordsMatched', 0) if statistics else 0
        records_scanned = statistics.get('recordsScanned', 0) if statistics else 0
        bytes_scanned = statistics.get('bytesScanned', 0) if statistics else 0

        return {
            "weight": len(results) if results else 0,
            "unit": "docs",
            "query_id": query_id,
            "status": status,
            "records_matched": records_matched,
            "records_scanned": records_scanned,
            "bytes_scanned": bytes_scanned,
            "query_duration": query_duration,
            "poll_count": poll_count,
            "results": results  # Include actual log records
        }


class CloudWatchLogsStartQuery(Runner):
    """
    Starts a CloudWatch Logs query without waiting for results.

    This is useful for initiating queries that will be retrieved later
    using CloudWatchLogsGetResults runner.

    Expected parameters:
    * `log-group-name` or `log-group-names`: Log group name(s)
    * `query-string`: CloudWatch Logs query
    * `start-time`: Query start time
    * `end-time`: Query end time
    * `query-language`: Query language - "CWLI", "SQL", or "PPL" (optional, default: "CWLI")
    * `region`: AWS region (optional)
    * `limit`: Max results (optional, default: 10000)
    * `context-key`: Key to store queryId in context (optional, for later retrieval)

    Returns:
    * `weight`: 1
    * `unit`: "ops"
    * `query_id`: The CloudWatch query ID
    """

    def __init__(self):
        super().__init__()
        if not BOTO3_AVAILABLE:
            raise exceptions.SystemSetupError(
                "boto3 is required for CloudWatch Logs operations. "
                "Install it with: pip install boto3"
            )
        self._clients_cache: Dict[str, Any] = {}

    def __str__(self):
        return "cloudwatch-logs-start-query"

    async def __call__(self, opensearch, params):
        """Execute CloudWatch Logs StartQuery operation."""
        # Parse parameters (similar to CloudWatchLogsQuery)
        log_group_name = params.get("log-group-name")
        log_group_names = params.get("log-group-names")

        if not log_group_name and not log_group_names:
            raise exceptions.DataError(
                "Either 'log-group-name' or 'log-group-names' must be specified"
            )

        if log_group_name:
            log_groups = [parse_string_parameter("log-group-name", params)]
        else:
            log_groups = log_group_names if isinstance(log_group_names, list) else [log_group_names]

        query_string = parse_string_parameter("query-string", params)
        start_time = params.get("start-time")
        end_time = params.get("end-time")
        query_language = params.get("query-language", "CWLI")
        region = params.get("region", None)
        limit = parse_int_parameter("limit", params, 10000)
        context_key = params.get("context-key")

        # Validate query language
        valid_languages = ["CWLI", "SQL", "PPL"]
        if query_language not in valid_languages:
            raise exceptions.DataError(
                f"'query-language' must be one of {valid_languages}, got: {query_language}"
            )

        # Parse time values
        query_parser = CloudWatchLogsQuery()
        start_epoch = query_parser._parse_time(start_time)
        end_epoch = query_parser._parse_time(end_time)

        # Get client
        client = self._get_client(region)

        # Start query
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.start_query(
                logGroupNames=log_groups,
                startTime=start_epoch,
                endTime=end_epoch,
                queryString=query_string,
                queryLanguage=query_language,
                limit=limit
            )
        )

        query_id = response['queryId']

        self.logger.info("Started CloudWatch Logs query: %s", query_id)

        # Store in context if key provided
        if context_key:
            from osbenchmark.client import RequestContextHolder
            # Store for later retrieval
            params[f"_cloudwatch_query_{context_key}"] = query_id

        return {
            "weight": 1,
            "unit": "ops",
            "query_id": query_id
        }

    def _get_client(self, region: Optional[str]):
        """Get or create CloudWatch Logs client."""
        cache_key = region or "default"
        if cache_key not in self._clients_cache:
            if region:
                self._clients_cache[cache_key] = boto3.client('logs', region_name=region)
            else:
                self._clients_cache[cache_key] = boto3.client('logs')
        return self._clients_cache[cache_key]


class CloudWatchLogsGetResults(Runner):
    """
    Retrieves results from a previously started CloudWatch Logs query.

    Expected parameters:
    * `query-id`: CloudWatch query ID to retrieve results for
    * `region`: AWS region (optional)
    * `poll-interval`: Seconds between polling (optional, default: 2)
    * `query-timeout`: Max wait time (optional, default: 300)

    Returns:
    * `weight`: Number of log records
    * `unit`: "docs"
    * `status`: Query status
    * `records_matched`: Total matched records
    * `records_scanned`: Total scanned records
    * `bytes_scanned`: Total bytes scanned
    """

    def __init__(self):
        super().__init__()
        if not BOTO3_AVAILABLE:
            raise exceptions.SystemSetupError(
                "boto3 is required for CloudWatch Logs operations. "
                "Install it with: pip install boto3"
            )
        self._clients_cache: Dict[str, Any] = {}

    def __str__(self):
        return "cloudwatch-logs-get-results"

    async def __call__(self, opensearch, params):
        """Retrieve CloudWatch Logs query results."""
        query_id = parse_string_parameter("query-id", params)
        region = params.get("region", None)
        poll_interval = parse_float_parameter("poll-interval", params, 0.1)
        query_timeout = parse_float_parameter("query-timeout", params, 300.0)

        client = self._get_client(region)

        # Poll for results
        start_time = time.time()
        deadline = start_time + query_timeout
        status = 'Running'
        results = None
        statistics = None

        while status in ['Running', 'Scheduled']:
            if time.time() >= deadline:
                raise exceptions.BenchmarkError(
                    f"Query results retrieval timed out after {query_timeout} seconds. Query ID: {query_id}"
                )

            await asyncio.sleep(poll_interval)

            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.get_query_results(queryId=query_id)
            )

            status = response['status']

            if status == 'Complete':
                results = response.get('results', [])
                statistics = response.get('statistics', {})
                break
            elif status in ['Failed', 'Cancelled']:
                raise exceptions.BenchmarkError(
                    f"CloudWatch Logs query {status.lower()}. Query ID: {query_id}"
                )

        duration = time.time() - start_time

        return {
            "weight": len(results) if results else 0,
            "unit": "docs",
            "query_id": query_id,
            "status": status,
            "records_matched": statistics.get('recordsMatched', 0) if statistics else 0,
            "records_scanned": statistics.get('recordsScanned', 0) if statistics else 0,
            "bytes_scanned": statistics.get('bytesScanned', 0) if statistics else 0,
            "duration": duration,
            "results": results
        }

    def _get_client(self, region: Optional[str]):
        """Get or create CloudWatch Logs client."""
        cache_key = region or "default"
        if cache_key not in self._clients_cache:
            if region:
                self._clients_cache[cache_key] = boto3.client('logs', region_name=region)
            else:
                self._clients_cache[cache_key] = boto3.client('logs')
        return self._clients_cache[cache_key]
