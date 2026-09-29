from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents import MainAgentRunner, MainAgentTools, create_main_agent
from web_testing_system.config import Settings
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.state import StateStore


class FakeSummaryClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self) -> None:
        self.messages: list[str] = []
        super().__init__()

    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        del stream, options, kwargs
        self.messages.append("\n".join(message.text for message in messages))
        return ChatResponse(messages=[Message(role="assistant", contents=["The recorded run confirmed one persistence bug with one Tester."])], usage_details={"input_token_count": 2, "output_token_count": 1})


def create_report_store(path: Path) -> StateStore:
    store = StateStore(path)
    store.initialize()
    store.create_run(run_id="run-report", application="http://demo.test", application_version="demo-1", test_goal="Test task persistence", scope={"focus_features": ["Task", "Permission"], "allowed_scope": ["/"]}, status="RUNNING", global_budget={"browser_steps": 10}, remaining_budget={"browser_steps": 5})
    store.create_identity(identity_id="identity-a", run_id="run-report", role="member", secret_reference="env:DEMO_MEMBER_PASSWORD", permissions=["read", "delete"])
    store.register_tester(tester_id="tester-a", run_id="run-report", session_reference="fake-session-a", identity_id="identity-a", role="member", data_namespace="run-report-tester-a-task-delete")
    store.create_task(task_id="task-delete", run_id="run-report", goal="Test Task deletion", priority="P0", dependencies=[], created_by="main-agent", step_budget=5, data_requirements={"feature": "Task"})
    store.claim_task(task_id="task-delete", tester_id="tester-a")
    store.update_task_status(task_id="task-delete", expected_status="RUNNING", new_status="COMPLETED")
    store.create_budget(budget_id="budget-delete", run_id="run-report", task_id="task-delete")
    store.update_budget(budget_id="budget-delete", llm_calls=1, jev_calls=1, input_tokens=2, output_tokens=1, browser_steps=4, runtime_seconds=1.5, estimated_cost=0.01)
    store.record_path(run_id="run-report", feature="Task", page="/tasks", state="deleted", action="refresh", result="FAILED_ASSERTION", last_tester="tester-a")
    steps = [{"action_type": "refresh", "expected_success": True}, {"action_type": "assertion", "target": "#task-1", "assertion": "hidden", "expected_success": False}]
    store.create_finding(finding_id="finding-b1", run_id="run-report", task_id="task-delete", title="Deleted Task reappears after refresh", status="REPRODUCED", expected_result="Deleted Task remains absent after refresh", actual_result="Deleted Task is visible again", first_seen_by="tester-a", severity_hint="HIGH", affected_role="member", affected_page="/tasks", reproduction_steps=steps)
    store.record_reproduction_attempt(finding_id="finding-b1", status="REPRODUCED", success=True, stable_steps=steps)
    store.update_finding_verification(finding_id="finding-b1", status="CONFIRMED_BUG", verification_result="FAIL", details={"reason": "task remained visible"})
    store.create_finding(finding_id="finding-needs-confirmation", run_id="run-report", task_id="task-delete", title="Unclear response", status="NEEDS_CONFIRMATION", expected_result="", actual_result="response contained fake-report-secret", first_seen_by="tester-a", needs_confirmation=True, screening_reason="EXPECTED_BEHAVIOR_MISSING")
    store.add_evidence(evidence_id="evidence-b1", run_id="run-report", task_id="task-delete", finding_id="finding-b1", evidence_type="SCREENSHOT", relative_file_path="run-report/evidence/finding-b1/verification.png", url="http://demo.test/tasks", browser_session_id="browser-a", attempt_id="verify-1")
    store.append_event(event_id="browser-event", run_id="run-report", task_id="task-delete", tester_id="tester-a", browser_session_id="browser-a", event_type="BROWSER_ACTION", url="http://demo.test/tasks", tool="Playwright", action="refresh", result={"success": True}, latency_ms=12)
    store.append_event(event_id="laya-event", run_id="run-report", task_id="task-delete", tester_id="tester-a", browser_session_id="browser-a", event_type="LAYA_CALL", url="http://demo.test", tool="Laya", action="select_candidate", result={"selected": "Tasks"}, latency_ms=3)
    store.finish_run(run_id="run-report", status="COMPLETED")
    return store


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_report_uses_state_facts_and_existing_main_agent_only_summarizes(tmp_path: Path) -> None:
    store = create_report_store(tmp_path / "report.db")
    report = FinalReportBuilder(store, secrets=("fake-report-secret",)).build("run-report", full_evaluation=False)

    assert report["test_summary"]["version"] == "demo-1"
    assert report["test_summary"]["tester_count"] == 1
    assert report["test_summary"]["completed_tasks"] == 1
    assert report["coverage_summary"]["features_tested"] == ["Task"]
    assert report["coverage_summary"]["untested_registered_targets"] == ["Permission"]
    assert report["confirmed_bugs"][0]["verification"] == "FAIL"
    assert report["confirmed_bugs"][0]["evidence"][0]["evidence_id"] == "evidence-b1"
    assert report["needs_confirmation"][0]["observed_behavior"] == "response contained [REDACTED]"
    assert report["cost_and_performance"]["cost_per_confirmed_bug"] == pytest.approx(0.01)
    assert report["full_evaluation"] == "Full Evaluation: NOT RUN"

    client = FakeSummaryClient()
    tools = MainAgentTools(store=store, run_id="run-report", target_url="http://demo.test", focus_features=["Task", "Permission"], allowed_scope=["/"], denied_operations=["production delete"], max_step_budget=5)
    agent = create_main_agent(client=client, settings=Settings(_env_file=None, main_agent_provider="gemini", main_agent_model="fake-gemini"), tools=tools)
    runner = MainAgentRunner(agent=agent, tools=tools, run_id="run-report", max_replans=0)
    summary = await runner.summarize_report(report)

    assert summary.text == "The recorded run confirmed one persistence bug with one Tester."
    assert "Deleted Task reappears after refresh" in client.messages[0]
    assert "Ground Truth" not in client.messages[0]
    summary_event = next(event for event in store.list_events("run-report") if event["event_type"] == "FINAL_REPORT_SUMMARY")
    assert summary_event["result"]["summary"] == summary.text
    assert "demo-member" not in json.dumps(report)
    assert "fake-report-secret" not in json.dumps(report)
