from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web_testing_system.config import RunConfig, Settings
from web_testing_system.evaluation import (
    AcceptanceChecklist,
    EvaluationControls,
    EvaluationExecution,
    EvaluationMode,
    EvaluationRunner,
    EvaluationRunRecord,
    FormalRunExecutor,
    FullEvaluationGate,
    FullEvaluationGateResult,
    build_evaluation_plan,
    build_execution_route,
    summarize_evaluation_runs,
)
from web_testing_system.evaluation.matching import match_ground_truth
from web_testing_system.evaluation.scenarios import load_run_config
from web_testing_system.state import StateStore


def make_controls() -> EvaluationControls:
    return EvaluationControls(demo_version="demo-1", seeded_bugs=("B1",), model_configuration=(("main", "fake-gemini"), ("tester", "fake-groq")), token_budget=100, time_budget_seconds=30, accounts=("admin", "member"), initial_data=(("project", "project-1"),), test_scope=("Project", "Task"))


def test_formal_scenario_uses_model_headroom_and_keeps_runaway_limits() -> None:
    path = Path(__file__).resolve().parents[2] / "evaluation/scenarios.json"
    original = path.read_bytes()
    config = load_run_config(path, scenario_id="D03", target_url="http://127.0.0.1:8000")
    assert config.budget.max_input_tokens == 10_000_000
    assert config.budget.max_output_tokens == 1_000_000
    assert config.budget.max_llm_calls == 500
    assert config.budget.max_runtime_seconds == 900
    assert config.budget.max_browser_steps_per_task == 90
    assert config.budget.max_replans_per_task == 2
    assert config.budget.max_testers == config.budget.max_parallel_browser_contexts == 3
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["canonical", "annotated", "wrong_page", "unverified_page", "wrong_role", "wrong_behavior", "wrong_identity", "wrong_entity", "owner_delete", "denied_delete", "project_remains", "unstable"])
def test_matching_annotated_page_requires_verified_path_and_exact_bug_evidence(tmp_path: Path, mode: str) -> None:
    bug = {"bug_id": "B2", "behavior_id": "EB-PROJECT-DELETE-AUTH", "applicable_role": "member", "identity_reference": "member", "page": "/", "project_reference": "project-1"}
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"demo_version": "demo-1", "scenarios": [{"scenario_id": "D05", "bugs": [bug], "enabled_bug_ids": ["B2"]}]}), encoding="utf-8")
    steps = [{"action_type": "navigation", "url": "http://app.test/", "identity_reference": "member"}, {"action_type": "assertion", "behavior_id": "EB-PROJECT-DELETE-AUTH", "identity_reference": "member"}]
    results = [{"success": True, "data": {"url": "http://app.test/"}}, {"success": False, "error_type": "ASSERTION_FAILURE", "data": {"matched": False}}]
    finding = {"finding_id": "finding", "task_id": "task", "status": "CONFIRMED_BUG", "verification_result": "FAIL", "reproduction_success_count": 2, "verification_details": {"attempt_id": "verify"}, "reproduction_steps": steps, "affected_role": "member", "affected_page": "/ (Projects list) and /api/projects"}
    records = [{"identity_reference": "member", "url": "http://app.test/api/login", "response_data": {"user": {"username": "member"}}, "captured_at": "1"}, {"identity_reference": "member", "url": "http://app.test/api/projects", "method": "GET", "response_data": {"projects": [{"project_id": "project-1", "owner": "admin"}]}, "captured_at": "2"}, {"identity_reference": "member", "url": "http://app.test/api/projects/project-1", "method": "DELETE", "status": 200, "captured_at": "3"}, {"identity_reference": "member", "url": "http://app.test/api/projects", "method": "GET", "response_data": {"projects": []}, "captured_at": "4"}]
    if mode == "canonical":
        finding["affected_page"] = "/"
    elif mode == "wrong_page":
        finding["affected_page"] = "/wrong (Projects list) and /"
    elif mode == "unverified_page":
        results[0]["data"]["url"] = "http://app.test/wrong"
    elif mode == "wrong_role":
        finding["affected_role"] = "admin"
    elif mode == "wrong_behavior":
        steps[-1]["behavior_id"] = "EB-PROJECT-SAVE"
    elif mode == "wrong_identity":
        steps[-1]["identity_reference"] = "admin"
    elif mode == "wrong_entity":
        records[2]["url"] = "http://app.test/api/projects/project-2"
    elif mode == "owner_delete":
        records[1]["response_data"]["projects"][0]["owner"] = "member"
    elif mode == "denied_delete":
        records[2]["status"] = 403
    elif mode == "project_remains":
        records[-1]["response_data"]["projects"] = [{"project_id": "project-1"}]
    elif mode == "unstable":
        finding["reproduction_success_count"] = 1
    (tmp_path / "network.json").write_text(json.dumps(records), encoding="utf-8")
    store = MagicMock(spec=StateStore)
    store.get_run.return_value = {"status": "COMPLETED", "finished_at": "finished", "application_version": "demo-1"}
    store.list_recent_findings.return_value = [finding]
    store.list_events.return_value = [{"result": {"finding_id": "finding", "attempt_id": "verify", "action_results": results, "assertion_positions": [1]}, "evidence_references": ["network"]}]
    store.list_evidence.return_value = [{"evidence_type": "NETWORK", "evidence_id": "network", "relative_file_path": "network.json", "browser_session_id": "fresh-session"}]
    _, matching = match_ground_truth(answers, scenario_id="D05", run_id="run", store=store, artifacts_root=tmp_path, test_data={})
    expected = "B2" if mode in {"canonical", "annotated"} else "unmatched"
    assert matching["finding_to_bug"] == {"finding": expected}
    assert matching["tp"] == int(expected == "B2")
    assert matching["fp"] == int(expected == "unmatched")


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
    checker = AcceptanceChecklist()
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
    assert [route.tester_count for route in tester_routes] == [1, 3]
    decision_routes = [build_execution_route(variant, full_evaluation_enabled=False) for variant in plans[EvaluationMode.DECISION_ENGINE].variants]
    assert [route.candidate_selection for route in decision_routes] == ["JEV", "TESTER_LLM_EVERY_DECISION"]
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
    model_every_step_route = build_execution_route(action_variants[1], full_evaluation_enabled=True)
    assert model_every_step_route.known_action == "TESTER_LLM_EVERY_STEP"
    assert model_every_step_route.candidate_selection == "TESTER_LLM_EVERY_DECISION"
    assert model_every_step_route.full_evaluation is True


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


@pytest.mark.asyncio
async def test_all_fake_evaluation_modes_dispatch_to_formal_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = RunConfig.model_validate({"target_url": "http://app.test/", "test_goal": "Test scoped app", "focus_features": ["Project"], "allowed_scope": ["/"], "account_references": [{"identity_reference": "admin", "role": "admin", "permissions": ["read"]}], "test_data": {}, "denied_operations": ["production deletion"]})
    settings = Settings(_env_file=None, full_evaluation=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs")
    routes: list[object] = []
    database_paths: list[Path] = []

    async def fake_run(run_config: RunConfig, run_settings: Settings, **kwargs: object) -> Path:
        assert run_config is config
        assert run_settings.state_db_path == run_settings.artifacts_dir / "state.db"
        routes.append(kwargs["route"])
        database_paths.append(run_settings.state_db_path)
        path = run_settings.artifacts_dir / str(kwargs["run_id"]) / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"test_summary": {"run_status": "COMPLETED"}}), encoding="utf-8")
        return path

    monkeypatch.setattr("web_testing_system.orchestration.runner.run", fake_run)
    monkeypatch.setattr("web_testing_system.evaluation.runner.MetricsCalculator.calculate", lambda self, run_id, **kwargs: {"score": 1.0})
    executor = FormalRunExecutor(config, settings, main_client_factory=lambda: object(), tester_client_factory=lambda: object(), jev_selector_factory=lambda: object(), computer_use_client_factory=lambda: object())  # type: ignore[arg-type]
    assert executor.is_fake

    async def reset() -> None:
        return None

    for mode in EvaluationMode:
        plan = build_evaluation_plan(mode, make_controls())
        records = await EvaluationRunner(tmp_path / "evaluation").run_fake_sample(plan=plan, executor=executor, reset=reset)
        assert len(records) == 2 and all(record.status == "COMPLETED" for record in records)

    assert len(routes) == 12
    assert len(set(database_paths)) == 12
    assert {route.tester_count for route in routes[:2]} == {1, 3}
    assert {route.known_action for route in routes[8:10]} == {"PLAYWRIGHT", "TESTER_LLM_EVERY_STEP"}
    assert settings.full_evaluation is False
