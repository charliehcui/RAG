"""Independent seeded-bug switches and the post-run Ground Truth boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SeededBugs:
    b1_deleted_task_reappears: bool = False
    b2_member_deletes_other_project: bool = False
    b3_empty_project_name: bool = False
    b4_saved_ui_stale: bool = False
    b5_double_submit_duplicates: bool = False
    b6_removed_member_session_active: bool = False

    @classmethod
    def from_environment(cls) -> SeededBugs:
        def enabled(name: str) -> bool:
            return os.getenv(name, "false").strip().casefold() in {"1", "true", "yes", "on"}

        return cls(
            b1_deleted_task_reappears=enabled("DEMO_BUG_B1"),
            b2_member_deletes_other_project=enabled("DEMO_BUG_B2"),
            b3_empty_project_name=enabled("DEMO_BUG_B3"),
            b4_saved_ui_stale=enabled("DEMO_BUG_B4"),
            b5_double_submit_duplicates=enabled("DEMO_BUG_B5"),
            b6_removed_member_session_active=enabled("DEMO_BUG_B6"),
        )

    def enabled_ids(self) -> tuple[str, ...]:
        values = {
            "B1": self.b1_deleted_task_reappears,
            "B2": self.b2_member_deletes_other_project,
            "B3": self.b3_empty_project_name,
            "B4": self.b4_saved_ui_stale,
            "B5": self.b5_double_submit_duplicates,
            "B6": self.b6_removed_member_session_active,
        }
        return tuple(bug_id for bug_id, is_enabled in values.items() if is_enabled)


@dataclass(frozen=True)
class GroundTruth:
    demo_version: str
    enabled_bugs: tuple[str, ...]


class GroundTruthReader:
    """Allow evaluation code to read seeded bugs only after a Run has ended."""

    FINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"}

    def __init__(self, bugs: SeededBugs, demo_version: str) -> None:
        self._bugs = bugs
        self._demo_version = demo_version

    def read(self, run_status: str) -> GroundTruth:
        if run_status not in self.FINAL_RUN_STATUSES:
            raise PermissionError("Ground Truth is unavailable while the Run is active")
        return GroundTruth(demo_version=self._demo_version, enabled_bugs=self._bugs.enabled_ids())
