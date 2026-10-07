"""Page-state capture and legal action candidate construction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
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

    async def read(self, page: Page, *, include_content: bool = False) -> PageState:
        body = page.locator("body")
        visible_dom = (await body.inner_text())[: self.max_dom_characters]
        try:
            accessibility = await body.aria_snapshot()
        except Exception:
            accessibility = ""
        load_state = await page.evaluate("() => document.readyState")
        elements = await self._read_elements(page)
        if include_content:
            for row in await self.read_rows(page):
                for cell in row["cells"]:
                    elements.append(InteractiveElement(kind="cell", label=cell["header"], target=cell["target"], context=row["text"], context_target=row["target"]))
        fingerprint_input = "|".join(
            [
                page.url,
                load_state,
                *(
                    f"{item.kind}:{item.label}:{item.target}:{item.href}:{item.context}"
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
        count = await locator.count()
        elements: list[InteractiveElement] = []
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            if not await item.evaluate("element => { const modal = document.querySelector('dialog:modal, [role=dialog][aria-modal=true]'); return !modal || modal.contains(element); }"):
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
            element_id = await item.get_attribute("id")
            if element_id:
                target = f"[id={json.dumps(element_id)}]"
            else:
                aria_label = await item.get_attribute("aria-label")
                if aria_label:
                    proposed = f"{kind}[aria-label={json.dumps(aria_label)}]"
                    if await page.locator(proposed).count() == 1:
                        target = proposed
            context = await item.evaluate("element => (element.closest('tr, dialog, form')?.innerText || '').slice(0, 300)")
            context_target = await item.evaluate("""element => {
                const parent = element.closest('tr, dialog, form');
                if (!parent) return null;
                if (parent.id) return '[id=' + JSON.stringify(parent.id) + ']';
                const attribute = Array.from(parent.attributes).find(item => item.name.startsWith('data-'));
                if (attribute) return parent.tagName.toLowerCase() + '[' + attribute.name + '=' + JSON.stringify(attribute.value) + ']';
                return null;
            }""")
            if kind in {"button", "a"} and label:
                proposed = f"{kind}:text-is({json.dumps(label)})"
                if context_target:
                    proposed = f"{context_target} >> {proposed}"
                if await page.locator(proposed).count() == 1:
                    target = proposed
            elements.append(
                InteractiveElement(
                    kind=kind,
                    label=label[:200],
                    target=target,
                    role=role,
                    href=href,
                    enabled=enabled,
                    context=context,
                    context_target=context_target,
                )
            )
            if len(elements) >= self.max_elements:
                break
        return elements

    async def read_rows(self, page: Page) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await page.locator("tr").evaluate_all("""elements => elements.flatMap((row, index) => {
            if (!row.getClientRects().length) return [];
            const table = row.closest('table');
            const headers = table?.querySelector('thead tr')?.children || [];
            const attribute = Array.from(row.attributes).find(item => item.name.startsWith('data-'));
            let target = 'tr >> nth=' + index;
            if (attribute) target = 'tr[' + attribute.name + '=' + JSON.stringify(attribute.value) + ']';
            if (attribute && table?.id) target = '[id=' + JSON.stringify(table.id) + '] ' + target;
            return [{target, container_target: table?.id ? '[id=' + JSON.stringify(table.id) + ']' : null, text: row.innerText.slice(0, 500), cells: Array.from(row.children).map((cell, position) => ({target: target + ' >> :scope > :nth-child(' + (position + 1) + ')', text: cell.innerText.slice(0, 500), header: headers[position]?.innerText || ''}))}];
        }).slice(0, 12)""")
        return rows


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
            if element.kind == "cell":
                continue
            if not element.enabled:
                continue
            action_type = ActionType.CLICK
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
                value_reference=action.value_reference,
                expected=action.expected,
                assertion=action.assertion,
                behavior_id=action.behavior_id,
                goal_check=action.goal_check,
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
            value_reference=candidate.value_reference,
            expected=candidate.expected,
            assertion=candidate.assertion,
            behavior_id=candidate.behavior_id,
            goal_check=candidate.goal_check,
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
