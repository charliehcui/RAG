"""Small data objects shared by the web testing runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    NAVIGATION = "navigation"
    CLICK = "click"
    INPUT = "input"
    SELECT = "select"
    KEYBOARD = "keyboard"
    WAIT = "wait"
    REFRESH = "refresh"
    DRAG_AND_DROP = "drag_and_drop"
    DOM_INSPECTION = "dom_inspection"
    URL_CHECK = "url_check"
    ASSERTION = "assertion"


class PermissionDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


@dataclass(frozen=True)
class InteractiveElement:
    kind: str
    label: str
    target: str
    role: str | None = None
    href: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class PageState:
    state_id: str
    url: str
    load_state: str
    visible_dom: str
    accessibility: str
    interactive_elements: tuple[InteractiveElement, ...]

    def necessary_summary(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "url": self.url,
            "load_state": self.load_state,
            "interactive_elements": [
                {
                    "kind": element.kind,
                    "label": element.label,
                    "target": element.target,
                    "role": element.role,
                }
                for element in self.interactive_elements
            ],
        }


@dataclass(frozen=True)
class ActionCandidate:
    candidate_id: str
    action: str
    label: str
    target: str | None
    state_id: str
    value: str | None = None
    url: str | None = None
    requires_confirmation: bool = False
    resource_id: str | None = None
    requires_resource: bool = False
    confirmed: bool = False


@dataclass(frozen=True)
class WebAction:
    action_type: ActionType
    target: str | None = None
    value: str | None = None
    value_reference: str | None = None
    url: str | None = None
    expected: str | None = None
    assertion: str = "contains"
    key: str | None = None
    wait_ms: int = 0
    resource_id: str | None = None
    requires_resource: bool = False
    confirmed: bool = False
    timeout_ms: int = 2_000


@dataclass(frozen=True)
class BusinessAction:
    label: str
    action: WebAction


@dataclass(frozen=True)
class ActionResult:
    started_at: str
    ended_at: str
    latency_ms: float
    success: bool
    error: str | None
    error_type: str | None
    data: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LayaSelection:
    selected_candidate_id: str | None
    confidence: float
    latency_ms: float
    cost: float
    error: str | None = None


@dataclass(frozen=True)
class DecisionResult:
    source: str
    reason: str
    candidate: ActionCandidate | None = None
    action_result: ActionResult | None = None
    needs_tester_llm: bool = False
