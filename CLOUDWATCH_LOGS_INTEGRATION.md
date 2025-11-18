# CloudWatch Logs Integration for OpenSearch Benchmark

This document describes the CloudWatch Logs integration capability added to OpenSearch Benchmark, enabling you to query and analyze logs from AWS CloudWatch Logs using CloudWatch Logs Insights queries.

## Overview

The CloudWatch Logs integration adds three new operation types to OpenSearch Benchmark:

1. **`cloudwatch-logs-query`** - Complete query operation (start + poll for results)
2. **`cloudwatch-logs-start-query`** - Start a query without waiting for results
3. **`cloudwatch-logs-get-results`** - Retrieve results from a previously started query

## Prerequisites

### 1. Install boto3

```bash
pip install boto3
```

### 2. Configure AWS Credentials

CloudWatch Logs operations require valid AWS credentials with the following permissions:

- `logs:StartQuery`
- `logs:GetQueryResults`

You can configure credentials in several ways:

**Option A: Environment Variables**
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=us-east-1
```

**Option B: AWS Config Files**
```bash
# ~/.aws/credentials
[default]
aws_access_key_id = your_access_key
aws_secret_access_key = your_secret_key

# ~/.aws/config
[default]
region = us-east-1
```

**Option C: IAM Roles** (for EC2 instances)
- Attach an IAM role with CloudWatch Logs read permissions to your EC2 instance

## Operation Types

### 1. cloudwatch-logs-query

Executes a complete CloudWatch Logs Insights query by starting the query and polling for results until completion.

#### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `log-group-name` | string | Yes* | - | Single log group name |
| `log-group-names` | list | Yes* | - | List of log group names |
| `query-string` | string | Yes | - | CloudWatch Logs query string |
| `query-language` | string | No | "CWLI" | Query language: "CWLI", "SQL", or "PPL" |
| `start-time` | string/int | Yes | - | Start time (Unix epoch, relative like "-1h", or "now") |
| `end-time` | string/int | Yes | - | End time (Unix epoch, relative, or "now") |
| `region` | string | No | Default | AWS region |
| `poll-interval` | float | No | 2.0 | Seconds between result polls |
| `query-timeout` | float | No | 300.0 | Max seconds to wait for completion |
| `limit` | int | No | 10000 | Max log events to return (1-10000) |

*Either `log-group-name` or `log-group-names` must be specified

#### Query Languages

CloudWatch Logs supports three query languages:

1. **CWLI (CloudWatch Logs Insights)** - Default, pipe-based syntax
2. **SQL** - Standard SQL query syntax
3. **PPL (Piped Processing Language)** - OpenSearch Piped Processing Language

#### Time Format Examples

- Unix epoch: `1609459200`
- Relative time: `"-1h"`, `"-30m"`, `"-1d"`, `"-2h"`
- Current time: `"now"`

#### Example Operations

**Using CWLI (default):**
```json
{
  "operation-type": "cloudwatch-logs-query",
  "log-group-name": "/aws/opensearch/my-domain",
  "query-string": "fields @timestamp, @message | sort @timestamp desc | limit 20",
  "query-language": "CWLI",
  "start-time": "-1h",
  "end-time": "now",
  "region": "us-east-1"
}
```

**Using SQL:**
```json
{
  "operation-type": "cloudwatch-logs-query",
  "log-group-name": "/aws/opensearch/my-domain",
  "query-string": "SELECT @timestamp, @message FROM '/aws/opensearch/my-domain' WHERE @message LIKE '%ERROR%' ORDER BY @timestamp DESC LIMIT 20",
  "query-language": "SQL",
  "start-time": "-1h",
  "end-time": "now",
  "region": "us-east-1"
}
```

**Using PPL:**
```json
{
  "operation-type": "cloudwatch-logs-query",
  "log-group-name": "/aws/opensearch/my-domain",
  "query-string": "source='/aws/opensearch/my-domain' | where @message like '%ERROR%' | sort @timestamp desc | head 20",
  "query-language": "PPL",
  "start-time": "-1h",
  "end-time": "now",
  "region": "us-east-1"
}
```

#### Query String Examples

**CWLI (CloudWatch Logs Insights) - Default:**
```
# Basic query
fields @timestamp, @message | limit 100

# Filter logs
fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc

# Aggregate statistics
fields @timestamp | stats count() by bin(5m)

# Complex analysis
fields @timestamp, statusCode, requestId
| filter statusCode >= 400
| stats count() by statusCode, bin(1h)
```

**SQL:**
```
# Basic query
SELECT @timestamp, @message FROM '/aws/opensearch/my-domain' LIMIT 100

# Filter logs
SELECT @timestamp, @message
FROM '/aws/opensearch/my-domain'
WHERE @message LIKE '%ERROR%'
ORDER BY @timestamp DESC

# Aggregate statistics
SELECT DATE_TRUNC('minute', @timestamp) as time, COUNT(*) as count
FROM '/aws/opensearch/my-domain'
GROUP BY time
ORDER BY time

# Complex analysis
SELECT statusCode, COUNT(*) as count
FROM '/aws/opensearch/my-domain'
WHERE statusCode >= 400
GROUP BY statusCode
```

**PPL (Piped Processing Language):**
```
# Basic query
source='/aws/opensearch/my-domain' | head 100

# Filter logs
source='/aws/opensearch/my-domain'
| where @message like '%ERROR%'
| sort @timestamp desc

# Aggregate statistics
source='/aws/opensearch/my-domain'
| stats count() by span(@timestamp, 5m)

# Complex analysis
source='/aws/opensearch/my-domain'
| where statusCode >= 400
| stats count() by statusCode, span(@timestamp, 1h)
```

#### Returns

- `weight`: Number of log records returned
- `unit`: "docs"
- `query_id`: CloudWatch query ID
- `status`: Final query status ("Complete", "Failed", "Cancelled")
- `records_matched`: Total records matching the query
- `records_scanned`: Total records scanned
- `bytes_scanned`: Total bytes scanned
- `query_duration`: Time for query execution (seconds)
- `total_duration`: Total time including polling (seconds)
- `results`: Array of log records

### 2. cloudwatch-logs-start-query

Starts a CloudWatch Logs query without waiting for results. Useful for initiating long-running queries.

#### Parameters

Same as `cloudwatch-logs-query` except:
- No `poll-interval` or `query-timeout` parameters
- Optional `context-key` parameter to store query ID for later retrieval
- Supports `query-language` parameter ("CWLI", "SQL", or "PPL")

#### Examples

**CWLI:**
```json
{
  "operation-type": "cloudwatch-logs-start-query",
  "log-group-name": "/aws/lambda/my-function",
  "query-string": "fields @timestamp, @message | stats count()",
  "query-language": "CWLI",
  "start-time": "-2h",
  "end-time": "now",
  "region": "us-east-1",
  "context-key": "my-query"
}
```

**SQL:**
```json
{
  "operation-type": "cloudwatch-logs-start-query",
  "log-group-name": "/aws/lambda/my-function",
  "query-string": "SELECT @timestamp, COUNT(*) FROM '/aws/lambda/my-function' GROUP BY @timestamp",
  "query-language": "SQL",
  "start-time": "-2h",
  "end-time": "now",
  "region": "us-east-1"
}
```

#### Returns

- `weight`: 1
- `unit`: "ops"
- `query_id`: The CloudWatch query ID

### 3. cloudwatch-logs-get-results

Retrieves results from a previously started query.

#### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query-id` | string | Yes | - | CloudWatch query ID to retrieve |
| `region` | string | No | Default | AWS region |
| `poll-interval` | float | No | 2.0 | Seconds between polls |
| `query-timeout` | float | No | 300.0 | Max seconds to wait |

#### Example

```json
{
  "operation-type": "cloudwatch-logs-get-results",
  "query-id": "abc123-def456-query-id",
  "region": "us-east-1",
  "poll-interval": 2,
  "query-timeout": 300
}
```

#### Returns

Same as `cloudwatch-logs-query` except no `total_duration` field.

## Complete Workload Example

```json
{
  "description": "CloudWatch Logs benchmark workload",
  "version": 2,
  "operations": [
    {
      "name": "query-recent-errors",
      "operation-type": "cloudwatch-logs-query",
      "log-group-name": "/aws/opensearch/production-domain",
      "query-string": "fields @timestamp, @message | filter @message like /ERROR/ | stats count() by bin(5m)",
      "start-time": "-1h",
      "end-time": "now",
      "region": "us-east-1"
    },
    {
      "name": "query-slow-queries",
      "operation-type": "cloudwatch-logs-query",
      "log-group-name": "/aws/opensearch/production-domain",
      "query-string": "fields @timestamp, took, query | filter took > 1000 | sort took desc",
      "start-time": "-24h",
      "end-time": "now",
      "region": "us-east-1",
      "limit": 1000
    }
  ],
  "test_procedures": [
    {
      "name": "error-analysis",
      "description": "Analyze error patterns in CloudWatch Logs",
      "default": true,
      "schedule": [
        {
          "operation": "query-recent-errors",
          "clients": 1,
          "iterations": 10,
          "warmup-iterations": 2
        }
      ]
    },
    {
      "name": "performance-analysis",
      "description": "Identify slow queries",
      "schedule": [
        {
          "operation": "query-slow-queries",
          "clients": 2,
          "iterations": 5
        }
      ]
    }
  ]
}
```

## Running a CloudWatch Logs Workload

### Basic Execution

```bash
opensearch-benchmark execute-test \
  --pipeline=benchmark-only \
  --workload-path=/path/to/cloudwatch-logs-workload.json \
  --test-procedure=error-analysis
```

### With Custom Parameters

```bash
opensearch-benchmark execute-test \
  --pipeline=benchmark-only \
  --workload-path=/path/to/cloudwatch-logs-workload.json \
  --test-procedure=error-analysis \
  --workload-params='{"log_group":"/aws/opensearch/my-domain","region":"us-west-2"}'
```

## Metrics Collected

CloudWatch Logs operations collect the following metrics:

### Per-Operation Metrics

- **Throughput**: Queries per second
- **Latency**: Query execution time (percentiles: 50th, 90th, 99th, 100th)
- **Service Time**: Time for query to complete on AWS side
- **Records**: Number of log records returned/matched/scanned
- **Data Volume**: Bytes scanned during query execution

### Sample Output

```
|                         Metric |      Task |       Value |   Unit |
|-------------------------------:|----------:|------------:|-------:|
|            Cumulative indexing time |            |           0 |    min |
|               Total throughput |      query-recent-errors |        8.5 |  ops/s |
|              Median throughput |      query-recent-errors |        8.5 |  ops/s |
|                   50th percentile latency |      query-recent-errors |      95.2 |     ms |
|                   90th percentile latency |      query-recent-errors |     152.8 |     ms |
|                   99th percentile latency |      query-recent-errors |     189.5 |     ms |
|                  100th percentile latency |      query-recent-errors |     201.3 |     ms |
```

## Error Handling

### Common Errors

**1. Missing boto3**
```
SystemSetupError: boto3 is required for CloudWatch Logs operations. Install it with: pip install boto3
```
**Solution:** Install boto3: `pip install boto3`

**2. Invalid AWS Credentials**
```
BenchmarkError: CloudWatch Logs query failed: An error occurred (UnrecognizedClientException) when calling the StartQuery operation
```
**Solution:** Configure valid AWS credentials (see Prerequisites)

**3. Missing Permissions**
```
BenchmarkError: CloudWatch Logs query failed: An error occurred (AccessDeniedException) when calling the StartQuery operation
```
**Solution:** Ensure IAM user/role has `logs:StartQuery` and `logs:GetQueryResults` permissions

**4. Query Timeout**
```
BenchmarkError: CloudWatch Logs query timed out after 300 seconds
```
**Solution:** Increase `query-timeout` parameter or optimize your query

**5. Invalid Log Group**
```
BenchmarkError: CloudWatch Logs query failed: The specified log group does not exist
```
**Solution:** Verify log group name exists in the specified region

## Best Practices

### 1. Query Optimization

- Use specific time ranges to reduce data scanned
- Apply filters early in the query pipeline
- Limit result sets appropriately
- Use `bin()` for time-series aggregations

### 2. Performance Tuning

- **Poll Interval**: Set to 2-5 seconds for most queries
- **Query Timeout**:
  - Simple queries: 60-120 seconds
  - Complex queries: 300-600 seconds
  - Large time ranges: 600+ seconds
- **Limit**: Use the smallest limit that meets your needs

### 3. Cost Optimization

- CloudWatch Logs Insights queries are charged per GB scanned
- Use narrow time ranges when possible
- Apply filters to reduce data scanned
- Monitor `bytes_scanned` metric in results

### 4. Workload Design

- Use `warmup-iterations` to establish baseline performance
- Run queries with varying complexity to test different scenarios
- Consider multiple `clients` for concurrent query testing
- Monitor AWS CloudWatch metrics alongside benchmark results

## Troubleshooting

### Enable Debug Logging

```bash
export OSB_LOG_LEVEL=DEBUG
opensearch-benchmark execute-test --workload-path=...
```

This will show detailed information about:
- Query execution steps
- Poll attempts and responses
- AWS API calls
- Error details

### Verify AWS Configuration

```bash
aws sts get-caller-identity
aws logs describe-log-groups --region us-east-1
```

### Test Query Manually

```bash
# Start query
QUERY_ID=$(aws logs start-query \
  --log-group-name "/aws/opensearch/my-domain" \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'fields @timestamp | limit 10' \
  --region us-east-1 \
  --query 'queryId' \
  --output text)

# Get results
aws logs get-query-results \
  --query-id $QUERY_ID \
  --region us-east-1
```

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────┐
│           OpenSearch Benchmark Workload                 │
│  (cloudwatch-logs-workload.json)                        │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│         CloudWatch Logs Runner                          │
│  (cloudwatch_runner.py)                                 │
│                                                          │
│  - CloudWatchLogsQuery                                  │
│  - CloudWatchLogsStartQuery                             │
│  - CloudWatchLogsGetResults                             │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│         boto3 CloudWatch Logs Client                    │
│  - start_query()                                        │
│  - get_query_results()                                  │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│         AWS CloudWatch Logs API                         │
│  - StartQuery (returns queryId)                         │
│  - GetQueryResults (polls until complete)               │
└─────────────────────────────────────────────────────────┘
```

### Query Execution Flow

```
1. Runner receives parameters
   ↓
2. Parse and validate parameters
   ↓
3. Create/reuse boto3 client
   ↓
4. Call StartQuery API
   ├─ Returns queryId immediately
   ↓
5. Poll GetQueryResults API
   ├─ Status: Running → continue polling
   ├─ Status: Scheduled → continue polling
   ├─ Status: Complete → return results
   ├─ Status: Failed → raise error
   └─ Status: Cancelled → raise error
   ↓
6. Extract metrics and return
   ├─ weight (record count)
   ├─ statistics (scanned/matched/bytes)
   ├─ query duration
   └─ results
```

## Code Reference

### Main Files

- **Runner Implementation**: `osbenchmark/worker_coordinator/cloudwatch_runner.py`
- **Operation Types**: `osbenchmark/workload/workload.py` (lines 624-626)
- **Runner Registration**: `osbenchmark/worker_coordinator/runner.py` (lines 52-54, 78-80)
- **Example Workload**: `examples/cloudwatch-logs-workload.json`

### Key Classes

```python
# Main query runner
class CloudWatchLogsQuery(Runner):
    async def __call__(self, opensearch, params):
        # Execute complete query with polling
        ...

# Async query starter
class CloudWatchLogsStartQuery(Runner):
    async def __call__(self, opensearch, params):
        # Start query without waiting
        ...

# Results retriever
class CloudWatchLogsGetResults(Runner):
    async def __call__(self, opensearch, params):
        # Poll for query results
        ...
```

## Contributing

To extend or modify the CloudWatch Logs integration:

1. Runner implementation: `osbenchmark/worker_coordinator/cloudwatch_runner.py`
2. Tests: Create `tests/worker_coordinator/cloudwatch_runner_test.py`
3. Integration tests: Create `it/cloudwatch_logs_test.py`

## License

This integration follows the Apache-2.0 license like the rest of OpenSearch Benchmark.
