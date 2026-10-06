from __future__ import annotations

from collections.abc import Sequence

import pytest
from playwright.async_api import Route, async_playwright

from web_testing_system.config import Settings
from web_testing_system.evidence import EvidenceStore
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.candidates import PageStateReader
from web_testing_system.runtime.computer_use import (
    ComputerUseController,
    ComputerUseDecision,
    ComputerUseRequest,
)
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.state import StateStore

HTML = """
<style>body { margin: 0; }</style>
<canvas id="graph" width="200" height="100"></canvas>
<p id="state">not selected</p>
<script>
  document.querySelector('#graph').addEventListener('click', () => {
    document.querySelector('#state').textContent = 'selected';
  });
</script>
"""


class FakeComputerUseClient:
    def __init__(self, responses: Sequence[ComputerUseDecision | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[ComputerUseRequest] = []

    async def execute(self, request: ComputerUseRequest) -> ComputerUseDecision:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def computer_budget(max_calls: int = 4) -> BudgetGuard:
    return BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=0, max_input_tokens=0, max_output_tokens=0, max_jev_calls=0, max_computer_use_calls=max_calls, max_task_steps=10, max_task_replans=0, max_browser_contexts=1))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_computer_use_is_visual_only_and_returns_to_playwright(phase2_store: StateStore) -> None:
    phase2_store.create_finding(finding_id="finding-visual", run_id="run-1", task_id="task-1", title="Visual graph requires fallback", status="ANOMALY", expected_result="Node is selected", actual_result="Playwright has no stable DOM target", first_seen_by="tester-1", affected_page="/")
    client = FakeComputerUseClient([
        ComputerUseDecision(status="ACTION", action="click", x=50, y=50, risk="LOW", cost=0.001),
        ComputerUseDecision(status="REFUSED", error="model could not identify a safe target"),
        RuntimeError("fake Gemini gateway failure"),
    ])
    budget = computer_budget()
    checker = PermissionChecker(store=phase2_store, policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)), run_id="run-1", task_id="task-1", tester_id="tester-1")
    test_root = phase2_store.database_path.parent
    controller = ComputerUseController(client=client, settings=Settings(_env_file=None, computer_use_provider="relace", computer_use_model="fake-gemini-computer"), store=phase2_store, evidence_store=EvidenceStore(store=phase2_store, artifacts_root=test_root / "artifacts" / "runs", temporary_sensitive_root=test_root / "temporary-sensitive"), page_state_reader=PageStateReader(), permission_checker=checker, budget=budget, budget_id="budget-1", run_id="run-1", task_id="task-1", tester_id="tester-1", max_consecutive_failures=2)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=HTML)

        await page.route("http://app.test/**", handle)
        await page.goto("http://app.test/")
        ordinary = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-visual", goal="Click normal Save button", component_type="button", playwright_failure_reason="selector typo", allowed_visual_actions=("click",))
        success = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-visual", goal="Select the graph node", component_type="canvas", playwright_failure_reason="NO_STABLE_DOM_TARGET", allowed_visual_actions=("click",))
        refused = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-visual", goal="Select another graph node", component_type="canvas", playwright_failure_reason="NO_STABLE_DOM_TARGET", allowed_visual_actions=("click",))
        failed = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-visual", goal="Select another graph node", component_type="canvas", playwright_failure_reason="NO_STABLE_DOM_TARGET", allowed_visual_actions=("click",))
        stopped = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-visual", goal="Try again", component_type="canvas", playwright_failure_reason="NO_STABLE_DOM_TARGET", allowed_visual_actions=("click",))
        await browser.close()

    assert controller.provider == "relace"
    assert controller.model == "fake-gemini-computer"
    assert ordinary.status == "REFUSED"
    assert ordinary.reason == "COMPONENT_MUST_USE_PLAYWRIGHT"
    assert success.status == "RETURN_TO_PLAYWRIGHT"
    assert success.after_state is not None and "selected" in success.after_state.visible_dom
    assert len(success.evidence_ids) == 2
    assert refused.status == "REFUSED"
    assert failed.status == "FAILED"
    assert stopped.status == "STOPPED"
    assert stopped.reason == "MAX_COMPUTER_USE_FAILURES_REACHED"
    assert len(client.requests) == 3
    request = client.requests[0]
    assert set(request.__dataclass_fields__) == {"screenshot", "goal", "url", "allowed_visual_actions", "remaining_budget"}
    assert request.url == "http://app.test/"
    assert request.allowed_visual_actions == ("click",)
    assert request.remaining_budget == 4
    assert budget.usage.computer_use_calls == 3
    budget_row = phase2_store.get_budget("budget-1")
    assert budget_row is not None and budget_row["computer_use_calls"] == 3
    events = phase2_store.list_events("run-1")
    assert sum(event["event_type"] == "COMPUTER_USE_REQUEST" for event in events) == 3
    result_events = [event for event in events if event["event_type"] == "COMPUTER_USE_RESULT"]
    assert [event["result"]["status"] for event in result_events] == ["RETURN_TO_PLAYWRIGHT", "REFUSED", "FAILED"]
    assert result_events[0]["result"]["return_route"] == "PLAYWRIGHT"
    computer_history = [item for item in phase2_store.list_action_history(run_id="run-1", task_id="task-1") if str(item["action"]).startswith("computer_use:")]
    assert len(computer_history) == 3
    assert sum(item["success"] for item in computer_history) == 1
    assert len(phase2_store.list_evidence(run_id="run-1", finding_id="finding-visual")) == 6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_computer_use_budget_blocks_gateway_before_call(phase2_store: StateStore) -> None:
    phase2_store.create_finding(finding_id="finding-budget", run_id="run-1", task_id="task-1", title="Visual budget", status="ANOMALY", expected_result="expected", actual_result="actual", first_seen_by="tester-1")
    client = FakeComputerUseClient([ComputerUseDecision(status="ACTION", action="click", x=1, y=1)])
    test_root = phase2_store.database_path.parent
    controller = ComputerUseController(client=client, settings=Settings(_env_file=None, computer_use_model="fake"), store=phase2_store, evidence_store=EvidenceStore(store=phase2_store, artifacts_root=test_root / "artifacts" / "runs", temporary_sensitive_root=test_root / "temporary-sensitive"), page_state_reader=PageStateReader(), permission_checker=PermissionChecker(store=phase2_store, policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)), run_id="run-1", task_id="task-1", tester_id="tester-1"), budget=computer_budget(max_calls=0), budget_id="budget-1", run_id="run-1", task_id="task-1", tester_id="tester-1")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("http://app.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
        await page.goto("http://app.test/")
        result = await controller.run(page=page, browser_session_id="browser-visual", finding_id="finding-budget", goal="Select graph", component_type="canvas", playwright_failure_reason="NO_STABLE_DOM_TARGET", allowed_visual_actions=("click",))
        await browser.close()

    assert result.status == "STOPPED"
    assert result.reason == "MAX_COMPUTER_USE_CALLS_REACHED"
    assert client.requests == []
