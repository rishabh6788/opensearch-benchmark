# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.

"""
Simplified executor for CloudWatch operations that bypasses OpenSearch-specific request tracking.
"""

import asyncio
import logging
import time
from typing import Dict, Any

from osbenchmark import exceptions, metrics


class CloudWatchExecutor:
    """
    Simplified executor for CloudWatch operations that doesn't require OpenSearch client context management.
    """

    def __init__(self, client_id, task, schedule, sampler, cancel, complete, on_error):
        self.client_id = client_id
        self.task = task
        self.schedule_handle = schedule
        self.sampler = sampler
        self.cancel = cancel
        self.complete = complete
        self.on_error = on_error
        self.logger = logging.getLogger(__name__)

    async def __call__(self):
        total_start = time.perf_counter()
        
        self.logger.debug("Initializing CloudWatch schedule for client id [%s].", self.client_id)
        schedule = self.schedule_handle()
        self.schedule_handle.start()
        
        # Handle ramp-up
        rampup_wait_time = self.schedule_handle.ramp_up_wait_time
        if rampup_wait_time:
            self.logger.info("Client id [%s] waiting [%.2f]s for ramp-up.", self.client_id, rampup_wait_time)
            await asyncio.sleep(rampup_wait_time)
            self.logger.info("Client id [%s] is running now.", self.client_id)

        self.logger.debug("Entering main loop for client id [%s].", self.client_id)
        
        try:
            async for expected_scheduled_time, sample_type, percent_completed, runner, params in schedule:
                if self.cancel.is_set():
                    self.logger.info("User cancelled execution.")
                    break

                # Simple timing control
                if expected_scheduled_time > 0:
                    absolute_expected_schedule_time = total_start + expected_scheduled_time
                    rest = absolute_expected_schedule_time - time.perf_counter()
                    if rest > 0:
                        await asyncio.sleep(rest)

                # Execute the CloudWatch operation
                absolute_processing_start = time.time()
                processing_start = time.perf_counter()
                self.schedule_handle.before_request(processing_start)

                try:
                    # CloudWatch runners don't need OpenSearch client - pass None
                    async with runner:
                        result = await runner(None, params)
                    
                    # Handle different return formats
                    if isinstance(result, dict):
                        total_ops = result.pop("weight", 1)
                        total_ops_unit = result.pop("unit", "ops")
                        request_meta_data = result
                        if "success" not in request_meta_data:
                            request_meta_data["success"] = True
                    elif isinstance(result, tuple) and len(result) == 2:
                        total_ops, total_ops_unit = result
                        request_meta_data = {"success": True}
                    else:
                        total_ops = 1
                        total_ops_unit = "ops"
                        request_meta_data = {"success": True}

                except Exception as e:
                    self.logger.exception("CloudWatch operation failed")
                    total_ops = 0
                    total_ops_unit = "ops"
                    request_meta_data = {
                        "success": False,
                        "error-type": "cloudwatch",
                        "error-description": str(e)
                    }
                    
                    if self.on_error == "abort":
                        raise exceptions.BenchmarkError(f"CloudWatch operation failed: {e}") from e

                processing_end = time.perf_counter()
                
                # Simple timing calculations
                service_time = processing_end - processing_start
                time_period = processing_end - total_start
                
                self.schedule_handle.after_request(processing_end, total_ops, total_ops_unit, request_meta_data)

                # Add sample with simplified timing
                self.sampler.add(
                    self.task, self.client_id, sample_type, request_meta_data,
                    absolute_processing_start, processing_start,
                    service_time,  # latency = service_time for CloudWatch
                    service_time,  # service_time
                    0,  # client_processing_time (not applicable)
                    service_time,  # processing_time
                    None,  # throughput (let calculator handle it)
                    total_ops, total_ops_unit, time_period, percent_completed
                )

                # Check completion
                runner_completed = getattr(runner, "completed", False)
                if self.complete.is_set() or runner_completed:
                    self.logger.info("Task [%s] is considered completed.", self.task)
                    break

        except BaseException as e:
            self.logger.exception("Could not execute CloudWatch schedule")
            raise exceptions.BenchmarkError(f"Cannot run CloudWatch task [{self.task}]: {e}") from None


class CloudWatchIoAdapter:
    """
    Simplified IO adapter for CloudWatch operations that doesn't need OpenSearch clients.
    """

    def __init__(self, cfg, workload, task_allocations, sampler, cancel, complete, abort_on_error):
        self.cfg = cfg
        self.workload = workload
        self.task_allocations = task_allocations
        self.sampler = sampler
        self.cancel = cancel
        self.complete = complete
        self.abort_on_error = abort_on_error
        self.logger = logging.getLogger(__name__)

    def __call__(self, *args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run())
        finally:
            loop.close()

    async def run(self):
        # Create CloudWatch executors (no OpenSearch clients needed)
        from osbenchmark.worker_coordinator.worker_coordinator import schedule_for
        from osbenchmark.workload import operation_parameters
        
        params_per_task = {}
        aws = []
        
        for client_id, task_allocation in self.task_allocations:
            task = task_allocation.task
            if task not in params_per_task:
                param_source = operation_parameters(self.workload, task)
                params_per_task[task] = param_source
            
            schedule = schedule_for(task_allocation, params_per_task[task])
            
            executor = CloudWatchExecutor(
                client_id, task, schedule, self.sampler, self.cancel, self.complete,
                task.error_behavior(self.abort_on_error)
            )
            aws.append(executor())

        run_start = time.perf_counter()
        try:
            await asyncio.gather(*aws)
        finally:
            run_end = time.perf_counter()
            self.logger.info("Total CloudWatch run duration: %f seconds.", (run_end - run_start))
