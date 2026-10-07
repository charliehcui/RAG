from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer
from test_architecture_fixes import FakeJev, PlanningClient, task

from demo_app import DemoAppServer
from web_testing_system.config import RunConfig, Settings
from web_testing_system.evaluation.metrics import (
    GroundTruthComparison,
    MetricsCalculator,
)
from web_testing_system.orchestration.runner import run
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.state import StateStore


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_complete_plan_with_explicit_checks_recovers_and_unifies_report(tmp_path: Path, recover: bool) -> None:
    workers = []

    class Worker(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            workers.append(self)

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            assert [tool.name for tool in options["tools"]] == ["execute_test_plan"]
            goal = {"goal": "Inspect both independent forms", "run_operations": False, "checks": [{"action_type": "assertion", "target": "#login-form", "assertion": "visible", "behavior_id": "EB-forms", "check_id": "local.login"}, {"action_type": "assertion", "target": "#register-form", "assertion": "visible", "behavior_id": "EB-forms", "check_id": "local.register"}]}
            if recover and self.calls == 1:
                goal["before_steps"] = [{"action_type": "wait", "target": "#missing-element"}]
            assert self.calls <= (2 if recover else 1)
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(str(self.calls), "execute_test_plan", arguments={"goals": [goal]})])])

    checks = [{"check_id": "local.login", "goal_id": "forms", "behavior_id": "EB-forms", "identity_reference": "admin", "description": "Login form is visible"}, {"check_id": "local.register", "goal_id": "forms", "behavior_id": "EB-forms", "identity_reference": "admin", "description": "Registration form is visible", "depends_on": ["local.login"]}]
    planned = task("forms", requirements={"identity_reference": "admin", "expected_behavior_ids": ["EB-forms"], "check_ids": [item["check_id"] for item in checks]})
    main = PlanningClient([planned])
    with DemoAppServer() as app:
        config = RunConfig(target_url=app.base_url, test_goal="Inspect the two public forms", focus_features=["Project"], allowed_scope=["/"], account_references=[{"identity_reference": "admin", "role": "admin", "permissions": ["read"]}], test_data={}, denied_operations=["production"], expected_behaviors=[{"behavior_id": "EB-forms", "description": "Both public forms are visible", "applies_to": "Project/admin/forms", "source": "USER_PROVIDED"}], required_checks=checks, evaluation_version="local-checks")
        settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs")
        path = await run(config, settings, run_id="local-check-flow", main_client=main, tester_client_factory=Worker, jev_selector=JevSelector(FakeJev()))
    report = json.loads(path.read_text(encoding="utf-8"))
    store = StateStore(settings.state_db_path)
    metrics = MetricsCalculator(store).calculate("local-check-flow", ground_truth=GroundTruthComparison(frozenset(), {}), expected_behavior_ids=frozenset({"EB-forms"}))
    assert workers[0].calls == (2 if recover else 1)
    assert report["test_summary"]["successful_tasks"] == metrics["task_success_rate"] == metrics["e2e_success"] == 1
    assert report["test_summary"]["check_completion"] == "2/2 checks completed"
    assert report["task_outcomes"] == MetricsCalculator(store).task_results("local-check-flow")
    assert all(error["status"] == "RECOVERED" for error in report["task_outcomes"][0]["errors"])
    assert bool(report["task_outcomes"][0]["errors"]) == recover
    assert main.summary_facts["task_outcomes"] == report["task_outcomes"]
