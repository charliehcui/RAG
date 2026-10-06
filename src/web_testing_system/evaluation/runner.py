"""Six controlled evaluation modes, result aggregation, and final gates."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any, Protocol
from uuid import uuid4

from agent_framework import BaseChatClient

from web_testing_system.config import RunConfig, Settings
from web_testing_system.evaluation.metrics import MetricsCalculator
from web_testing_system.runtime.computer_use import ComputerUseClient
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.state import StateStore

N_A = "N/A"
MetricValue = int | float | str


class EvaluationMode(StrEnum):
    TESTER_COUNT = "single_tester_vs_multi_tester"
    DECISION_ENGINE = "jev_vs_llm_every_decision"
    COORDINATION = "shared_state_vs_independent_testers"
    REPRODUCTION = "auto_reproduction_on_vs_off"
    ACTION_POLICY = "playwright_first_vs_model_every_step"
    COMPUTER_USE = "computer_use_fallback_on_vs_off"


@dataclass(frozen=True)
class EvaluationControls:
    demo_version: str
    seeded_bugs: tuple[str, ...]
    model_configuration: tuple[tuple[str, str], ...]
    token_budget: int
    time_budget_seconds: int
    accounts: tuple[str, ...]
    initial_data: tuple[tuple[str, str], ...]
    test_scope: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationVariant:
    variant_id: str
    tester_count: int
    decision_policy: str
    shared_state: bool
    auto_reproduction: bool
    action_policy: str
    computer_use_fallback: bool
    controls: EvaluationControls


@dataclass(frozen=True)
class EvaluationPlan:
    mode: EvaluationMode
    variants: tuple[EvaluationVariant, EvaluationVariant]
    changed_field: str


@dataclass(frozen=True)
class ExecutionRoute:
    tester_count: int
    candidate_selection: str
    coordination: str
    finding_after_anomaly: str
    known_action: str
    visual_fallback: str
    full_evaluation: bool = False


@dataclass(frozen=True)
class EvaluationExecution:
    status: str
    metrics: Mapping[str, MetricValue]
    error: str | None = None


@dataclass(frozen=True)
class EvaluationRunRecord:
    mode: str
    variant_id: str
    run_number: int
    run_id: str
    status: str
    metrics: Mapping[str, MetricValue]
    evidence_directory: str
    error: str | None = None


class VariantExecutor(Protocol):
    is_fake: bool

    async def execute(self, *, config: EvaluationVariant, route: ExecutionRoute, run_number: int, run_id: str, evidence_directory: Path) -> EvaluationExecution: ...


ResetCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class FinalAcceptanceResult:
    passed: bool
    checks: Mapping[str, bool]
    agent_types: tuple[str, ...]
    task23_passed: bool
    full_evaluation: str


class AcceptanceChecklist:
    REQUIRED_CAPABILITIES = (
        "main_agent",
        "tester_agent",
        "multi_agent_collaboration",
        "dynamic_replanning",
        "sqlite_shared_state",
        "playwright",
        "jev_boundary",
        "finding",
        "evidence",
        "reproduction",
        "verification",
        "computer_use_boundary",
        "demo_app",
        "seeded_bugs_b1_b6",
        "final_report",
        "metrics",
        "evaluation_modes",
        "decision_order",
        "ground_truth_isolation",
        "secret_safety",
    )

    def check(self, *, capability_checks: Mapping[str, bool], agent_types: Sequence[str], task23_passed: bool, full_evaluation_ran: bool) -> FinalAcceptanceResult:
        checks = {name: bool(capability_checks.get(name, False)) for name in self.REQUIRED_CAPABILITIES}
        normalized_agent_types = tuple(sorted(set(agent_types)))
        checks["only_main_and_tester_agents"] = normalized_agent_types == ("Main Agent", "Tester Agent")
        checks["task23_low_cost_demo"] = task23_passed
        checks["full_evaluation_not_run"] = not full_evaluation_ran
        return FinalAcceptanceResult(passed=all(checks.values()), checks=checks, agent_types=normalized_agent_types, task23_passed=task23_passed, full_evaluation="Full Evaluation: RUN" if full_evaluation_ran else "Full Evaluation: NOT RUN")


@dataclass(frozen=True)
class FullEvaluationGateResult:
    allowed: bool
    reason: str


class FullEvaluationGate:
    def check(self, *, settings: Settings, acceptance: FinalAcceptanceResult, explicitly_enabled: bool) -> FullEvaluationGateResult:
        if not settings.full_evaluation:
            return FullEvaluationGateResult(False, "FULL_EVALUATION_DISABLED")
        if not explicitly_enabled:
            return FullEvaluationGateResult(False, "EXPLICIT_ENABLE_REQUIRED")
        if not acceptance.task23_passed:
            return FullEvaluationGateResult(False, "TASK_23_NOT_PASSED")
        if not acceptance.passed:
            return FullEvaluationGateResult(False, "FINAL_ACCEPTANCE_INCOMPLETE")
        return FullEvaluationGateResult(True, "FULL_EVALUATION_ALLOWED")


def build_evaluation_plan(mode: EvaluationMode, controls: EvaluationControls) -> EvaluationPlan:
    base = EvaluationVariant(variant_id="baseline", tester_count=3, decision_policy="JEV", shared_state=True, auto_reproduction=True, action_policy="PLAYWRIGHT_FIRST", computer_use_fallback=True, controls=controls)
    values: dict[EvaluationMode, tuple[str, str, Any, str, Any]] = {
        EvaluationMode.TESTER_COUNT: ("single-tester", "tester_count", 1, "multi-tester", 3),
        EvaluationMode.DECISION_ENGINE: ("jev", "decision_policy", "JEV", "llm-every-decision", "LLM_EVERY_DECISION"),
        EvaluationMode.COORDINATION: ("shared-state", "shared_state", True, "independent-testers", False),
        EvaluationMode.REPRODUCTION: ("auto-reproduction-on", "auto_reproduction", True, "auto-reproduction-off", False),
        EvaluationMode.ACTION_POLICY: ("playwright-first", "action_policy", "PLAYWRIGHT_FIRST", "model-every-step", "MODEL_EVERY_STEP"),
        EvaluationMode.COMPUTER_USE: ("computer-use-on", "computer_use_fallback", True, "computer-use-off", False),
    }
    left_id, field, left_value, right_id, right_value = values[mode]
    left = replace(base, variant_id=left_id, **{field: left_value})
    right = replace(base, variant_id=right_id, **{field: right_value})
    plan = EvaluationPlan(mode=mode, variants=(left, right), changed_field=field)
    _validate_plan(plan)
    return plan


def build_execution_route(config: EvaluationVariant, *, full_evaluation_enabled: bool, fake_provider: bool = False) -> ExecutionRoute:
    if not 1 <= config.tester_count <= 4:
        raise ValueError("tester_count must be between 1 and 4")
    if config.decision_policy not in {"JEV", "LLM_EVERY_DECISION"}:
        raise ValueError(f"unsupported decision policy: {config.decision_policy}")
    if config.action_policy not in {"PLAYWRIGHT_FIRST", "MODEL_EVERY_STEP"}:
        raise ValueError(f"unsupported action policy: {config.action_policy}")
    if config.action_policy == "MODEL_EVERY_STEP" and not (full_evaluation_enabled or fake_provider):
        raise PermissionError("MODEL_EVERY_STEP_REQUIRES_FULL_EVALUATION")
    return ExecutionRoute(
        tester_count=config.tester_count,
        candidate_selection="JEV" if config.decision_policy == "JEV" and config.action_policy == "PLAYWRIGHT_FIRST" else "TESTER_LLM_EVERY_DECISION",
        coordination="SHARED_STATE" if config.shared_state else "INDEPENDENT_TESTER_STATE",
        finding_after_anomaly="REPRODUCTION" if config.auto_reproduction else "SUSPECTED_ISSUE",
        known_action="PLAYWRIGHT" if config.action_policy == "PLAYWRIGHT_FIRST" else "TESTER_LLM_EVERY_STEP",
        visual_fallback="COMPUTER_USE" if config.computer_use_fallback else "FAIL_WITHOUT_COMPUTER_USE",
        full_evaluation=full_evaluation_enabled,
    )


class FormalRunExecutor:
    """Run an Evaluation variant through the same entry point as the CLI."""

    def __init__(self, run_config: RunConfig, settings: Settings, *, main_client_factory: Callable[[], BaseChatClient] | None = None, tester_client_factory: Callable[[], BaseChatClient] | None = None, jev_selector_factory: Callable[[], JevSelector] | None = None, computer_use_client_factory: Callable[[], ComputerUseClient] | None = None) -> None:
        self.run_config = run_config
        self.settings = settings
        self.main_client_factory = main_client_factory
        self.tester_client_factory = tester_client_factory
        self.jev_selector_factory = jev_selector_factory
        self.computer_use_client_factory = computer_use_client_factory
        self.is_fake = all(factory is not None for factory in (main_client_factory, tester_client_factory, jev_selector_factory, computer_use_client_factory))

    async def execute(self, *, config: EvaluationVariant, route: ExecutionRoute, run_number: int, run_id: str, evidence_directory: Path) -> EvaluationExecution:
        del config, run_number
        from web_testing_system.orchestration.runner import run

        main_client = self.main_client_factory() if self.main_client_factory is not None else None
        jev_selector = self.jev_selector_factory() if self.jev_selector_factory is not None else None
        run_directory = evidence_directory.parent.parent
        run_settings = self.settings.model_copy(update={"artifacts_dir": run_directory, "state_db_path": run_directory / "state.db", "temporary_sensitive_dir": run_directory / "temporary_sensitive"})
        report_path = await run(self.run_config, run_settings, route=route, run_id=run_id, main_client=main_client, tester_client_factory=self.tester_client_factory, computer_use_client_factory=self.computer_use_client_factory, jev_selector=jev_selector)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        metrics = MetricsCalculator(StateStore(run_settings.state_db_path)).calculate(run_id, final_report=report)
        status = str(report["test_summary"]["run_status"])
        return EvaluationExecution(status="INTERRUPTED" if status == "STOPPED" else status, metrics=metrics)


class EvaluationRunner:
    """Execute injected variants; development samples require an explicitly fake executor."""

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory

    async def run_fake_sample(self, *, plan: EvaluationPlan, executor: VariantExecutor, reset: ResetCallback) -> list[EvaluationRunRecord]:
        if not executor.is_fake:
            raise PermissionError("DEVELOPMENT_SAMPLE_REQUIRES_FAKE_EXECUTOR")
        records = await self._run(plan=plan, executor=executor, reset=reset, runs_per_variant=1, full_evaluation_enabled=False)
        self._save_summary(plan, records)
        return records

    async def run_full_mode(self, *, plan: EvaluationPlan, executor: VariantExecutor, reset: ResetCallback, gate: FullEvaluationGateResult) -> list[EvaluationRunRecord]:
        if not gate.allowed:
            raise PermissionError(gate.reason)
        records = await self._run(plan=plan, executor=executor, reset=reset, runs_per_variant=3, full_evaluation_enabled=True)
        self._save_summary(plan, records)
        return records

    async def _run(self, *, plan: EvaluationPlan, executor: VariantExecutor, reset: ResetCallback, runs_per_variant: int, full_evaluation_enabled: bool) -> list[EvaluationRunRecord]:
        records: list[EvaluationRunRecord] = []
        for variant in plan.variants:
            route = build_execution_route(variant, full_evaluation_enabled=full_evaluation_enabled, fake_provider=executor.is_fake)
            for run_number in range(1, runs_per_variant + 1):
                await reset()
                run_id = f"evaluation-{plan.mode.value}-{variant.variant_id}-{run_number}-{uuid4().hex[:8]}"
                run_directory = self.output_directory / plan.mode.value / variant.variant_id / f"run-{run_number}"
                evidence_directory = run_directory / run_id / "evidence"
                evidence_directory.mkdir(parents=True, exist_ok=True)
                try:
                    execution = await executor.execute(config=variant, route=route, run_number=run_number, run_id=run_id, evidence_directory=evidence_directory)
                    if execution.status not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                        raise ValueError(f"unsupported evaluation status: {execution.status}")
                    status = execution.status
                    metrics = dict(execution.metrics) if status == "COMPLETED" else {}
                    error = execution.error
                except Exception as caught_error:
                    status = "FAILED"
                    metrics = {}
                    error = f"{type(caught_error).__name__}: {caught_error}"
                record = EvaluationRunRecord(mode=plan.mode.value, variant_id=variant.variant_id, run_number=run_number, run_id=run_id, status=status, metrics=metrics, evidence_directory=str(evidence_directory.relative_to(self.output_directory)), error=error)
                self._save_record(run_directory / "result.json", variant, route, record)
                records.append(record)
        return records

    @staticmethod
    def _save_record(path: Path, config: EvaluationVariant, route: ExecutionRoute, record: EvaluationRunRecord) -> None:
        content = {"config": asdict(config), "route": asdict(route), "result": asdict(record)}
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_summary(self, plan: EvaluationPlan, records: Sequence[EvaluationRunRecord]) -> None:
        path = self.output_directory / plan.mode.value / "summary.json"
        path.write_text(json.dumps(summarize_evaluation_runs(records), ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_evaluation_runs(records: Sequence[EvaluationRunRecord]) -> dict[str, Any]:
    grouped: dict[str, list[EvaluationRunRecord]] = {}
    for record in records:
        grouped.setdefault(record.variant_id, []).append(record)
    summary: dict[str, Any] = {}
    for variant_id, variant_records in grouped.items():
        ordered = sorted(variant_records, key=lambda item: item.run_number)
        metric_names = sorted({name for record in ordered for name in record.metrics})
        metric_summary: dict[str, dict[str, MetricValue]] = {}
        for name in metric_names:
            values: dict[str, MetricValue] = {}
            numeric: list[float] = []
            for record in ordered:
                value = record.metrics.get(name, N_A) if record.status == "COMPLETED" else N_A
                values[f"run_{record.run_number}"] = value
                if isinstance(value, int | float) and not isinstance(value, bool):
                    numeric.append(float(value))
            values["median"] = round(float(median(numeric)), 6) if numeric else N_A
            values["min"] = round(min(numeric), 6) if numeric else N_A
            values["max"] = round(max(numeric), 6) if numeric else N_A
            metric_summary[name] = values
        summary[variant_id] = {
            "runs": [{"run": record.run_number, "run_id": record.run_id, "status": record.status, "error": record.error} for record in ordered],
            "metrics": metric_summary,
        }
    return summary


def _validate_plan(plan: EvaluationPlan) -> None:
    left, right = plan.variants
    if left.controls != right.controls:
        raise ValueError("evaluation controls must be identical between variants")
    ignored = {"variant_id", "controls"}
    left_values = asdict(left)
    right_values = asdict(right)
    changed = {name for name in left_values if name not in ignored and left_values[name] != right_values[name]}
    if changed != {plan.changed_field}:
        raise ValueError("an evaluation pair must change exactly one comparison field")
