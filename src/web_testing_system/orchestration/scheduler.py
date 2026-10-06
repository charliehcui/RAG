"""Small local AsyncIO scheduler for identical Tester Agent instances."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from web_testing_system.agents import TesterRunner
from web_testing_system.state import StateConflictError, StateStore

TaskExecutor = Callable[[TesterRunner, Mapping[str, Any]], Awaitable[str]]
FINAL_TASK_STATUSES = {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"}


@dataclass(frozen=True)
class TesterInstance:
    tester_id: str
    runner: TesterRunner


@dataclass(frozen=True)
class ScheduleResult:
    task_id: str
    tester_id: str
    status: str
    browser_session_id: str | None
    error: str | None = None


class LocalTesterScheduler:
    """Continuously fill available local slots with dependency-ready Tasks."""

    def __init__(
        self,
        *,
        store: StateStore,
        run_id: str,
        testers: Sequence[TesterInstance] = (),
        max_testers: int,
        max_browser_contexts: int,
        tester_factory: Callable[[Mapping[str, Any]], TesterInstance] | None = None,
        on_task_finished: Callable[[ScheduleResult], Awaitable[None]] | None = None,
    ) -> None:
        if tester_factory is None and not 1 <= len(testers) <= 4:
            raise ValueError("the local Tester pool must contain 1 to 4 instances")
        if not 1 <= max_testers <= 4 or not 1 <= max_browser_contexts <= 4:
            raise ValueError("Tester and Browser Context limits must be within the supported range")
        tester_ids = [tester.tester_id for tester in testers]
        session_ids = [tester.runner.session.session_id for tester in testers]
        if len(set(tester_ids)) != len(tester_ids):
            raise ValueError("Tester IDs must be unique")
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("each Tester must have an independent MAF Session")
        if any(
            tester.tester_id != tester.runner.assignment.tester_id
            or tester.runner.assignment.run_id != run_id
            for tester in testers
        ):
            raise ValueError("Tester instances must match their Runner assignments")
        self.store = store
        self.run_id = run_id
        self.testers = list(testers)
        self.tester_factory = tester_factory
        self.on_task_finished = on_task_finished
        self.capacity = min(max_testers, max_browser_contexts, len(testers) if tester_factory is None else max_testers)

    async def run_ready_tasks(self, execute_task: TaskExecutor) -> list[ScheduleResult]:
        """Refill a freed slot immediately, without waiting for its slower siblings."""
        active: set[asyncio.Task[ScheduleResult]] = set()
        results: list[ScheduleResult] = []
        try:
            while True:
                for instance, task in self._claim_ready_assignments(limit=self.capacity - len(active)):
                    active.add(asyncio.create_task(self._run_assignment(instance, task, execute_task)))
                if not active:
                    return results
                finished, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for future in finished:
                    result = future.result()
                    results.append(result)
                    if self.on_task_finished is not None:
                        await self.on_task_finished(result)
        finally:
            for future in active:
                future.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    def _claim_ready_assignments(
        self, *, limit: int | None = None,
    ) -> list[tuple[TesterInstance, dict[str, Any]]]:
        limit = self.capacity if limit is None else limit
        if limit <= 0:
            return []
        run = self.store.get_run(self.run_id)
        if run is None:
            raise KeyError(f"unknown run: {self.run_id}")
        remaining_budget = run["remaining_budget"]
        if any(
            remaining_budget.get(key) == 0
            for key in (
                "browser_steps",
                "max_runtime_seconds",
            )
        ):
            return []
        tasks = self.store.list_tasks(self.run_id)
        completed_task_ids = {
            task["task_id"] for task in tasks if task["status"] == "COMPLETED"
        }
        ready_tasks = [
            task
            for task in tasks
            if task["status"] == "PENDING"
            and task["step_budget"] > 0
            and set(task["dependencies"]).issubset(completed_task_ids)
        ]
        available_instances = [
            instance
            for instance in self.testers
            if (tester := self.store.get_tester(instance.tester_id)) is not None
            and tester["run_id"] == self.run_id
            and tester["status"] == "READY"
        ]
        assignments: list[tuple[TesterInstance, dict[str, Any]]] = []
        for task in ready_tasks:
            if len(assignments) >= limit:
                break
            instance = next(
                (
                    candidate
                    for candidate in available_instances
                    if candidate.runner.assignment.task_id == task["task_id"]
                    and self._tester_can_run(candidate.tester_id, task)
                ),
                None,
            )
            if instance is None:
                if self.tester_factory is None or task["assigned_tester"] is not None:
                    continue
                instance = self.tester_factory(task)
                self.testers.append(instance)
                available_instances.append(instance)
                if not self._tester_can_run(instance.tester_id, task):
                    continue
            try:
                claimed = self.store.claim_task(
                    task_id=str(task["task_id"]), tester_id=instance.tester_id
                )
                self.store.update_tester_status(
                    tester_id=instance.tester_id,
                    expected_status="READY",
                    new_status="BUSY",
                )
            except StateConflictError:
                continue
            tester = self.store.get_tester(instance.tester_id)
            assert tester is not None
            self._append_event(
                task_id=str(task["task_id"]),
                tester_id=instance.tester_id,
                event_type="TASK_ASSIGNED",
                action="assign_task",
                result={
                    "identity_id": tester["identity_id"],
                    "data_namespace": tester["data_namespace"],
                    "step_budget": claimed["step_budget"],
                },
            )
            assignments.append((instance, claimed))
            available_instances.remove(instance)
        return assignments

    def _tester_can_run(self, tester_id: str, task: Mapping[str, Any]) -> bool:
        tester = self.store.get_tester(tester_id)
        if tester is None:
            return False
        if task["assigned_tester"] not in {None, tester_id}:
            return False
        requirements = task["data_requirements"]
        required_role = requirements.get("role")
        if required_role is not None and tester["role"] != required_role:
            return False
        resource_id = requirements.get("resource_id")
        if resource_id is None:
            return True
        resource = self.store.get_resource(str(resource_id))
        operation = str(requirements.get("resource_operation", "read"))
        allowed = False
        if resource is not None:
            allowed = operation in resource["allowed_operations"] or "*" in resource["allowed_operations"]
            if allowed and resource["sharing_mode"] == "ISOLATED":
                allowed = resource["owner"] == tester_id and resource["owner_task"] == task["task_id"]
            if allowed and resource["sharing_mode"] == "SHARED":
                allowed = tester_id in resource["participants"]
        if not allowed:
            self._append_event(
                task_id=str(task["task_id"]),
                tester_id=tester_id,
                event_type="RESOURCE_CONFLICT",
                action="schedule_task",
                result={"resource_id": resource_id, "operation": operation},
            )
        return bool(allowed)

    async def _run_assignment(
        self,
        instance: TesterInstance,
        task: Mapping[str, Any],
        execute_task: TaskExecutor,
    ) -> ScheduleResult:
        browser_session_id: str | None = None
        error: str | None = None
        requested_status = "FAILED"
        try:
            browser_session_id = await instance.runner.runtime.start_session()
            self._append_event(
                task_id=str(task["task_id"]),
                tester_id=instance.tester_id,
                event_type="TASK_STARTED",
                action="start_task",
                result={"browser_session_id": browser_session_id},
                browser_session_id=browser_session_id,
            )
            requested_status = await execute_task(instance.runner, task)
            if requested_status not in FINAL_TASK_STATUSES:
                raise ValueError(f"unsupported final Task status: {requested_status}")
        except Exception as caught_error:
            error = str(caught_error)
            requested_status = "FAILED"
        finally:
            current = self.store.get_task(str(task["task_id"]))
            if current is not None and current["status"] == "RUNNING":
                self.store.update_task_status(
                    task_id=str(task["task_id"]),
                    expected_status="RUNNING",
                    new_status=requested_status,
                )
            if instance.runner.runtime.browser_session_id is not None:
                await instance.runner.runtime.browser_manager.close_session(
                    instance.runner.runtime.browser_session_id
                )
                instance.runner.runtime.browser_session_id = None
            tester = self.store.get_tester(instance.tester_id)
            if tester is not None and tester["status"] == "BUSY":
                self.store.update_tester_status(
                    tester_id=instance.tester_id,
                    expected_status="BUSY",
                    new_status="READY",
                )
        final_task = self.store.get_task(str(task["task_id"]))
        assert final_task is not None
        event_type = "TESTER_FAILED" if error is not None else "TASK_FINISHED"
        self._append_event(
            task_id=str(task["task_id"]),
            tester_id=instance.tester_id,
            event_type=event_type,
            action="finish_task",
            result={"status": final_task["status"], "error": error},
            browser_session_id=browser_session_id,
        )
        return ScheduleResult(
            task_id=str(task["task_id"]),
            tester_id=instance.tester_id,
            status=str(final_task["status"]),
            browser_session_id=browser_session_id,
            error=error,
        )

    def _append_event(
        self,
        *,
        task_id: str,
        tester_id: str,
        event_type: str,
        action: str,
        result: Mapping[str, Any],
        browser_session_id: str | None = None,
    ) -> None:
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=task_id,
            tester_id=tester_id,
            browser_session_id=browser_session_id,
            event_type=event_type,
            tool="LocalTesterScheduler",
            action=action,
            result=result,
            latency_ms=0,
        )
