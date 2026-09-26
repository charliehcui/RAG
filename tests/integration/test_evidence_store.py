from __future__ import annotations

import zipfile

import pytest
from playwright.async_api import Route, async_playwright

from web_testing_system.evidence import EvidenceBuffer, EvidenceStore
from web_testing_system.state import StateStore

HTML = """
<main>
  <h1>Evidence page</h1>
  <input type="password" value="real-secret">
  <p>token=real-secret</p>
  <script>console.error("password=real-secret")</script>
</main>
"""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_critical_evidence_types_are_saved_and_sensitive_text_is_redacted(phase2_store: StateStore) -> None:
    phase2_store.create_finding(finding_id="finding-evidence", run_id="run-1", task_id="task-1", title="Evidence test", status="ANOMALY", expected_result="expected", actual_result="actual", first_seen_by="tester-1")
    test_root = phase2_store.database_path.parent
    artifacts_root = test_root / "artifacts" / "runs"
    evidence_store = EvidenceStore(store=phase2_store, artifacts_root=artifacts_root, temporary_sensitive_root=test_root / "temporary-sensitive", secrets=("real-secret",))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        buffer = EvidenceBuffer()
        buffer.attach(page)

        async def handle(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=HTML)

        await page.route("http://app.test/**", handle)
        await evidence_store.start_trace(context)
        marker = buffer.mark()
        await page.goto("http://app.test/?token=real-secret")
        screenshot, _ = await evidence_store.capture_screenshot(page=page, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1", name="page")
        dom = await evidence_store.capture_dom(page=page, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1")
        accessibility = await evidence_store.capture_accessibility(page=page, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1")
        network = evidence_store.capture_network(buffer=buffer, marker=marker, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1", url=page.url, relevant_url="app.test")
        console = evidence_store.capture_console(buffer=buffer, marker=marker, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1", url=page.url)
        trace = await evidence_store.capture_trace(context=context, run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="attempt-1", browser_session_id="browser-1", url=page.url)
        await browser.close()

    evidence = [screenshot, dom, accessibility, network, console, trace]
    assert {item["evidence_type"] for item in evidence} == {"SCREENSHOT", "DOM", "ACCESSIBILITY", "NETWORK", "CONSOLE", "TRACE"}
    assert all(item["attempt_id"] == "attempt-1" for item in evidence)
    for item in evidence:
        path = artifacts_root / str(item["relative_file_path"])
        assert path.is_file()
        assert "run-1/evidence/finding-evidence/attempt-1" in item["relative_file_path"]
    for item in (dom, accessibility, network, console):
        content = (artifacts_root / str(item["relative_file_path"])).read_text(encoding="utf-8")
        assert "real-secret" not in content
    trace_path = artifacts_root / str(trace["relative_file_path"])
    with zipfile.ZipFile(trace_path) as trace_zip:
        assert all("network" not in name.casefold() and not name.startswith("resources/") for name in trace_zip.namelist())
        assert "real-secret" not in b"".join(trace_zip.read(name) for name in trace_zip.namelist()).decode("utf-8", errors="ignore")

    metadata_count = len(phase2_store.list_evidence(run_id="run-1"))
    blocked_root = test_root / "blocked-root"
    blocked_root.write_text("not a directory", encoding="utf-8")
    blocked_store = EvidenceStore(store=phase2_store, artifacts_root=blocked_root, temporary_sensitive_root=test_root / "temporary-sensitive")
    with pytest.raises(OSError):
        blocked_store.save_text(content="failure", extension="txt", evidence_type="DOM", run_id="run-1", task_id="task-1", finding_id="finding-evidence", attempt_id="failed", browser_session_id="browser-1", url="http://app.test/", name="dom")
    assert len(phase2_store.list_evidence(run_id="run-1")) == metadata_count
