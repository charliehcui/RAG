"""Page-state capture and legal action candidate construction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urljoin

from playwright.async_api import Page

from web_testing_system.runtime.models import (
    ActionCandidate,
    ActionType,
    BusinessAction,
    InteractiveElement,
    PageState,
    WebAction,
)
from web_testing_system.runtime.permissions import PermissionChecker

INTERACTIVE_SELECTOR = "button, a[href], input, select, textarea, [role='button'], [role='menuitem'], [role='tab'], dialog button"


@dataclass(frozen=True)
class CandidateValidation:
    valid: bool
    reason: str
    action: WebAction | None = None


class PageStateReader:
    def __init__(
        self, max_elements: int = 30, max_dom_characters: int = 10_000
    ) -> None:
        self.max_elements = max_elements
        self.max_dom_characters = max_dom_characters

    async def read(self, page: Page) -> PageState:
        body = page.locator("body")
        visible_dom = (await body.inner_text())[: self.max_dom_characters]
        try:
            accessibility = await body.aria_snapshot()
        except Exception:
            accessibility = ""
        load_state = await page.evaluate("() => document.readyState")
        elements = await self._read_elements(page)
        fingerprint_input = "|".join(
            [
                page.url,
                load_state,
                *(
                    f"{item.kind}:{item.label}:{item.target}:{item.href}"
                    for item in elements
                ),
            ]
        )
        state_id = sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
        return PageState(
            state_id=state_id,
            url=page.url,
            load_state=load_state,
            visible_dom=visible_dom,
            accessibility=accessibility,
            interactive_elements=tuple(elements),
        )

    async def _read_elements(self, page: Page) -> list[InteractiveElement]:
        locator = page.locator(INTERACTIVE_SELECTOR)
        count = min(await locator.count(), self.max_elements)
        elements: list[InteractiveElement] = []
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            kind = await item.evaluate("element => element.tagName.toLowerCase()")
            role = await item.get_attribute("role")
            href = await item.get_attribute("href")
            label = (
                await item.get_attribute("aria-label")
                or await item.get_attribute("placeholder")
                or await item.inner_text()
                or await item.get_attribute("name")
                or kind
            ).strip()
            enabled = await item.is_enabled()
            target = f"{INTERACTIVE_SELECTOR} >> nth={index}"
            elements.append(
                InteractiveElement(
                    kind=kind,
                    label=label[:200],
                    target=target,
                    role=role,
                    href=href,
                    enabled=enabled,
                )
            )
        return elements


class CandidateBuilder:
    def __init__(
        self, permission_checker: PermissionChecker, max_candidates: int = 8
    ) -> None:
        self.permission_checker = permission_checker
        self.max_candidates = max_candidates

    def build(
        self,
        *,
        goal: str,
        page_state: PageState,
        include_controls: bool = True,
        business_actions: Sequence[BusinessAction] = (),
        explored_targets: set[str] | None = None,
    ) -> list[ActionCandidate]:
        goal_words = {word.lower() for word in goal.split() if len(word) > 2}
        explored_targets = explored_targets or set()
        ranked: list[tuple[int, ActionCandidate]] = []
        for element in page_state.interactive_elements:
            if not element.enabled:
                continue
            action_type = self._action_for_element(element)
            candidate_url = (
                urljoin(page_state.url, element.href) if element.href else None
            )
            action = WebAction(
                action_type=action_type, target=element.target, url=candidate_url
            )
            permission = self.permission_checker.check(
                action, current_url=page_state.url
            )
            if permission.decision.value == "DENY":
                continue
            candidate_id = self._candidate_id(
                page_state.state_id, action_type.value, element.target
            )
            candidate = ActionCandidate(
                candidate_id=candidate_id,
                action=action_type.value,
                label=element.label,
                target=element.target,
                state_id=page_state.state_id,
                url=candidate_url,
                requires_confirmation=not permission.allowed,
            )
            label_words = {
                word.lower() for word in element.label.split() if len(word) > 2
            }
            unexplored_score = 1 if element.target not in explored_targets else 0
            ranked.append(
                (len(goal_words & label_words) * 2 + unexplored_score, candidate)
            )
        for business_action in business_actions:
            action = business_action.action
            permission = self.permission_checker.check(
                action, current_url=page_state.url
            )
            if permission.decision.value == "DENY":
                continue
            if action.target is not None and not any(
                element.target == action.target and element.enabled
                for element in page_state.interactive_elements
            ):
                continue
            target_key = action.target or action.url or action.action_type.value
            candidate = ActionCandidate(
                candidate_id=self._candidate_id(
                    page_state.state_id, action.action_type.value, target_key
                ),
                action=action.action_type.value,
                label=business_action.label,
                target=action.target,
                state_id=page_state.state_id,
                value=action.value,
                url=action.url,
                requires_confirmation=not permission.allowed,
                resource_id=action.resource_id,
                requires_resource=action.requires_resource,
                confirmed=action.confirmed,
            )
            label_words = {
                word.lower() for word in business_action.label.split() if len(word) > 2
            }
            unexplored_score = 1 if target_key not in explored_targets else 0
            ranked.append(
                (len(goal_words & label_words) * 2 + unexplored_score, candidate)
            )
        ranked.sort(key=lambda item: (-item[0], item[1].candidate_id))
        candidates = [item[1] for item in ranked[: self.max_candidates]]
        if include_controls:
            candidates.extend(self._control_candidates(page_state.state_id))
        return candidates

    def validate(
        self, *, candidate: ActionCandidate, current_page_state: PageState
    ) -> CandidateValidation:
        if candidate.state_id != current_page_state.state_id:
            return CandidateValidation(False, "CANDIDATE_EXPIRED")
        if candidate.action in {"request_replan", "stop_current_path"}:
            return CandidateValidation(True, "CONTROL_CANDIDATE")
        try:
            action_type = ActionType(candidate.action)
        except ValueError:
            return CandidateValidation(False, "FORGED_ACTION")
        target_actions = {
            ActionType.CLICK,
            ActionType.INPUT,
            ActionType.SELECT,
            ActionType.DRAG_AND_DROP,
            ActionType.DOM_INSPECTION,
            ActionType.ASSERTION,
        }
        if candidate.target is None and action_type in target_actions:
            return CandidateValidation(False, "TARGET_MISSING")
        if candidate.target is not None:
            matching_target = next(
                (
                    element
                    for element in current_page_state.interactive_elements
                    if element.target == candidate.target and element.enabled
                ),
                None,
            )
            if matching_target is None:
                return CandidateValidation(False, "TARGET_INVALID")
        action = WebAction(
            action_type=action_type,
            target=candidate.target,
            value=candidate.value,
            url=candidate.url,
            resource_id=candidate.resource_id,
            requires_resource=candidate.requires_resource,
            confirmed=candidate.confirmed,
        )
        permission = self.permission_checker.check(
            action, current_url=current_page_state.url
        )
        if not permission.allowed:
            return CandidateValidation(False, permission.code)
        return CandidateValidation(True, "VALID", action)

    @staticmethod
    def _action_for_element(element: InteractiveElement) -> ActionType:
        if element.kind == "input" or element.kind == "textarea":
            return ActionType.CLICK
        return ActionType.CLICK

    @staticmethod
    def _candidate_id(state_id: str, action: str, target: str) -> str:
        value = sha256(f"{state_id}:{action}:{target}".encode()).hexdigest()[:12]
        return f"candidate-{value}"

    @staticmethod
    def _control_candidates(state_id: str) -> list[ActionCandidate]:
        return [
            ActionCandidate(
                candidate_id=f"candidate-replan-{state_id}",
                action="request_replan",
                label="Request Replan",
                target=None,
                state_id=state_id,
            ),
            ActionCandidate(
                candidate_id=f"candidate-stop-{state_id}",
                action="stop_current_path",
                label="Stop Current Path",
                target=None,
                state_id=state_id,
            ),
        ]
