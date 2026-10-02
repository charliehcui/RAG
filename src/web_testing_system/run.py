"""Run a configured web test with the existing Agent and Runtime components."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from agent_framework import BaseChatClient

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
from web_testing_system.evaluation.runner import ExecutionRoute
from web_testing_system.evidence import EvidenceStore
from web_testing_system.findings import FindingService, ScreeningSignals
from web_testing_system.providers import (
    ProviderUsageMiddleware,
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.reproduction import ReplayPlanBuilder, ReproductionRunner
from web_testing_system.reproduction.replay import DeterministicReplay
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.computer_use import (
    ComputerUseClient,
    ComputerUseController,
    GeminiComputerUseClient,
)
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.scheduler import LocalTesterScheduler, TesterInstance
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


async def _reset_before_replay(config: RunConfig) -> None:
    hook = config.reset_hook
    if hook is None:
        return
    if hook.hook_type != "HTTP_POST":
        raise ValueError("only HTTP_POST reset hooks are supported")
    target = urljoin(str(config.target_url), hook.target)
    if urlparse(target).netloc != urlparse(str(config.target_url)).netloc:
        raise ValueError("reset hook must use the target application origin")

    def post_reset() -> None:
        with urlopen(Request(target, method="POST"), timeout=10):
            pass

    await asyncio.to_thread(post_reset)


async def run(config: RunConfig, settings: Settings, *, route: ExecutionRoute | None = None, run_id: str | None = None, main_client: BaseChatClient | None = None, tester_client_factory: Callable[[], BaseChatClient] | None = None, computer_use_client_factory: Callable[[], ComputerUseClient] | None = None, browser_manager: BrowserManager | None = None, jev_selector: JevSelector | None = None) -> Path:
    """Execute one scoped run and write its deterministic final report."""
    run_id = run_id or f"run-{uuid4().hex}"
    tester_count = route.tester_count if route is not None else config.budget.max_testers
    if not 1 <= tester_count <= 4:
        raise ValueError("tester_count must be between 1 and 4")
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
    manager = browser_manager or BrowserManager(BudgetGuard(_budget_limits(config, task_steps=config.budget.max_browser_steps_per_task, remaining=budget_config)))
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
        main_usage = ProviderUsageMiddleware(store=store, run_id=run_id, budget_id=main_budget_id, provider="gemini", model=settings.main_agent_model or "", agent="main")
        main_agent = create_main_agent(client=main_client or create_main_chat_client(settings, usage=main_usage), settings=settings, tools=main_tools)
        main_runner = MainAgentRunner(agent=main_agent, tools=main_tools, run_id=run_id, max_replans=config.budget.max_replans_per_task)
        await main_runner.create_initial_plan(config)
        if not store.list_tasks(run_id):
            raise RuntimeError("Main Agent did not create any Tasks")

        selector = jev_selector or JevSelector.openrouter(settings)
        finding_service = FindingService(store, run_id, shared_state=route is None or route.coordination == "SHARED_STATE")
        account_by_role = {account.role: account for account in config.account_references}
        for account in config.account_references:
            store.create_identity(
                identity_id=f"{run_id}-{account.identity_reference}",
                run_id=run_id,
                role=account.role,
                secret_reference=account.secret_reference,
                permissions=account.permissions,
            )

        handled_findings: set[str] = set()
        handled_replans: set[str] = set()
        while pending := [task for task in store.list_tasks(run_id) if task["status"] == "PENDING"]:
            completed = {task["task_id"] for task in store.list_tasks(run_id) if task["status"] == "COMPLETED"}
            capacity = min(tester_count, config.budget.max_parallel_browser_contexts)
            ready = [task for task in pending if task["step_budget"] > 0 and set(task["dependencies"]).issubset(completed)][:capacity]
            if not ready:
                for task in pending:
                    store.update_task_status(task_id=str(task["task_id"]), expected_status="PENDING", new_status="STOPPED")
                break
            current_run = store.get_run(run_id)
            assert current_run is not None
            instances: list[TesterInstance] = []
            for task in ready:
                task_id = str(task["task_id"])
                role = str(task["data_requirements"].get("role", config.account_references[0].role))
                selected_account = account_by_role.get(role)
                if selected_account is None:
                    store.update_task_status(task_id=task_id, expected_status="PENDING", new_status="STOPPED")
                    continue
                tester_id = f"tester-{uuid4().hex}"
                identity_id = f"{run_id}-{selected_account.identity_reference}"
                budget_id = f"budget-{uuid4().hex}"
                namespace = build_data_namespace(run_id, tester_id, task_id)
                store.create_budget(budget_id=budget_id, run_id=run_id, task_id=task_id)
                budget = BudgetGuard(_budget_limits(config, task_steps=int(task["step_budget"]), remaining=current_run["remaining_budget"]))
                checker = PermissionChecker(store=store, policy=ExecutionPolicy(allowed_url_prefixes=tuple(urljoin(str(config.target_url), scope) for scope in config.allowed_scope), denied_operations=frozenset(config.denied_operations)), run_id=run_id, task_id=task_id, tester_id=tester_id)
                reader = PageStateReader()
                executor = PlaywrightExecutor(store=store, permission_checker=checker, run_id=run_id, task_id=task_id, tester_id=tester_id)
                computer_controller = None
                if (route is None or route.visual_fallback == "COMPUTER_USE") and config.budget.max_computer_use_calls > 0 and settings.computer_use_model and (tester_client_factory is None or computer_use_client_factory is not None):
                    evidence = EvidenceStore(store=store, artifacts_root=settings.artifacts_dir, temporary_sensitive_root=settings.temporary_sensitive_dir)
                    computer_client = computer_use_client_factory() if computer_use_client_factory is not None else GeminiComputerUseClient(settings)
                    computer_controller = ComputerUseController(client=computer_client, settings=settings, store=store, evidence_store=evidence, page_state_reader=reader, permission_checker=checker, budget=budget, budget_id=budget_id, run_id=run_id, task_id=task_id, tester_id=tester_id)
                runtime = WebTestingRuntime(store=store, browser_manager=manager, executor=executor, page_state_reader=reader, candidate_builder=CandidateBuilder(checker), jev_selector=selector, budget=budget, run_id=run_id, task_id=task_id, tester_id=tester_id, identity_id=identity_id, budget_id=budget_id, computer_use_controller=computer_controller)
                assignment = TesterAssignment(run_id=run_id, task_id=task_id, tester_id=tester_id, identity_id=identity_id, role=role, data_namespace=namespace, scope=tuple(config.allowed_scope), step_budget=int(task["step_budget"]))
                tools = TesterAgentTools(assignment=assignment, runtime=runtime, store=store, shared_state=route is None or route.coordination == "SHARED_STATE", decision_policy=route.candidate_selection if route else "JEV", action_policy=route.known_action if route else "PLAYWRIGHT")
                usage = ProviderUsageMiddleware(store=store, run_id=run_id, budget_id=budget_id, provider=settings.tester_agent_provider, model=settings.tester_agent_model or "", agent="tester", task_id=task_id, tester_id=tester_id, budget=budget)
                client = tester_client_factory() if tester_client_factory is not None else create_tester_chat_client(settings, usage=usage)
                tester = TesterRunner(agent=create_tester_agent(client=client, assignment=assignment, tools=tools), assignment=assignment, runtime=runtime, store=store, budget=budget, budget_id=budget_id, provider_usage_recorded=tester_client_factory is None, decision_policy=route.candidate_selection if route else "JEV", action_policy=route.known_action if route else "PLAYWRIGHT")
                store.register_tester(tester_id=tester_id, run_id=run_id, session_reference=tester.session.session_id, identity_id=identity_id, role=role, data_namespace=namespace)
                instances.append(TesterInstance(tester_id=tester_id, runner=tester))
            if not instances:
                continue
            scheduler = LocalTesterScheduler(store=store, run_id=run_id, testers=instances, max_testers=tester_count, max_browser_contexts=config.budget.max_parallel_browser_contexts)

            async def execute_task(tester: TesterRunner, task: Mapping[str, Any]) -> str:
                navigation = await tester.execute_known(WebAction(action_type=ActionType.NAVIGATION, url=str(config.target_url)))
                if not navigation["success"]:
                    raise RuntimeError("Initial navigation failed")
                answer = await tester.ask_tester_llm(f"Execute assigned Task {task['task_id']}: {task['goal']}. Role: {tester.assignment.role}. Use the provided tools and record observations or findings in Shared State. Do not declare a confirmed bug.")
                return "STOPPED" if answer.startswith("STOPPED:") else "COMPLETED"

            results = await scheduler.run_ready_tasks(execute_task)
            if not results:
                for task in pending:
                    current = store.get_task(str(task["task_id"]))
                    if current is not None and current["status"] == "PENDING":
                        store.update_task_status(task_id=str(task["task_id"]), expected_status="PENDING", new_status="STOPPED")
                break
            new_findings: list[dict[str, Any]] = []
            for finding in store.list_recent_findings(run_id, limit=1_000):
                finding_id = str(finding["finding_id"])
                if finding_id in handled_findings:
                    continue
                handled_findings.add(finding_id)
                if finding["status"] == "OBSERVATION":
                    finding = finding_service.screen(finding_id, ScreeningSignals())
                new_findings.append(finding)
            requests = [event for event in store.list_events(run_id) if event["event_type"] == "REQUEST_REPLAN" and event["event_id"] not in handled_replans]
            handled_replans.update(str(event["event_id"]) for event in requests)
            shared_coordination = route is None or route.coordination == "SHARED_STATE"
            if requests and shared_coordination:
                await main_runner.replan(str(requests[-1]["result"].get("reason", "TESTER_REQUEST")))
            elif shared_coordination:
                for finding in new_findings:
                    try:
                        main_tools._require_high_risk_finding(str(finding["finding_id"]))
                    except ValueError:
                        continue
                    await main_runner.replan(f"HIGH_RISK_FINDING:{finding['finding_id']}")
                    break
            for finding in new_findings:
                finding_id = str(finding["finding_id"])
                current = store.get_finding(finding_id)
                assert current is not None
                if current["status"] not in {"OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"}:
                    continue
                auto_reproduction = route is None or route.finding_after_anomaly == "REPRODUCTION"
                owner = next((instance.runner for instance in instances if instance.tester_id == current["first_seen_by"]), None)
                if auto_reproduction and owner is not None and current["status"] in {"ANOMALY", "SUSPECTED_ISSUE"}:
                    input_values = {key: value for key, value in config.test_data.items() if isinstance(value, str)}
                    plan_result = ReplayPlanBuilder(store).from_finding(finding_id=finding_id, input_values=input_values)
                    if plan_result.plan is None:
                        plan_result = ReplayPlanBuilder(store).from_action_history(run_id=run_id, task_id=str(current["task_id"]), input_values=input_values)
                    if plan_result.plan is not None:
                        evidence = EvidenceStore(store=store, artifacts_root=settings.artifacts_dir, temporary_sensitive_root=settings.temporary_sensitive_dir)
                        replay = DeterministicReplay(store=store, browser_manager=manager, executor=owner.runtime.executor, evidence_store=evidence, budget=owner.budget, budget_id=owner.budget_id)
                        reset_hook = (lambda: _reset_before_replay(config)) if config.reset_hook is not None else None
                        await ReproductionRunner(store=store, finding_service=finding_service, replay=replay).run(finding_id=finding_id, plan=plan_result.plan, max_attempts=config.budget.max_reproductions_per_finding, stable_successes=min(2, config.budget.max_reproductions_per_finding), reset_hook=reset_hook)
                        continue
                store.update_finding_status(finding_id=finding_id, status="NEEDS_CONFIRMATION" if auto_reproduction else "SUSPECTED_ISSUE", screening_reason=str(current["screening_reason"] or "REPRODUCTION_NOT_RUN"), needs_confirmation=True)
    except Exception as error:
        failure = error
    finally:
        await manager.close()
        tasks = store.list_tasks(run_id)
        status = "FAILED" if failure is not None or any(task["status"] == "FAILED" for task in tasks) else "STOPPED" if any(task["status"] == "STOPPED" for task in tasks) else "COMPLETED"
        store.finish_run(run_id=run_id, status=status, stop_reason=type(failure).__name__ if failure is not None else None)
        report_path = settings.artifacts_dir / run_id / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = FinalReportBuilder(store).build(run_id, full_evaluation=route.full_evaluation if route is not None else False)
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
