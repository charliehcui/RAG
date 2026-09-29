from __future__ import annotations

import json
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from demo_app import DEMO_VERSION, DemoAppServer, SeededBugs


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        payload = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
        request = Request(f"{self.base_url}{path}", data=payload, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def login(self, username: str, password: str) -> None:
        status, _ = self.request("POST", "/api/login", {"username": username, "password": password})
        assert status == 200


@pytest.mark.integration
def test_demo_app_normal_mode_crud_version_reset_and_ground_truth_boundary() -> None:
    with DemoAppServer() as app:
        admin = ApiClient(app.base_url)
        status, version = admin.request("GET", "/version")
        assert status == 200
        assert version == {"version": DEMO_VERSION}

        status, registered = admin.request("POST", "/api/register", {"username": "new-member", "password": "local-test", "display_name": "New Member"})
        assert status == 201
        assert registered["user"]["role"] == "member"
        admin.login("admin", "demo-admin")

        status, created = admin.request("POST", "/api/projects", {"name": "run-1-tester-a-project", "submission_id": "submission-normal"})
        assert status == 201
        project_id = created["project"]["project_id"]
        status, repeated = admin.request("POST", "/api/projects", {"name": "run-1-tester-a-project", "submission_id": "submission-normal"})
        assert status == 201
        assert repeated["project"]["project_id"] == project_id

        status, updated = admin.request("PATCH", f"/api/projects/{project_id}", {"name": "Updated Project"})
        assert status == 200
        assert updated["project"]["name"] == "Updated Project"
        status, task = admin.request("POST", f"/api/projects/{project_id}/tasks", {"title": "Task A"})
        assert status == 201
        task_id = task["task"]["task_id"]
        status, changed_task = admin.request("PATCH", f"/api/projects/{project_id}/tasks/{task_id}", {"title": "Task B", "status": "DONE"})
        assert status == 200
        assert changed_task["task"]["status"] == "DONE"
        assert admin.request("DELETE", f"/api/projects/{project_id}/tasks/{task_id}")[0] == 200
        assert admin.request("GET", f"/api/projects/{project_id}/tasks")[1]["tasks"] == []

        assert admin.request("POST", f"/api/projects/{project_id}/members", {"username": "new-member"})[0] == 201
        status, changed_member = admin.request("PATCH", f"/api/projects/{project_id}/members/new-member", {"display_name": "Updated Member"})
        assert status == 200
        assert changed_member["member"]["display_name"] == "Updated Member"
        assert admin.request("DELETE", f"/api/projects/{project_id}/members/new-member")[0] == 200
        assert admin.request("DELETE", f"/api/projects/{project_id}")[0] == 200
        assert admin.request("POST", "/api/logout")[0] == 200

        with pytest.raises(PermissionError):
            app.ground_truth.read("RUNNING")
        assert app.ground_truth.read("COMPLETED").enabled_bugs == ()
        assert admin.request("POST", "/test/reset")[0] == 200
        assert admin.request("GET", "/api/projects")[0] == 401


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bugs", "check"),
    [
        (SeededBugs(b1_deleted_task_reappears=True), "B1"),
        (SeededBugs(b2_member_deletes_other_project=True), "B2"),
        (SeededBugs(b3_empty_project_name=True), "B3"),
        (SeededBugs(b4_saved_ui_stale=True), "B4"),
        (SeededBugs(b5_double_submit_duplicates=True), "B5"),
        (SeededBugs(b6_removed_member_session_active=True), "B6"),
    ],
)
def test_each_seeded_bug_can_be_enabled_independently(bugs: SeededBugs, check: str) -> None:
    with DemoAppServer(bugs=bugs) as app:
        admin = ApiClient(app.base_url)
        member = ApiClient(app.base_url)
        admin.login("admin", "demo-admin")
        member.login("member", "demo-member")

        if check == "B1":
            assert admin.request("DELETE", "/api/projects/project-1/tasks/task-1")[0] == 200
            assert [task["task_id"] for task in admin.request("GET", "/api/projects/project-1/tasks")[1]["tasks"]] == ["task-1"]
        elif check == "B2":
            assert member.request("DELETE", "/api/projects/project-1")[0] == 200
        elif check == "B3":
            assert member.request("POST", "/api/projects", {"name": "", "submission_id": "empty"})[0] == 201
        elif check == "B4":
            response = admin.request("PATCH", "/api/projects/project-1", {"name": "Saved Name"})[1]
            assert response["project"]["name"] == "Seed Project"
            assert admin.request("GET", "/api/projects/project-1")[1]["project"]["name"] == "Saved Name"
        elif check == "B5":
            first = member.request("POST", "/api/projects", {"name": "Duplicate", "submission_id": "double"})[1]
            second = member.request("POST", "/api/projects", {"name": "Duplicate", "submission_id": "double"})[1]
            assert first["project"]["project_id"] != second["project"]["project_id"]
        elif check == "B6":
            assert admin.request("DELETE", "/api/projects/project-1/members/member")[0] == 200
            assert member.request("GET", "/api/projects/project-1/tasks")[0] == 200

        truth = app.ground_truth.read("COMPLETED")
        assert truth.enabled_bugs == (check,)


@pytest.mark.integration
def test_all_seeded_bugs_off_has_correct_deterministic_behavior() -> None:
    with DemoAppServer() as app:
        admin = ApiClient(app.base_url)
        member = ApiClient(app.base_url)
        admin.login("admin", "demo-admin")
        member.login("member", "demo-member")

        assert member.request("DELETE", "/api/projects/project-1")[0] == 403
        assert member.request("POST", "/api/projects", {"name": "", "submission_id": "empty"})[0] == 400
        assert admin.request("DELETE", "/api/projects/project-1/tasks/task-1")[0] == 200
        assert admin.request("GET", "/api/projects/project-1/tasks")[1]["tasks"] == []
        updated = admin.request("PATCH", "/api/projects/project-1", {"name": "Fresh UI Name"})[1]
        assert updated["project"]["name"] == "Fresh UI Name"
        first = member.request("POST", "/api/projects", {"name": "Single", "submission_id": "same"})[1]
        second = member.request("POST", "/api/projects", {"name": "Single", "submission_id": "same"})[1]
        assert first["project"]["project_id"] == second["project"]["project_id"]
        assert admin.request("DELETE", "/api/projects/project-1/members/member")[0] == 200
        assert member.request("GET", "/api/projects/project-1/tasks")[0] == 403
