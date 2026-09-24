"""Scope, permission, and registered-resource checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from web_testing_system.runtime.models import ActionType, PermissionDecision, WebAction
from web_testing_system.state import StateStore


@dataclass(frozen=True)
class ExecutionPolicy:
    allowed_url_prefixes: tuple[str, ...]
    permissions: dict[ActionType, PermissionDecision] = field(default_factory=dict)
    denied_operations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PermissionCheck:
    allowed: bool
    decision: PermissionDecision
    code: str
    reason: str


class PermissionChecker:
    def __init__(
        self,
        *,
        store: StateStore,
        policy: ExecutionPolicy,
        run_id: str,
        task_id: str,
        tester_id: str,
    ) -> None:
        self.store = store
        self.policy = policy
        self.run_id = run_id
        self.task_id = task_id
        self.tester_id = tester_id

    def check(
        self, action: WebAction, *, current_url: str | None = None
    ) -> PermissionCheck:
        if action.action_type.value in self.policy.denied_operations:
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "PERMISSION_DENIED",
                "operation is explicitly denied",
            )
        checked_url = action.url or current_url
        if checked_url is not None and not self.url_allowed(checked_url):
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "SCOPE_VIOLATION",
                "URL is outside the allowed scope",
            )
        permission = self.policy.permissions.get(
            action.action_type, PermissionDecision.ALLOW
        )
        if permission == PermissionDecision.DENY:
            return PermissionCheck(
                False, permission, "PERMISSION_DENIED", "operation is denied"
            )
        if (
            permission == PermissionDecision.REQUIRE_CONFIRMATION
            and not action.confirmed
        ):
            return PermissionCheck(
                False,
                permission,
                "REQUIRE_CONFIRMATION",
                "operation requires confirmation",
            )
        resource_check = self._check_resource(action)
        if resource_check is not None:
            return resource_check
        return PermissionCheck(True, permission, "ALLOW", "operation is allowed")

    def url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        for prefix in self.policy.allowed_url_prefixes:
            allowed = urlparse(prefix)
            if (
                allowed.scheme not in {"http", "https"}
                or parsed.scheme != allowed.scheme
                or parsed.netloc.lower() != allowed.netloc.lower()
            ):
                continue
            allowed_path = allowed.path or "/"
            candidate_path = parsed.path or "/"
            if allowed_path.endswith("/"):
                if candidate_path.startswith(allowed_path):
                    return True
            elif candidate_path == allowed_path or candidate_path.startswith(
                f"{allowed_path}/"
            ):
                return True
        return False

    def _check_resource(self, action: WebAction) -> PermissionCheck | None:
        if action.requires_resource and action.resource_id is None:
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "RESOURCE_NOT_REGISTERED",
                "operation requires a registered resource",
            )
        if action.resource_id is None:
            return None
        resource = self.store.get_resource(action.resource_id)
        if resource is None or resource["run_id"] != self.run_id:
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "RESOURCE_NOT_REGISTERED",
                "resource is not registered for this run",
            )
        if (
            action.action_type.value not in resource["allowed_operations"]
            and "*" not in resource["allowed_operations"]
        ):
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "RESOURCE_OPERATION_DENIED",
                "operation is not allowed for this resource",
            )
        if resource["sharing_mode"] == "ISOLATED" and (
            resource["owner_task"] != self.task_id
            or resource["owner"] != self.tester_id
        ):
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "RESOURCE_OWNER_MISMATCH",
                "isolated resource belongs to another task or tester",
            )
        if resource["sharing_mode"] == "SHARED" and self.tester_id not in resource[
            "participants"
        ]:
            return PermissionCheck(
                False,
                PermissionDecision.DENY,
                "RESOURCE_PARTICIPANT_MISSING",
                "tester is not a registered participant",
            )
        return None
