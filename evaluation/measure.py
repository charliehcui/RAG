"""Measure one frozen Development or Holdout set through the formal executor."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from demo_app import DemoAppServer, SeededBugs
from web_testing_system.config import Settings
from web_testing_system.evaluation.runner import (
    EvaluationControls,
    EvaluationMode,
    FormalRunExecutor,
    build_evaluation_plan,
    build_execution_route,
)
from web_testing_system.evaluation.scenarios import load_run_config

ROOT = Path(__file__).resolve().parents[1]
BUG_FIELDS = {"B1": "b1_deleted_task_reappears", "B2": "b2_member_deletes_other_project", "B3": "b3_empty_project_name", "B4": "b4_saved_ui_stale", "B5": "b5_double_submit_duplicates", "B6": "b6_removed_member_session_active"}


def frozen_files() -> dict[str, str]:
    paths = []
    for directory in ("src", "demo_app", "tests", "evaluation"):
        for path in (ROOT / directory).rglob("*"):
            if path.suffix in {".py", ".json", ".md"}:
                paths.append(path)
    paths += [ROOT / name for name in ("pyproject.toml", ".python-version", ".env", ".env.example", "README.md")]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths) if path.is_file()}


def save(path: Path, data: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def measure(output: Path, *, split: Literal["development", "holdout"], settings: Settings) -> Path:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    snapshots = frozen_files()
    if not settings.openrouter_api_key or not settings.langsmith_api_key or not settings.langsmith_tracing:
        raise ValueError("OpenRouter and LangSmith credentials and enabled tracing are required")
    dataset = json.loads((ROOT / "evaluation/scenarios.json").read_text(encoding="utf-8"))
    freeze = json.loads((ROOT / "evaluation/evaluation_v2_freeze.json").read_text(encoding="utf-8"))
    cases = [case for case in dataset["scenarios"] if case["split"] == split]
    case_ids = [case["scenario_id"] for case in cases]
    if case_ids != freeze["sets"][split]["case_ids"]:
        raise ValueError("Case set does not match the frozen evaluation contract")
    models = {name: getattr(settings, name) for name in ("main_agent_model", "main_agent_provider", "main_agent_backup_model", "main_agent_backup_provider", "tester_agent_model", "tester_agent_provider", "tester_agent_backup_model", "tester_agent_backup_provider", "jev_model")}
    manifest: dict[str, Any] = {"measurement": f"{split.title()} v2", "evaluation_version": freeze[f"{split}_version"], "started_at_utc": datetime.now(UTC).isoformat(), "case_ids": case_ids, "frozen_files": snapshots, "model_configuration": models, "tracing": {"enabled": True, "project": settings.langsmith_project, "endpoint": settings.langsmith_endpoint}, "status": "RUNNING", "runs": []}
    manifest_path = output / "manifest.json"
    save(manifest_path, manifest)
    for identity, value in {"ADMIN": "demo-admin", "MEMBER": "demo-member", "MEMBER2": "demo-member2", "NEW_MEMBER": "demo-new-member"}.items():
        os.environ[f"DEMO_{identity}_PASSWORD"] = value
    print(json.dumps({"started": manifest["measurement"], "cases": case_ids, "manifest": str(manifest_path)}), flush=True)
    for case in cases:
        if frozen_files() != snapshots:
            raise ValueError("Frozen files changed; stop dispatching cases")
        case_id = case["scenario_id"]
        case_started = perf_counter()
        record: dict[str, Any] = {"case_id": case_id, "run_id": f"measurement-{split}-{case_id}-{output.name[-12:]}", "attempt": 1, "started_at_utc": datetime.now(UTC).isoformat(), "status": "DISPATCHING", "validity": "PENDING_POST_RUN_REVIEW"}
        manifest["runs"].append(record)
        save(manifest_path, manifest)
        print(json.dumps({"case_started": case_id, "run_id": record["run_id"]}), flush=True)
        bugs = SeededBugs(**{field: bug in case["enabled_seeded_bugs"] for bug, field in BUG_FIELDS.items()})
        try:
            with DemoAppServer(bugs=bugs) as app:
                app.state.reset()
                config = load_run_config(ROOT / "evaluation/scenarios.json", scenario_id=case_id, target_url=app.base_url, tester_count=3)
                record.update(budget=config.budget.model_dump(), enabled_seeded_bugs=case["enabled_seeded_bugs"], required_check_count=len(config.required_checks))
                save(manifest_path, manifest)
                case_directory = output / case_id
                case_directory.mkdir()
                save(case_directory / "run_config.json", config.model_dump(mode="json"))
                controls = EvaluationControls(demo_version=dataset["demo_version"], seeded_bugs=tuple(case["enabled_seeded_bugs"]), model_configuration=tuple((key, str(value)) for key, value in models.items()), token_budget=config.budget.max_input_tokens + config.budget.max_output_tokens, time_budget_seconds=config.budget.max_runtime_seconds, accounts=tuple(account.identity_reference for account in config.account_references), initial_data=tuple((key, str(value)) for key, value in config.test_data.items()), test_scope=tuple(config.allowed_scope))
                variant = build_evaluation_plan(EvaluationMode.TESTER_COUNT, controls).variants[1]
                route = build_execution_route(variant, full_evaluation_enabled=False, fake_provider=False)
                executor = FormalRunExecutor(config, settings, scenario_id=case_id, ground_truth_path=ROOT / "evaluation/ground_truth.json")
                execution = await executor.execute(config=variant, route=route, run_number=1, run_id=record["run_id"], evidence_directory=case_directory / record["run_id"] / "evidence")
                record.update(asdict(execution))
        except Exception as error:
            record.update(status="DRIVER_OR_EXECUTOR_FAILURE", error_type=type(error).__name__)
            save(output / f"{case_id}-exception.json", {"type": type(error).__name__, "message": str(error)})
        record.update(wall_seconds=perf_counter() - case_started, finished_at_utc=datetime.now(UTC).isoformat())
        save(manifest_path, manifest)
        print(json.dumps({"case_finished": case_id, "status": record["status"], "wall_seconds": record["wall_seconds"], "metrics": record.get("metrics", {}), "exception_type": record.get("error_type")}), flush=True)
    if frozen_files() != snapshots:
        raise ValueError("Frozen files changed during measurement")
    manifest.update(status="CASE_DISPATCH_COMPLETE", finished_at_utc=datetime.now(UTC).isoformat(), total_wall_seconds=perf_counter() - started, frozen_files_unchanged=True)
    save(manifest_path, manifest)
    print(json.dumps({"finished": manifest["status"], "total_wall_seconds": manifest["total_wall_seconds"]}), flush=True)
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure a frozen evaluation set once with real models")
    parser.add_argument("split", choices=("development", "holdout"))
    parser.add_argument("output", type=Path, help="New output directory; existing results are never overwritten")
    arguments = parser.parse_args()
    asyncio.run(measure(arguments.output, split=arguments.split, settings=Settings()))


if __name__ == "__main__":
    main()
