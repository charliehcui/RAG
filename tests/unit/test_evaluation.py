from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from web_testing_system.config import Settings
from web_testing_system.evaluation import (
    EvaluationControls,
    EvaluationExecution,
    EvaluationMode,
    EvaluationRunner,
    EvaluationRunRecord,
    FinalAcceptanceChecker,
    FullEvaluationGate,
    FullEvaluationGateResult,
    build_evaluation_plan,
    build_execution_route,
    summarize_evaluation_runs,
)


def make_controls() -> EvaluationControls:
    return EvaluationControls(demo_version="demo-1", seeded_bugs=("B1",), model_configuration=(("main", "fake-gemini"), ("tester", "fake-groq")), token_budget=100, time_budget_seconds=30, accounts=("admin", "member"), initial_data=(("project", "project-1"),), test_scope=("Project", "Task"))


class FakeExecutor:
    is_fake = True

    def __init__(self, *, status: str = "COMPLETED") -> None:
        self.status = status
        self.calls: list[tuple[object, object, int, Path]] = []

    async def execute(self, *, config: object, route: object, run_number: int, run_id: str, evidence_directory: Path) -> EvaluationExecution:
        del run_id
        self.calls.append((config, route, run_number, evidence_directory))
        return EvaluationExecution(status=self.status, metrics={"score": float(run_number)})


class RealExecutor(FakeExecutor):
    is_fake = False


def complete_acceptance(*, task23_passed: bool = True, full_evaluation_ran: bool = False, agent_types: tuple[str, ...] = ("Main Agent", "Tester Agent")):
    checker = FinalAcceptanceChecker()
    checks = {name: True for name in checker.REQUIRED_CAPABILITIES}
    return checker.check(capability_checks=checks, agent_types=agent_types, task23_passed=task23_passed, full_evaluation_ran=full_evaluation_ran)


def test_all_six_modes_change_only_the_named_field_and_keep_controls() -> None:
    controls = make_controls()

    plans = [build_evaluation_plan(mode, controls) for mode in EvaluationMode]

    assert len(plans) == 6
    assert {plan.mode for plan in plans} == set(EvaluationMode)
    for plan in plans:
        left, right = plan.variants
        assert left.controls == right.controls == controls
        changed = {name for name in asdict(left) if name not in {"variant_id", "controls"} and getattr(left, name) != getattr(right, name)}
        assert changed == {plan.changed_field}


def test_six_mode_variants_select_the_required_execution_paths() -> None:
    plans = {mode: build_evaluation_plan(mode, make_controls()) for mode in EvaluationMode}

    tester_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.TESTER_COUNT].variants]
    assert [route.tester_count for route in tester_routes] == [1, 2]
    decision_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.DECISION_ENGINE].variants]
    assert [route.candidate_selection for route in decision_routes] == ["LAYA_JEV", "TESTER_LLM_EVERY_DECISION"]
    coordination_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.COORDINATION].variants]
    assert [route.coordination for route in coordination_routes] == ["SHARED_STATE", "INDEPENDENT_TESTER_STATE"]
    reproduction_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.REPRODUCTION].variants]
    assert [route.finding_after_anomaly for route in reproduction_routes] == ["REPRODUCTION", "SUSPECTED_ISSUE"]
    computer_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.COMPUTER_USE].variants]
    assert [route.visual_fallback for route in computer_routes] == ["COMPUTER_USE", "FAIL_WITHOUT_COMPUTER_USE"]
    action_variants = plans[EvaluationMode.ACTION_POLICY].variants
    assert build_execution_route(action_variants[0], full_evaluation_enabled=False).known_action == "PLAYWRIGHT"
    with pytest.raises(PermissionError, match="MODEL_EVERY_STEP_REQUIRES_FULL_EVALUATION"):
        build_execution_route(action_variants[1], full_evaluation_enabled=False)
    assert build_execution_route(action_variants[1], full_evaluation_enabled=True).known_action == "TESTER_LLM_EVERY_STEP"


def test_invalid_policy_cannot_bypass_the_model_every_step_gate() -> None:
    variant = build_evaluation_plan(EvaluationMode.ACTION_POLICY, make_controls()).variants[0]

    with pytest.raises(ValueError, match="unsupported action policy"):
        build_execution_route(replace(variant, action_policy="MODEL_EVERY_STEP_TYPO"), full_evaluation_enabled=False)


@pytest.mark.asyncio
async def test_fake_sample_resets_records_raw_results_and_writes_summary(tmp_path: Path) -> None:
    plan = build_evaluation_plan(EvaluationMode.COMPUTER_USE, make_controls())
    executor = FakeExecutor()
    reset_count = 0

    async def reset() -> None:
        nonlocal reset_count
        reset_count += 1

    records = await EvaluationRunner(tmp_path).run_fake_sample(plan=plan, executor=executor, reset=reset)

    assert reset_count == 2
    assert len(records) == 2
    assert len({record.evidence_directory for record in records}) == 2
    for record in records:
        result_path = tmp_path / plan.mode.value / record.variant_id / "run-1" / "result.json"
        assert result_path.exists()
        assert json.loads(result_path.read_text(encoding="utf-8"))["result"]["status"] == "COMPLETED"
    summary_path = tmp_path / plan.mode.value / "summary.json"
    assert summary_path.exists()
    assert set(json.loads(summary_path.read_text(encoding="utf-8"))) == {"computer-use-on", "computer-use-off"}


@pytest.mark.asyncio
async def test_development_runner_rejects_real_executor_and_full_mode_without_gate(tmp_path: Path) -> None:
    plan = build_evaluation_plan(EvaluationMode.TESTER_COUNT, make_controls())

    async def reset() -> None:
        return None

    real_executor = RealExecutor()
    with pytest.raises(PermissionError, match="DEVELOPMENT_SAMPLE_REQUIRES_FAKE_EXECUTOR"):
        await EvaluationRunner(tmp_path).run_fake_sample(plan=plan, executor=real_executor, reset=reset)
    with pytest.raises(PermissionError, match="FULL_EVALUATION_DISABLED"):
        await EvaluationRunner(tmp_path).run_full_mode(plan=plan, executor=FakeExecutor(), reset=reset, gate=FullEvaluationGateResult(False, "FULL_EVALUATION_DISABLED"))
    assert real_executor.calls == []


@pytest.mark.asyncio
async def test_invalid_executor_status_is_recorded_as_failure(tmp_path: Path) -> None:
    plan = build_evaluation_plan(EvaluationMode.COORDINATION, make_controls())

    async def reset() -> None:
        return None

    records = await EvaluationRunner(tmp_path).run_fake_sample(plan=plan, executor=FakeExecutor(status="MADE_UP"), reset=reset)

    assert {record.status for record in records} == {"FAILED"}
    assert all(record.metrics == {} for record in records)
    assert all("unsupported evaluation status" in str(record.error) for record in records)


def test_run_aggregation_reports_each_run_median_min_max_and_truthful_failure() -> None:
    records = [
        EvaluationRunRecord(mode="mode", variant_id="variant-a", run_number=1, run_id="a-1", status="COMPLETED", metrics={"score": 3.0}, evidence_directory="a/1"),
        EvaluationRunRecord(mode="mode", variant_id="variant-a", run_number=2, run_id="a-2", status="FAILED", metrics={"score": 999.0}, evidence_directory="a/2", error="fake failure"),
        EvaluationRunRecord(mode="mode", variant_id="variant-a", run_number=3, run_id="a-3", status="COMPLETED", metrics={"score": 1.0}, evidence_directory="a/3"),
    ]

    summary = summarize_evaluation_runs(records)["variant-a"]

    assert summary["metrics"]["score"] == {"run_1": 3.0, "run_2": "N/A", "run_3": 1.0, "median": 2.0, "min": 1.0, "max": 3.0}
    assert summary["runs"][1]["status"] == "FAILED"
    assert summary["runs"][1]["error"] == "fake failure"


def test_final_acceptance_and_full_evaluation_gate_require_every_condition() -> None:
    gate = FullEvaluationGate()
    complete = complete_acceptance()

    assert complete.passed
    assert complete.full_evaluation == "Full Evaluation: NOT RUN"
    assert gate.check(settings=Settings(_env_file=None), acceptance=complete, explicitly_enabled=False).reason == "FULL_EVALUATION_DISABLED"
    enabled_settings = Settings(_env_file=None, full_evaluation=True)
    assert gate.check(settings=enabled_settings, acceptance=complete, explicitly_enabled=False).reason == "EXPLICIT_ENABLE_REQUIRED"
    incomplete_task23 = complete_acceptance(task23_passed=False)
    assert gate.check(settings=enabled_settings, acceptance=incomplete_task23, explicitly_enabled=True).reason == "TASK_23_NOT_PASSED"
    extra_agent = complete_acceptance(agent_types=("Main Agent", "Tester Agent", "Verifier Agent"))
    assert not extra_agent.passed
    assert gate.check(settings=enabled_settings, acceptance=extra_agent, explicitly_enabled=True).reason == "FINAL_ACCEPTANCE_INCOMPLETE"
    assert gate.check(settings=enabled_settings, acceptance=complete, explicitly_enabled=True).allowed
