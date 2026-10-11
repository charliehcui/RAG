"""The measurement entry keeps frozen case ordering and failures without real models."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Literal

import pytest

from evaluation import measure
from web_testing_system.config import Settings
from web_testing_system.evaluation.runner import EvaluationExecution


@pytest.mark.parametrize("split,case_count,check_count", [("development", 10, 74), ("holdout", 8, 66)])
async def test_measurement_preserves_cases_budgets_and_valid_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, split: Literal["development", "holdout"], case_count: int, check_count: int) -> None:
    repository = tmp_path / "repository"
    (repository / "evaluation").mkdir(parents=True)
    for name in ("scenarios.json", "ground_truth.json", "evaluation_v2_freeze.json"):
        shutil.copyfile(measure.ROOT / "evaluation" / name, repository / "evaluation" / name)
    monkeypatch.setattr(measure, "ROOT", repository)
    calls: list[dict[str, Any]] = []

    class FakeDemo:
        base_url = "http://app.test"

        def __init__(self, *, bugs: Any) -> None:
            self.state = self
            self.bugs = bugs

        def reset(self) -> None:
            pass

        def __enter__(self) -> FakeDemo:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    class FakeExecutor:
        def __init__(self, config: Any, settings: Settings, **options: Any) -> None:
            calls.append({"config": config, **options})

        async def execute(self, **options: Any) -> EvaluationExecution:
            calls[-1].update(options)
            return EvaluationExecution(status="FAILED" if len(calls) == 1 else "COMPLETED", metrics={"e2e_success": 0 if len(calls) == 1 else 1})

    monkeypatch.setattr(measure, "DemoAppServer", FakeDemo)
    monkeypatch.setattr(measure, "FormalRunExecutor", FakeExecutor)
    for identity in ("ADMIN", "MEMBER", "MEMBER2", "NEW_MEMBER"):
        monkeypatch.setenv(f"DEMO_{identity}_PASSWORD", "restore-after-test")
    settings = Settings(_env_file=None, openrouter_api_key="fake-key", langsmith_api_key="fake-trace-key", langsmith_tracing=True)
    manifest_path = await measure.measure(tmp_path / "measurement", split=split, settings=settings)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    freeze = json.loads((repository / "evaluation/evaluation_v2_freeze.json").read_text(encoding="utf-8"))
    assert [call["scenario_id"] for call in calls] == freeze["sets"][split]["case_ids"]
    assert len(calls) == case_count
    assert sum(record["required_check_count"] for record in manifest["runs"]) == check_count
    assert manifest["runs"][0]["status"] == "FAILED"
    assert all(record["attempt"] == 1 for record in manifest["runs"])
    assert all(record["budget"]["max_browser_steps_per_task"] == 90 for record in manifest["runs"])
    assert all(call["route"].tester_count == 3 for call in calls)
    assert all(call["ground_truth_path"] == repository / "evaluation/ground_truth.json" for call in calls)
    assert manifest["frozen_files_unchanged"] is True
    assert "fake-key" not in manifest_path.read_text(encoding="utf-8")


async def test_measurement_never_overwrites_existing_results(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    original = output / "manifest.json"
    original.write_text("preserve existing results", encoding="utf-8")
    with pytest.raises(FileExistsError):
        await measure.measure(output, split="development", settings=Settings(_env_file=None))
    assert original.read_text(encoding="utf-8") == "preserve existing results"
