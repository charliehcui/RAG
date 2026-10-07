"""Run a configured web test with the existing Agent and Runtime components."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
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
from web_testing_system.observability import trace_result, trace_run, trace_span
from web_testing_system.orchestration.scheduler import (
    LocalTesterScheduler,
    ScheduleResult,
    TesterInstance,
)
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
    OpenRouterComputerUseClient,
)
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.scoring import score_run_tasks
from web_testing_system.state import StateStore, build_data_namespace
from web_testing_system.verification import VerificationRunner


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


async def run(config: RunConfig, settings: Settings, *, route: ExecutionRoute | None = None, run_id: str | None = None, scenario_id: str | None = None, main_client: BaseChatClient | None = None, tester_client_factory: Callable[[], BaseChatClient] | None = None, computer_use_client_factory: Callable[[], ComputerUseClient] | None = None, browser_manager: BrowserManager | None = None, jev_selector: JevSelector | None = None) -> Path:
    """Run Manager planning, concurrent Workers, deterministic checks and final review."""
    run_id = run_id or f"run-{uuid4().hex}"
    sensitive_values: list[str] = []
    pending_values: list[Any] = [config.test_data]
    while pending_values:
        value = pending_values.pop()
        if isinstance(value, Mapping):
            pending_values.extend(value.values())
        elif isinstance(value, list):
            pending_values.extend(value)
        elif isinstance(value, str) and value:
            sensitive_values.append(value)
    for account in config.account_references:
        reference = account.secret_reference
        if reference is not None and reference.startswith("env:"):
            secret = os.environ.get(reference[4:])
            if secret:
                sensitive_values.append(secret)
    async with trace_run(settings, run_id, scenario_id=scenario_id, secrets=sensitive_values):
        return await _run(config, settings, route=route, run_id=run_id, main_client=main_client, tester_client_factory=tester_client_factory, computer_use_client_factory=computer_use_client_factory, browser_manager=browser_manager, jev_selector=jev_selector)


async def _run(config: RunConfig, settings: Settings, *, route: ExecutionRoute | None, run_id: str, main_client: BaseChatClient | None, tester_client_factory: Callable[[], BaseChatClient] | None, computer_use_client_factory: Callable[[], ComputerUseClient] | None, browser_manager: BrowserManager | None, jev_selector: JevSelector | None) -> Path:
    tester_count = route.tester_count if route is not None else config.budget.max_testers
    if not 1 <= tester_count <= 4:
        raise ValueError("tester_count must be between 1 and 4")
    store = StateStore(settings.state_db_path)
    store.initialize()
    budget_config = config.budget.model_dump()
    model_configuration = {"transport": "openrouter", "main_model": settings.main_agent_model, "main_provider": settings.main_agent_provider, "tester_model": settings.tester_agent_model, "tester_provider": settings.tester_agent_provider, "tester_backup_model": settings.tester_agent_backup_model, "tester_backup_provider": settings.tester_agent_backup_provider, "jev_model": settings.jev_model, "automatic_fallback": "TESTER_API_FAILURE_ONLY"}
    store.create_run(run_id=run_id, application=str(config.target_url), application_version=resolve_application_version(config.application_version, None, None), test_goal=config.test_goal, scope={"focus_features": config.focus_features, "allowed_scope": config.allowed_scope, "configured_tester_capacity": min(tester_count, config.budget.max_parallel_browser_contexts), "model_configuration": model_configuration, "required_checks": [check.model_dump(mode="json") for check in config.required_checks], "evaluation_version": config.evaluation_version}, status="RUNNING", global_budget=budget_config, remaining_budget=budget_config)
    main_budget_id = f"budget-{uuid4().hex}"
    store.create_budget(budget_id=main_budget_id, run_id=run_id)
    manager = browser_manager or BrowserManager(BudgetGuard(_budget_limits(config, task_steps=config.budget.max_browser_steps_per_task, remaining=budget_config)))
    failure: Exception | None = None
    main_runner: MainAgentRunner | None = None
    owners: dict[str, TesterRunner] = {}
    replay_values: dict[str, dict[str, str]] = {}
    shared_coordination = route is None or route.coordination == "SHARED_STATE"
    handled_findings: set[str] = set()
    handled_replans: set[str] = set()
    try:
        main_tools = MainAgentTools(store=store, run_id=run_id, target_url=str(config.target_url), focus_features=config.focus_features, allowed_scope=config.allowed_scope, denied_operations=config.denied_operations, max_step_budget=config.budget.max_browser_steps_per_task)
        main_usage = ProviderUsageMiddleware(store=store, run_id=run_id, budget_id=main_budget_id, provider="openrouter", model=settings.main_agent_model, agent="main")
        main_agent = create_main_agent(client=main_client or create_main_chat_client(settings, usage=main_usage), settings=settings, tools=main_tools)
        main_runner = MainAgentRunner(agent=main_agent, tools=main_tools, run_id=run_id, max_replans=config.budget.max_replans_per_task, usage=main_usage, provider_usage_recorded=main_client is None)
        await main_runner.create_initial_plan(config)
        if not store.list_tasks(run_id):
            raise RuntimeError("Main Agent did not create any Tasks")
        selector = jev_selector or JevSelector.openrouter(settings)
        finding_service = FindingService(store, run_id, shared_state=shared_coordination)
        account_by_reference = {account.identity_reference: account for account in config.account_references}
        identity_values: dict[str, dict[str, str]] = {}
        identity_references: dict[str, str] = {}
        for account in config.account_references:
            identity_id = f"{run_id}-{account.identity_reference}"
            store.create_identity(identity_id=identity_id, run_id=run_id, role=account.role, secret_reference=account.secret_reference, permissions=account.permissions)
            identity_references[identity_id] = account.identity_reference
            values = {key: value for key, value in config.test_data.items() if isinstance(value, str)}
            if account.secret_reference is not None:
                if not account.secret_reference.startswith("env:"):
                    raise ValueError("only env: account secret references are supported")
                secret = os.environ.get(account.secret_reference[4:])
                if secret is not None:
                    values[account.secret_reference] = secret
            identity_values[account.identity_reference] = values

        def create_tester(task: Mapping[str, Any]) -> TesterInstance:
            task_id = str(task["task_id"])
            requirements = dict(task["data_requirements"])
            reference = requirements.get("identity_reference")
            selected_account = None
            role = requirements.get("role")
            if reference is not None:
                selected_account = account_by_reference.get(str(reference))
                if role is None and selected_account is not None:
                    role = selected_account.role
            if role is None:
                role = config.account_references[0].role
            role = str(role)
            accounts = [account for account in config.account_references if account.role == role]
            if reference is None and len(accounts) == 1:
                selected_account = accounts[0]
            if selected_account is None or selected_account.role != role:
                raise ValueError(f"Task {task_id} needs an unambiguous identity_reference for role {role}")
            feature = str(requirements.get("feature", ""))
            requested_ids = requirements.get("expected_behavior_ids")
            behaviors = [behavior for behavior in config.expected_behaviors if behavior.behavior_id in requested_ids] if requested_ids is not None else [behavior for behavior in config.expected_behaviors if behavior.applies_to.split("/")[0].casefold() == feature.casefold()]
            if requested_ids is not None and set(requested_ids) != {behavior.behavior_id for behavior in behaviors}:
                raise ValueError(f"Task {task_id} references unknown expected behaviors")
            values = dict(identity_values[selected_account.identity_reference])
            selected_keys = requirements.get("test_data_keys", [])
            if any(key not in config.test_data for key in selected_keys):
                raise ValueError(f"Task {task_id} references unavailable test data")
            context = {"goal": task["goal"], "feature": feature, "role": role, "identity_reference": selected_account.identity_reference, "secret_reference": selected_account.secret_reference, "expected_behaviors": [behavior.model_dump(mode="json") for behavior in behaviors], "test_data_references": selected_keys or list(config.test_data), "scope": requirements.get("scope_targets", config.allowed_scope), "denied_operations": config.denied_operations, "required_operations": requirements.get("required_operations", []), "step_budget": task["step_budget"]}
            context["target_url"] = str(config.target_url)
            context["required_checks"] = requirements.get("required_checks", [])
            current_run = store.get_run(run_id)
            assert current_run is not None
            tester_id = f"tester-{uuid4().hex}"
            identity_id = f"{run_id}-{selected_account.identity_reference}"
            budget_id = f"budget-{uuid4().hex}"
            namespace = build_data_namespace(run_id, tester_id, task_id)
            context["data_namespace"] = namespace
            store.create_budget(budget_id=budget_id, run_id=run_id, task_id=task_id)
            budget = BudgetGuard(_budget_limits(config, task_steps=int(task["step_budget"]), remaining=current_run["remaining_budget"]))
            checker = PermissionChecker(store=store, policy=ExecutionPolicy(allowed_url_prefixes=tuple(urljoin(str(config.target_url), scope) for scope in config.allowed_scope), denied_operations=frozenset(config.denied_operations)), run_id=run_id, task_id=task_id, tester_id=tester_id)
            reader = PageStateReader()
            executor = PlaywrightExecutor(store=store, permission_checker=checker, run_id=run_id, task_id=task_id, tester_id=tester_id, identity_reference=selected_account.identity_reference)
            secrets = tuple(value for key, value in values.items() if key.startswith("env:"))
            evidence = EvidenceStore(store=store, artifacts_root=settings.artifacts_dir, temporary_sensitive_root=settings.temporary_sensitive_dir, secrets=secrets)
            computer_controller = None
            if (route is None or route.visual_fallback == "COMPUTER_USE") and config.budget.max_computer_use_calls > 0 and settings.computer_use_model and (tester_client_factory is None or computer_use_client_factory is not None):
                computer_client = computer_use_client_factory() if computer_use_client_factory is not None else OpenRouterComputerUseClient(settings)
                computer_controller = ComputerUseController(client=computer_client, settings=settings, store=store, evidence_store=evidence, page_state_reader=reader, permission_checker=checker, budget=budget, budget_id=budget_id, run_id=run_id, task_id=task_id, tester_id=tester_id)
            runtime = WebTestingRuntime(store=store, browser_manager=manager, executor=executor, page_state_reader=reader, candidate_builder=CandidateBuilder(checker), jev_selector=selector, budget=budget, run_id=run_id, task_id=task_id, tester_id=tester_id, identity_id=identity_id, budget_id=budget_id, computer_use_controller=computer_controller, input_values=values, expected_behavior_ids=tuple(behavior.behavior_id for behavior in behaviors), evidence_store=evidence)
            assignment = TesterAssignment(run_id=run_id, task_id=task_id, tester_id=tester_id, identity_id=identity_id, role=role, data_namespace=namespace, scope=tuple(config.allowed_scope), step_budget=int(task["step_budget"]))
            tools = TesterAgentTools(assignment=assignment, runtime=runtime, store=store, shared_state=shared_coordination, decision_policy=route.candidate_selection if route else "JEV", action_policy=route.known_action if route else "PLAYWRIGHT", task_context=context)
            usage = ProviderUsageMiddleware(store=store, run_id=run_id, budget_id=budget_id, provider="openrouter", model=settings.tester_agent_model, agent="tester", task_id=task_id, tester_id=tester_id, budget=budget)
            client = tester_client_factory() if tester_client_factory is not None else create_tester_chat_client(settings, usage=usage)
            tester = TesterRunner(agent=create_tester_agent(client=client, assignment=assignment, tools=tools), assignment=assignment, runtime=runtime, store=store, budget=budget, budget_id=budget_id, provider_usage_recorded=tester_client_factory is None, decision_policy=route.candidate_selection if route else "JEV", action_policy=route.known_action if route else "PLAYWRIGHT", prepare_task_page=tester_client_factory is None)
            store.register_tester(tester_id=tester_id, run_id=run_id, session_reference=tester.session.session_id, identity_id=identity_id, role=role, data_namespace=namespace)
            owners[tester_id] = tester
            replay_values[tester_id] = values
            return TesterInstance(tester_id=tester_id, runner=tester)

        async def execute_task(tester: TesterRunner, task: Mapping[str, Any]) -> str:
            with trace_span("Tester", metadata={"agent_role": "tester", "tester_id": tester.assignment.tester_id, "task_id": str(task["task_id"]), "phase": "tester", "model": settings.tester_agent_model, "provider": settings.tester_agent_provider, "ls_model_name": settings.tester_agent_model, "ls_provider": "openrouter"}) as span:
                result = await execute_worker(tester, task)
                trace_result(span, status=result, success=result == "COMPLETED")
                return result

        async def execute_worker(tester: TesterRunner, task: Mapping[str, Any]) -> str:
            navigation = await tester.execute_known(WebAction(action_type=ActionType.NAVIGATION, url=str(config.target_url)))
            if not navigation["success"]:
                raise RuntimeError("Initial navigation failed")
            tools_context = tester.agent.additional_properties["task_context"]
            answer = await tester.ask_tester_llm(f"Execute assigned Task {task['task_id']}: {task['goal']}.\nTask Context: {json.dumps(tools_context, ensure_ascii=False)}\nUse provided tools and goal assertions. Finish with finish_task. Do not declare a confirmed bug.")
            if not tester.runtime.task_finished and tester.runtime.browser_session_id is not None:
                await tester.runtime.record_task_outcome()
            for finding in store.list_recent_findings(run_id, limit=1000):
                if finding["first_seen_by"] == tester.assignment.tester_id:
                    await tester.runtime.capture_finding_evidence(str(finding["finding_id"]))
            return "STOPPED" if answer.startswith("STOPPED:") else "COMPLETED"

        async def coordinate(result: ScheduleResult) -> None:
            assert main_runner is not None
            findings = [finding for finding in store.list_recent_findings(run_id, limit=1000) if finding["finding_id"] not in handled_findings]
            new_findings = []
            for finding in reversed(findings):
                finding_id = str(finding["finding_id"])
                handled_findings.add(finding_id)
                if finding["status"] in {"OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"}:
                    finding = finding_service.screen(finding_id, ScreeningSignals())
                new_findings.append(finding)
            requests = [event for event in store.list_events(run_id, event_types=("REQUEST_REPLAN",)) if event["event_id"] not in handled_replans]
            handled_replans.update(str(event["event_id"]) for event in requests)
            if not shared_coordination or not any(task["status"] == "PENDING" for task in store.list_open_tasks(run_id)):
                return
            if requests:
                await main_runner.replan(str(requests[-1]["result"].get("reason", "TESTER_REQUEST")))
                return
            for finding in new_findings:
                try:
                    main_tools._require_high_risk_finding(str(finding["finding_id"]))
                except ValueError:
                    continue
                await main_runner.replan(f"HIGH_RISK_FINDING:{finding['finding_id']}")
                break

        scheduler = LocalTesterScheduler(store=store, run_id=run_id, max_testers=tester_count, max_browser_contexts=config.budget.max_parallel_browser_contexts, tester_factory=create_tester, on_task_finished=coordinate)
        await scheduler.run_ready_tasks(execute_task)
        for task in store.list_open_tasks(run_id):
            store.update_task_status(task_id=str(task["task_id"]), expected_status=str(task["status"]), new_status="STOPPED")
            store.record_task_outcome(task_id=str(task["task_id"]), success_status="UNKNOWN", reason="DEPENDENCY_OR_BUDGET_BLOCKED", assertion_results=[])
        # Exploration has ended: global application resets cannot disrupt another Tester.
        for finding in store.list_recent_findings(run_id, limit=1000):
            if finding["status"] not in {"ANOMALY", "SUSPECTED_ISSUE", "OBSERVATION"}:
                continue
            finding_id = str(finding["finding_id"])
            auto_reproduction = route is None or route.finding_after_anomaly == "REPRODUCTION"
            owner = owners.get(str(finding["first_seen_by"]))
            if not auto_reproduction or owner is None or finding["status"] == "OBSERVATION":
                store.update_finding_status(finding_id=finding_id, status="NEEDS_CONFIRMATION" if auto_reproduction else "SUSPECTED_ISSUE", screening_reason="REPRODUCTION_NOT_RUN", needs_confirmation=True)
                continue
            values = replay_values[owner.assignment.tester_id]
            plan_result = ReplayPlanBuilder(store).from_finding(finding_id=finding_id, input_values=values, identity_values=identity_values, identity_references=identity_references)
            if plan_result.plan is None:
                created_events = store.list_events(run_id, event_types=("FINDING_CREATED",), task_id=str(finding["task_id"]))
                boundary = next((event["result"].get("boundary_event_id") for event in created_events if event["result"].get("finding_id") == finding_id), None)
                related: list[str] = next((event["result"].get("related_task_ids", []) for event in created_events if event["result"].get("finding_id") == finding_id), [])
                plan_result = ReplayPlanBuilder(store).from_action_history(run_id=run_id, task_id=str(finding["task_id"]), input_values=values, through_event_id=boundary, related_task_ids=related, identity_values=identity_values, identity_references=identity_references)
            if plan_result.plan is None or not plan_result.plan.assertion_positions:
                store.update_finding_status(finding_id=finding_id, status="NEEDS_CONFIRMATION", screening_reason=plan_result.reason or "DETERMINISTIC_ASSERTION_MISSING", needs_confirmation=True)
                continue
            current_run = store.get_run(run_id)
            assert current_run is not None
            replay_budget_id = f"budget-{uuid4().hex}"
            store.create_budget(budget_id=replay_budget_id, run_id=run_id, task_id=str(finding["task_id"]))
            limits = _budget_limits(config, task_steps=config.budget.max_replay_steps_per_finding, remaining=current_run["remaining_budget"])
            limits = replace(limits, max_task_steps=config.budget.max_replay_steps_per_finding)
            replay_budget = BudgetGuard(limits)
            evidence = EvidenceStore(store=store, artifacts_root=settings.artifacts_dir, temporary_sensitive_root=settings.temporary_sensitive_dir, secrets=tuple(value for inputs in identity_values.values() for key, value in inputs.items() if key.startswith("env:")))
            replay = DeterministicReplay(store=store, browser_manager=manager, executor=owner.runtime.executor, evidence_store=evidence, budget=replay_budget, budget_id=replay_budget_id)
            reset_hook = (lambda: _reset_before_replay(config)) if config.reset_hook is not None else None
            replay_metadata = {"task_id": str(finding["task_id"]), "finding_id": finding_id, "tester_id": owner.assignment.tester_id, "agent_role": "runtime"}
            with trace_span("Reproduction", metadata={**replay_metadata, "phase": "reproduction"}) as span:
                reproduced = await ReproductionRunner(store=store, finding_service=finding_service, replay=replay).run(finding_id=finding_id, plan=plan_result.plan, max_attempts=config.budget.max_reproductions_per_finding, stable_successes=min(2, config.budget.max_reproductions_per_finding), max_minimization_attempts=0, reset_hook=reset_hook)
                trace_result(span, status=reproduced.status)
            if reproduced.status == "REPRODUCED":
                verified_plan = ReplayPlanBuilder(store).from_finding(finding_id=finding_id, input_values=values, identity_values=identity_values, identity_references=identity_references)
                if verified_plan.plan is not None:
                    with trace_span("Verification", metadata={**replay_metadata, "phase": "verification"}) as span:
                        verified = await VerificationRunner(store=store, finding_service=finding_service, replay=replay).run(finding_id=finding_id, plan=verified_plan.plan, reset_hook=reset_hook)
                        trace_result(span, status=verified.result)
    except Exception as error:
        failure = error
    finally:
        await manager.close()
        tasks = store.list_tasks(run_id)
        for outcome in score_run_tasks(store, run_id):
            store.record_task_outcome(task_id=outcome["task_id"], success_status=outcome["success_status"], reason=outcome["reason"], assertion_results=outcome["assertions"])
        status = "FAILED" if failure is not None or any(task["status"] == "FAILED" for task in tasks) else "STOPPED" if any(task["status"] not in {"COMPLETED", "CANCELLED"} for task in tasks) else "COMPLETED"
        report_path = settings.artifacts_dir / run_id / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = FinalReportBuilder(store).build(run_id, full_evaluation=route.full_evaluation if route is not None else False)
        report["test_summary"]["run_status"] = status
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        testing_metrics = report["cost_and_performance"]
        if main_runner is not None:
            try:
                summary = await main_runner.summarize_report(report)
                summary_path = report_path.with_name("summary.md")
                summary_path.write_text(summary.text, encoding="utf-8")
            except Exception as error:
                store.append_event(event_id=f"event-{uuid4().hex}", run_id=run_id, event_type="FINAL_REPORT_SUMMARY_FAILED", tool="MainAgent", action="summarize_report", result={"status": "FAILED", "error_type": type(error).__name__}, latency_ms=0)
                if failure is None:
                    failure = error
                    status = "FAILED"
        store.finish_run(run_id=run_id, status=status, stop_reason=type(failure).__name__ if failure is not None else None)
        report = FinalReportBuilder(store).build(run_id, full_evaluation=route.full_evaluation if route is not None else False)
        report["testing_phase_cost_and_performance"] = testing_metrics
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
    report_path = asyncio.run(run(config, Settings(), scenario_id=arguments.config.stem))
    print(report_path.with_name("summary.md").read_text(encoding="utf-8"))
    print(f"\nStructured report: {report_path}")
