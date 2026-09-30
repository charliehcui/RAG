"""Run a configured web test with the existing Agent and Runtime components."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

from web_testing_system.agents import (
    MainAgentRunner,
    MainAgentTools,
    TesterAgentTools,
    TesterAssignment,
    TesterRunner,
    create_main_agent,
    create_tester_agent,
)
from web_testing_system.config import RunConfig, Settings, resolve_application_version
from web_testing_system.findings import FindingService, ScreeningSignals
from web_testing_system.providers import (
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore, build_data_namespace


def _budget_limits(config: RunConfig, *, task_steps: int, remaining: dict[str, Any]) -> BudgetLimits:
    budget = config.budget
    return BudgetLimits(
        max_runtime_seconds=float(remaining["max_runtime_seconds"]),
        max_llm_calls=int(remaining["max_llm_calls"]),
        max_input_tokens=int(remaining["max_input_tokens"]),
        max_output_tokens=int(remaining["max_output_tokens"]),
        max_jev_calls=int(remaining["max_jev_calls"]),
        max_computer_use_calls=int(remaining["max_computer_use_calls"]),
        max_task_steps=min(task_steps, budget.max_browser_steps_per_task),
        max_task_replans=budget.max_replans_per_task,
        max_browser_contexts=budget.max_parallel_browser_contexts,
    )


async def run(config: RunConfig, settings: Settings) -> Path:
    """Execute one scoped run and write its deterministic final report."""
    run_id = f"run-{uuid4().hex}"
    store = StateStore(settings.state_db_path)
    store.initialize()
    budget_config = config.budget.model_dump()
    store.create_run(
        run_id=run_id,
        application=str(config.target_url),
        application_version=resolve_application_version(config.application_version, None, None),
        test_goal=config.test_goal,
        scope={"focus_features": config.focus_features, "allowed_scope": config.allowed_scope},
        status="RUNNING",
        global_budget=budget_config,
        remaining_budget=budget_config,
    )
    main_budget_id = f"budget-{uuid4().hex}"
    store.create_budget(budget_id=main_budget_id, run_id=run_id)
    manager = BrowserManager(BudgetGuard(_budget_limits(config, task_steps=config.budget.max_browser_steps_per_task, remaining=budget_config)))
    failure: Exception | None = None
    try:
        main_tools = MainAgentTools(
            store=store,
            run_id=run_id,
            target_url=str(config.target_url),
            focus_features=config.focus_features,
            allowed_scope=config.allowed_scope,
            denied_operations=config.denied_operations,
            max_step_budget=config.budget.max_browser_steps_per_task,
        )
        main_agent = create_main_agent(client=create_main_chat_client(settings), settings=settings, tools=main_tools)
        main_runner = MainAgentRunner(agent=main_agent, tools=main_tools, run_id=run_id, max_replans=config.budget.max_replans_per_task)
        main_started_at = perf_counter()
        main_response = await main_runner.create_initial_plan(config)
        main_latency_seconds = perf_counter() - main_started_at
        main_usage = main_response.usage_details or {}
        main_input_tokens = int(main_usage.get("input_token_count") or 0)
        main_output_tokens = int(main_usage.get("output_token_count") or 0)
        store.update_budget(
            budget_id=main_budget_id,
            llm_calls=1,
            input_tokens=main_input_tokens,
            output_tokens=main_output_tokens,
            runtime_seconds=main_latency_seconds,
        )
        store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=run_id,
            event_type="LLM_CALL",
            tool="Microsoft Agent Framework",
            action="create_initial_plan",
            result={"agent": "main", "input_tokens": main_input_tokens, "output_tokens": main_output_tokens},
            latency_ms=main_latency_seconds * 1_000,
        )
        if not store.list_tasks(run_id):
            raise RuntimeError("Main Agent did not create any Tasks")

        selector = JevSelector.openrouter(settings)
        finding_service = FindingService(store, run_id)
        account_by_role = {account.role: account for account in config.account_references}
        for account in config.account_references:
            store.create_identity(
                identity_id=f"{run_id}-{account.identity_reference}",
                run_id=run_id,
                role=account.role,
                secret_reference=account.secret_reference,
                permissions=account.permissions,
            )

        while pending := [task for task in store.list_tasks(run_id) if task["status"] == "PENDING"]:
            completed = {task["task_id"] for task in store.list_tasks(run_id) if task["status"] == "COMPLETED"}
            ready = next((task for task in pending if set(task["dependencies"]).issubset(completed)), None)
            if ready is None:
                for task in pending:
                    store.update_task_status(task_id=str(task["task_id"]), expected_status="PENDING", new_status="STOPPED")
                break
            task_id = str(ready["task_id"])
            role = str(ready["data_requirements"].get("role", config.account_references[0].role))
            selected_account = account_by_role.get(role)
            if selected_account is None:
                store.update_task_status(task_id=task_id, expected_status="PENDING", new_status="STOPPED")
                continue
            tester_id = f"tester-{uuid4().hex}"
            identity_id = f"{run_id}-{selected_account.identity_reference}"
            budget_id = f"budget-{uuid4().hex}"
            namespace = build_data_namespace(run_id, tester_id, task_id)
            store.register_tester(tester_id=tester_id, run_id=run_id, session_reference=tester_id, identity_id=identity_id, role=role, data_namespace=namespace)
            store.create_budget(budget_id=budget_id, run_id=run_id, task_id=task_id)
            remaining = store.get_run(run_id)
            assert remaining is not None
            budget = BudgetGuard(_budget_limits(config, task_steps=int(ready["step_budget"]), remaining=remaining["remaining_budget"]))
            checker = PermissionChecker(
                store=store,
                policy=ExecutionPolicy(
                    allowed_url_prefixes=tuple(urljoin(str(config.target_url), scope) for scope in config.allowed_scope),
                    denied_operations=frozenset(config.denied_operations),
                ),
                run_id=run_id,
                task_id=task_id,
                tester_id=tester_id,
            )
            runtime = WebTestingRuntime(
                store=store,
                browser_manager=manager,
                executor=PlaywrightExecutor(store=store, permission_checker=checker, run_id=run_id, task_id=task_id, tester_id=tester_id),
                page_state_reader=PageStateReader(),
                candidate_builder=CandidateBuilder(checker),
                jev_selector=selector,
                budget=budget,
                run_id=run_id,
                task_id=task_id,
                tester_id=tester_id,
                identity_id=identity_id,
                budget_id=budget_id,
            )
            assignment = TesterAssignment(
                run_id=run_id,
                task_id=task_id,
                tester_id=tester_id,
                identity_id=identity_id,
                role=role,
                data_namespace=namespace,
                scope=tuple(config.allowed_scope),
                step_budget=int(ready["step_budget"]),
            )
            tools = TesterAgentTools(assignment=assignment, runtime=runtime, store=store)
            tester = TesterRunner(
                agent=create_tester_agent(client=create_tester_chat_client(settings), assignment=assignment, tools=tools),
                assignment=assignment,
                runtime=runtime,
                store=store,
                budget=budget,
                budget_id=budget_id,
            )
            store.claim_task(task_id=task_id, tester_id=tester_id)
            try:
                await runtime.start_session()
                navigation = await tester.execute_known(WebAction(action_type=ActionType.NAVIGATION, url=str(config.target_url)))
                if not navigation["success"]:
                    raise RuntimeError("Initial navigation failed")
                answer = await tester.ask_tester_llm(
                    f"Execute assigned Task {task_id}: {ready['goal']}. Role: {role}. "
                    "Use the provided tools and record observations or findings in Shared State. "
                    "Do not declare a confirmed bug."
                )
                for finding in store.list_recent_findings(run_id, limit=1_000):
                    if finding["task_id"] != task_id:
                        continue
                    if finding["status"] == "OBSERVATION":
                        finding = finding_service.screen(str(finding["finding_id"]), ScreeningSignals())
                    if finding["status"] in {"OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"}:
                        store.update_finding_status(
                            finding_id=str(finding["finding_id"]),
                            status="NEEDS_CONFIRMATION",
                            screening_reason=str(finding["screening_reason"] or "REPRODUCTION_NOT_RUN"),
                            needs_confirmation=True,
                        )
                current_task = store.get_task(task_id)
                if current_task is not None and current_task["status"] == "RUNNING":
                    status = "STOPPED" if answer.startswith("STOPPED:") else "COMPLETED"
                    store.update_task_status(task_id=task_id, expected_status="RUNNING", new_status=status)
            except Exception:
                current_task = store.get_task(task_id)
                if current_task is not None and current_task["status"] == "RUNNING":
                    store.update_task_status(task_id=task_id, expected_status="RUNNING", new_status="FAILED")
                raise
            finally:
                if runtime.browser_session_id is not None:
                    await manager.close_session(runtime.browser_session_id)
    except Exception as error:
        failure = error
    finally:
        await manager.close()
        tasks = store.list_tasks(run_id)
        status = "FAILED" if failure is not None or any(task["status"] == "FAILED" for task in tasks) else "STOPPED" if any(task["status"] == "STOPPED" for task in tasks) else "COMPLETED"
        store.finish_run(run_id=run_id, status=status, stop_reason=type(failure).__name__ if failure is not None else None)
        report_path = settings.artifacts_dir / run_id / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = FinalReportBuilder(store).build(run_id, full_evaluation=False)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if failure is not None:
        raise failure
    return report_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run one configured web test")
    parser.add_argument("config", type=Path, help="Path to RunConfig JSON")
    arguments = parser.parse_args()
    config = RunConfig.model_validate_json(arguments.config.read_text(encoding="utf-8"))
    print(asyncio.run(run(config, Settings())))
