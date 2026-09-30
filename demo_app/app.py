"""Small local Web App used by the multi-Agent browser-testing demo."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from demo_app.bugs import GroundTruthReader, SeededBugs

DEMO_VERSION = "phase5-demo-1.0"
SESSION_COOKIE = "demo_session"


@dataclass
class DemoSession:
    username: str
    role: str
    project_access_snapshot: set[str]


class DemoState:
    """Keep only the small amount of state needed by the local demo."""

    def __init__(self, bugs: SeededBugs) -> None:
        self.bugs = bugs
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.users: dict[str, dict[str, str]] = {
                "admin": {"username": "admin", "password": "demo-admin", "role": "admin", "display_name": "Demo Admin"},
                "member": {"username": "member", "password": "demo-member", "role": "member", "display_name": "Demo Member"},
                "member2": {"username": "member2", "password": "demo-member2", "role": "member", "display_name": "Second Member"},
            }
            self.projects: dict[str, dict[str, Any]] = {
                "project-1": {"project_id": "project-1", "name": "Seed Project", "owner": "admin", "members": ["member"]},
                "project-2": {"project_id": "project-2", "name": "Admin Private Project", "owner": "admin", "members": []},
            }
            self.tasks: dict[str, dict[str, Any]] = {
                "task-1": {"task_id": "task-1", "project_id": "project-1", "title": "Seed Task", "status": "OPEN"}
            }
            self.sessions: dict[str, DemoSession] = {}
            self.project_submissions: dict[str, str] = {}
            self.next_project = 3
            self.next_task = 2

    def register(self, username: str, password: str, display_name: str) -> dict[str, str]:
        username = username.strip()
        if not username or not password:
            raise DemoError(HTTPStatus.BAD_REQUEST, "username and password are required")
        with self.lock:
            if username in self.users:
                raise DemoError(HTTPStatus.CONFLICT, "username already exists")
            user = {"username": username, "password": password, "role": "member", "display_name": display_name.strip() or username}
            self.users[username] = user
            return self.public_user(user)

    def login(self, username: str, password: str) -> tuple[str, dict[str, str]]:
        with self.lock:
            user = self.users.get(username)
            if user is None or user["password"] != password:
                raise DemoError(HTTPStatus.UNAUTHORIZED, "invalid login")
            accessible = {project_id for project_id, project in self.projects.items() if self._has_live_access(user, project)}
            token = uuid4().hex
            self.sessions[token] = DemoSession(username=username, role=user["role"], project_access_snapshot=accessible)
            return token, self.public_user(user)

    def logout(self, token: str | None) -> None:
        if token is not None:
            with self.lock:
                self.sessions.pop(token, None)

    def get_session(self, token: str | None) -> DemoSession:
        with self.lock:
            session = self.sessions.get(token or "")
            if session is None:
                raise DemoError(HTTPStatus.UNAUTHORIZED, "login required")
            return session

    def list_projects(self, session: DemoSession) -> list[dict[str, Any]]:
        with self.lock:
            return [self.public_project(project) for project in self.projects.values() if self._has_access(session, project)]

    def get_project(self, session: DemoSession, project_id: str) -> dict[str, Any]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            return self.public_project(project)

    def create_project(self, session: DemoSession, name: str, submission_id: str | None) -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name and not self.bugs.b3_empty_project_name:
            raise DemoError(HTTPStatus.BAD_REQUEST, "project name is required")
        with self.lock:
            if submission_id and not self.bugs.b5_double_submit_duplicates and submission_id in self.project_submissions:
                return self.public_project(self.projects[self.project_submissions[submission_id]])
            project_id = f"project-{self.next_project}"
            self.next_project += 1
            project = {"project_id": project_id, "name": normalized_name, "owner": session.username, "members": []}
            self.projects[project_id] = project
            session.project_access_snapshot.add(project_id)
            if submission_id:
                self.project_submissions[submission_id] = project_id
            return self.public_project(project)

    def update_project(self, session: DemoSession, project_id: str, name: str) -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name and not self.bugs.b3_empty_project_name:
            raise DemoError(HTTPStatus.BAD_REQUEST, "project name is required")
        with self.lock:
            project = self._require_project(project_id)
            self._require_owner_or_admin(session, project)
            old_project = self.public_project(project)
            project["name"] = normalized_name
            return old_project if self.bugs.b4_saved_ui_stale else self.public_project(project)

    def delete_project(self, session: DemoSession, project_id: str) -> None:
        with self.lock:
            project = self._require_project(project_id)
            allowed = session.role == "admin" or project["owner"] == session.username
            if self.bugs.b2_member_deletes_other_project and session.role == "member" and self._has_access(session, project):
                allowed = True
            if not allowed:
                raise DemoError(HTTPStatus.FORBIDDEN, "only the owner or Admin can delete this Project")
            del self.projects[project_id]
            self.tasks = {task_id: task for task_id, task in self.tasks.items() if task["project_id"] != project_id}

    def list_tasks(self, session: DemoSession, project_id: str) -> list[dict[str, Any]]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            return [dict(task) for task in self.tasks.values() if task["project_id"] == project_id]

    def create_task(self, session: DemoSession, project_id: str, title: str) -> dict[str, Any]:
        if not title.strip():
            raise DemoError(HTTPStatus.BAD_REQUEST, "task title is required")
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            task_id = f"task-{self.next_task}"
            self.next_task += 1
            task = {"task_id": task_id, "project_id": project_id, "title": title.strip(), "status": "OPEN"}
            self.tasks[task_id] = task
            return dict(task)

    def update_task(self, session: DemoSession, project_id: str, task_id: str, title: str, status: str) -> dict[str, Any]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            task = self._require_task(project_id, task_id)
            if title.strip():
                task["title"] = title.strip()
            if status in {"OPEN", "DONE"}:
                task["status"] = status
            return dict(task)

    def delete_task(self, session: DemoSession, project_id: str, task_id: str) -> None:
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            self._require_task(project_id, task_id)
            if not self.bugs.b1_deleted_task_reappears:
                del self.tasks[task_id]

    def list_members(self, session: DemoSession, project_id: str) -> list[dict[str, str]]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_access(session, project)
            usernames = [project["owner"], *project["members"]]
            return [self.public_user(self.users[username]) for username in usernames]

    def add_member(self, session: DemoSession, project_id: str, username: str) -> dict[str, str]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_owner_or_admin(session, project)
            user = self.users.get(username)
            if user is None or user["role"] != "member":
                raise DemoError(HTTPStatus.NOT_FOUND, "member not found")
            if username not in project["members"]:
                project["members"].append(username)
            return self.public_user(user)

    def update_member(self, session: DemoSession, project_id: str, username: str, display_name: str) -> dict[str, str]:
        with self.lock:
            project = self._require_project(project_id)
            self._require_owner_or_admin(session, project)
            if username != project["owner"] and username not in project["members"]:
                raise DemoError(HTTPStatus.NOT_FOUND, "member not found")
            user = self.users[username]
            user["display_name"] = display_name.strip() or username
            return self.public_user(user)

    def remove_member(self, session: DemoSession, project_id: str, username: str) -> None:
        with self.lock:
            project = self._require_project(project_id)
            self._require_owner_or_admin(session, project)
            if username not in project["members"]:
                raise DemoError(HTTPStatus.NOT_FOUND, "member not found")
            project["members"].remove(username)

    def _require_project(self, project_id: str) -> dict[str, Any]:
        project = self.projects.get(project_id)
        if project is None:
            raise DemoError(HTTPStatus.NOT_FOUND, "project not found")
        return project

    def _require_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None or task["project_id"] != project_id:
            raise DemoError(HTTPStatus.NOT_FOUND, "task not found")
        return task

    def _has_access(self, session: DemoSession, project: dict[str, Any]) -> bool:
        if session.role == "admin":
            return True
        if self.bugs.b6_removed_member_session_active:
            return project["project_id"] in session.project_access_snapshot
        user = self.users[session.username]
        return self._has_live_access(user, project)

    @staticmethod
    def _has_live_access(user: dict[str, str], project: dict[str, Any]) -> bool:
        return user["role"] == "admin" or project["owner"] == user["username"] or user["username"] in project["members"]

    def _require_access(self, session: DemoSession, project: dict[str, Any]) -> None:
        if not self._has_access(session, project):
            raise DemoError(HTTPStatus.FORBIDDEN, "project access denied")

    @staticmethod
    def _require_owner_or_admin(session: DemoSession, project: dict[str, Any]) -> None:
        if session.role != "admin" and project["owner"] != session.username:
            raise DemoError(HTTPStatus.FORBIDDEN, "owner or Admin required")

    @staticmethod
    def public_user(user: dict[str, str]) -> dict[str, str]:
        return {"username": user["username"], "role": user["role"], "display_name": user["display_name"]}

    @staticmethod
    def public_project(project: dict[str, Any]) -> dict[str, Any]:
        return {"project_id": project["project_id"], "name": project["name"], "owner": project["owner"], "members": list(project["members"])}


class DemoError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DemoRequestHandler(BaseHTTPRequestHandler):
    state: DemoState
    testing: bool

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_html(APP_HTML)
                return
            if path == "/version":
                self._send_json(HTTPStatus.OK, {"version": DEMO_VERSION})
                return
            session = self.state.get_session(self._session_token())
            if path == "/api/session":
                self._send_json(HTTPStatus.OK, {"username": session.username, "role": session.role})
                return
            parts = self._parts(path)
            if parts == ["api", "projects"]:
                self._send_json(HTTPStatus.OK, {"projects": self.state.list_projects(session)})
                return
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                self._send_json(HTTPStatus.OK, {"project": self.state.get_project(session, parts[2])})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "tasks":
                self._send_json(HTTPStatus.OK, {"tasks": self.state.list_tasks(session, parts[2])})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "members":
                self._send_json(HTTPStatus.OK, {"members": self.state.list_members(session, parts[2])})
                return
            raise DemoError(HTTPStatus.NOT_FOUND, "route not found")
        except DemoError as error:
            self._send_json(error.status, {"error": error.message})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            request_body = self._read_json()
            if path == "/api/register":
                user = self.state.register(str(request_body.get("username", "")), str(request_body.get("password", "")), str(request_body.get("display_name", "")))
                self._send_json(HTTPStatus.CREATED, {"user": user})
                return
            if path == "/api/login":
                token, user = self.state.login(str(request_body.get("username", "")), str(request_body.get("password", "")))
                self._send_json(HTTPStatus.OK, {"user": user}, cookie=token)
                return
            if path == "/api/logout":
                self.state.logout(self._session_token())
                self._send_json(HTTPStatus.OK, {"ok": True}, clear_cookie=True)
                return
            if path == "/test/reset":
                if not self.testing:
                    raise DemoError(HTTPStatus.NOT_FOUND, "route not found")
                self.state.reset()
                self._send_json(HTTPStatus.OK, {"reset": True, "version": DEMO_VERSION}, clear_cookie=True)
                return
            session = self.state.get_session(self._session_token())
            parts = self._parts(path)
            if parts == ["api", "projects"]:
                project = self.state.create_project(session, str(request_body.get("name", "")), str(request_body["submission_id"]) if request_body.get("submission_id") else None)
                self._send_json(HTTPStatus.CREATED, {"project": project})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "tasks":
                task = self.state.create_task(session, parts[2], str(request_body.get("title", "")))
                self._send_json(HTTPStatus.CREATED, {"task": task})
                return
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "members":
                user = self.state.add_member(session, parts[2], str(request_body.get("username", "")))
                self._send_json(HTTPStatus.CREATED, {"member": user})
                return
            raise DemoError(HTTPStatus.NOT_FOUND, "route not found")
        except DemoError as error:
            self._send_json(error.status, {"error": error.message})

    def do_PATCH(self) -> None:
        try:
            request_body = self._read_json()
            session = self.state.get_session(self._session_token())
            parts = self._parts(urlparse(self.path).path)
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                project = self.state.update_project(session, parts[2], str(request_body.get("name", "")))
                self._send_json(HTTPStatus.OK, {"project": project})
                return
            if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "tasks":
                task = self.state.update_task(session, parts[2], parts[4], str(request_body.get("title", "")), str(request_body.get("status", "")))
                self._send_json(HTTPStatus.OK, {"task": task})
                return
            if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "members":
                member = self.state.update_member(session, parts[2], parts[4], str(request_body.get("display_name", "")))
                self._send_json(HTTPStatus.OK, {"member": member})
                return
            raise DemoError(HTTPStatus.NOT_FOUND, "route not found")
        except DemoError as error:
            self._send_json(error.status, {"error": error.message})

    def do_DELETE(self) -> None:
        try:
            session = self.state.get_session(self._session_token())
            parts = self._parts(urlparse(self.path).path)
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                self.state.delete_project(session, parts[2])
            elif len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "tasks":
                self.state.delete_task(session, parts[2], parts[4])
            elif len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "members":
                self.state.remove_member(session, parts[2], parts[4])
            else:
                raise DemoError(HTTPStatus.NOT_FOUND, "route not found")
            self._send_json(HTTPStatus.OK, {"deleted": True})
        except DemoError as error:
            self._send_json(error.status, {"error": error.message})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            raise DemoError(HTTPStatus.BAD_REQUEST, "invalid JSON") from error
        if not isinstance(value, dict):
            raise DemoError(HTTPStatus.BAD_REQUEST, "JSON object required")
        return value

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        value = cookie.get(SESSION_COOKIE)
        return value.value if value is not None else None

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [part for part in path.split("/") if part]

    def _send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, content: dict[str, Any], *, cookie: str | None = None, clear_cookie: bool = False) -> None:
        body = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={cookie}; HttpOnly; SameSite=Lax; Path=/")
        if clear_cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class DemoAppServer:
    """Run the local demo in a background thread for development and tests."""

    def __init__(self, *, bugs: SeededBugs | None = None, testing: bool = True, host: str = "127.0.0.1", port: int = 0) -> None:
        self.bugs = bugs or SeededBugs()
        self.state = DemoState(self.bugs)
        handler = type("ConfiguredDemoRequestHandler", (DemoRequestHandler,), {"state": self.state, "testing": testing})
        self.server = ThreadingHTTPServer((host, port), handler)
        self.thread: threading.Thread | None = None
        self.ground_truth = GroundTruthReader(self.bugs, DEMO_VERSION)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        host_text = host.decode() if isinstance(host, bytes) else host
        return f"http://{host_text}:{port}"

    def start(self) -> DemoAppServer:
        if self.thread is None:
            self.thread = threading.Thread(target=self.server.serve_forever, name="demo-app", daemon=True)
            self.thread.start()
        return self

    def stop(self) -> None:
        if self.thread is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.thread = None

    def __enter__(self) -> DemoAppServer:
        return self.start()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.stop()


APP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Agent Testing Demo</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; color: #172033; }
    nav, form { display: flex; gap: .5rem; margin: .75rem 0; flex-wrap: wrap; }
    table { border-collapse: collapse; width: 100%; margin: .75rem 0 1.5rem; }
    th, td { border: 1px solid #ccd3df; padding: .45rem; text-align: left; }
    button, input, select { padding: .45rem; }
    section[hidden] { display: none; }
    dialog { min-width: 24rem; }
    #dependency-canvas { border: 1px solid #536078; width: 480px; height: 220px; }
    #message { min-height: 1.5rem; color: #9b2633; }
  </style>
</head>
<body>
  <h1>Project Management Demo</h1>
  <p id="version">Version: loading</p>
  <p id="message" role="status"></p>

  <section id="auth-section">
    <h2>Login</h2>
    <form id="login-form">
      <input id="login-username" name="username" placeholder="Username" aria-label="Login username">
      <input id="login-password" name="password" type="password" placeholder="Password" aria-label="Login password">
      <button type="submit">Login</button>
    </form>
    <h2>Register</h2>
    <form id="register-form">
      <input id="register-username" name="username" placeholder="Username" aria-label="Register username">
      <input id="register-name" name="display_name" placeholder="Display name" aria-label="Display name">
      <input id="register-password" name="password" type="password" placeholder="Password" aria-label="Register password">
      <button type="submit">Register</button>
    </form>
  </section>

  <section id="app-section" hidden>
    <p id="identity"></p>
    <nav>
      <button id="nav-projects" type="button">Projects</button>
      <button id="nav-tasks" type="button">Tasks</button>
      <button id="nav-members" type="button">Members</button>
      <button id="logout" type="button">Logout</button>
    </nav>

    <section id="projects-view">
      <h2>Projects</h2>
      <form id="project-form">
        <input id="project-name" name="name" placeholder="Project name" aria-label="Project name">
        <button id="project-submit" type="submit">Create Project</button>
      </form>
      <table id="projects-table"><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody></tbody></table>
    </section>

    <section id="tasks-view" hidden>
      <h2>Tasks</h2>
      <form id="task-form">
        <select id="task-project" aria-label="Task project"></select>
        <input id="task-title" placeholder="Task title" aria-label="Task title">
        <button type="submit">Create Task</button>
      </form>
      <table id="tasks-table"><thead><tr><th>Title</th><th>Status</th><th>Actions</th></tr></thead><tbody></tbody></table>
    </section>

    <section id="members-view" hidden>
      <h2>Members</h2>
      <form id="member-form">
        <select id="member-project" aria-label="Member project"></select>
        <input id="member-username" placeholder="Member username" aria-label="Member username">
        <button type="submit">Add Member</button>
      </form>
      <table id="members-table"><thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Actions</th></tr></thead><tbody></tbody></table>
    </section>

    <section>
      <h2>Project Dependency Graph</h2>
      <p>Select a dependency node on the canvas. The nodes intentionally have no DOM targets.</p>
      <canvas id="dependency-canvas" width="480" height="220" aria-label="Project dependency graph"></canvas>
      <p id="visual-status">No dependency selected</p>
    </section>
  </section>

  <dialog id="edit-dialog">
    <form id="edit-form" method="dialog">
      <h2>Edit item</h2>
      <input id="edit-value" aria-label="Edit value">
      <button id="edit-save" value="save">Save</button>
      <button value="cancel">Cancel</button>
    </form>
  </dialog>

<script>
const message = document.querySelector('#message');
let projects = [];
let currentProjectId = null;
let editAction = null;
let projectSubmissionId = crypto.randomUUID();

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function showMessage(text) { message.textContent = text; }
function showView(name) {
  for (const view of ['projects', 'tasks', 'members']) document.querySelector(`#${view}-view`).hidden = view !== name;
}
async function loadProjects() {
  const data = await api('/api/projects');
  projects = data.projects;
  currentProjectId = projects[0]?.project_id || null;
  const rows = document.querySelector('#projects-table tbody');
  rows.innerHTML = '';
  for (const project of projects) {
    const row = document.createElement('tr');
    row.dataset.projectId = project.project_id;
    row.innerHTML = `<td class="project-name"></td><td></td><td><button class="edit-project">Edit</button> <button class="delete-project">Delete</button></td>`;
    row.children[0].textContent = project.name;
    row.children[1].textContent = project.owner;
    row.querySelector('.edit-project').onclick = () => openEdit(project.name, async value => {
      const result = await api(`/api/projects/${project.project_id}`, {method: 'PATCH', body: JSON.stringify({name: value})});
      row.querySelector('.project-name').textContent = result.project.name;
    });
    row.querySelector('.delete-project').onclick = async () => { await api(`/api/projects/${project.project_id}`, {method: 'DELETE'}); await loadProjects(); };
    rows.appendChild(row);
  }
  fillProjectSelects();
}
function fillProjectSelects() {
  for (const selector of ['#task-project', '#member-project']) {
    const select = document.querySelector(selector);
    select.innerHTML = projects.map(project => `<option value="${project.project_id}">${project.name}</option>`).join('');
  }
}
async function loadTasks() {
  const projectId = document.querySelector('#task-project').value || currentProjectId;
  const rows = document.querySelector('#tasks-table tbody');
  rows.innerHTML = '';
  if (!projectId) return;
  const data = await api(`/api/projects/${projectId}/tasks`);
  for (const task of data.tasks) {
    const row = document.createElement('tr');
    row.dataset.taskId = task.task_id;
    row.innerHTML = `<td></td><td></td><td><button class="edit-task">Edit</button> <button class="delete-task">Delete</button></td>`;
    row.children[0].textContent = task.title;
    row.children[1].textContent = task.status;
    row.querySelector('.edit-task').onclick = () => openEdit(task.title, async value => { await api(`/api/projects/${projectId}/tasks/${task.task_id}`, {method: 'PATCH', body: JSON.stringify({title: value, status: task.status})}); await loadTasks(); });
    row.querySelector('.delete-task').onclick = async () => { await api(`/api/projects/${projectId}/tasks/${task.task_id}`, {method: 'DELETE'}); row.remove(); };
    rows.appendChild(row);
  }
}
async function loadMembers() {
  const projectId = document.querySelector('#member-project').value || currentProjectId;
  const rows = document.querySelector('#members-table tbody');
  rows.innerHTML = '';
  if (!projectId) return;
  const data = await api(`/api/projects/${projectId}/members`);
  for (const member of data.members) {
    const row = document.createElement('tr');
    row.dataset.username = member.username;
    row.innerHTML = `<td></td><td class="member-name"></td><td></td><td><button class="edit-member">Edit</button> <button class="remove-member">Remove</button></td>`;
    row.children[0].textContent = member.username;
    row.children[1].textContent = member.display_name;
    row.children[2].textContent = member.role;
    row.querySelector('.edit-member').onclick = () => openEdit(member.display_name, async value => { await api(`/api/projects/${projectId}/members/${member.username}`, {method: 'PATCH', body: JSON.stringify({display_name: value})}); await loadMembers(); });
    row.querySelector('.remove-member').onclick = async () => { await api(`/api/projects/${projectId}/members/${member.username}`, {method: 'DELETE'}); await loadMembers(); };
    rows.appendChild(row);
  }
}
function openEdit(value, action) {
  document.querySelector('#edit-value').value = value;
  editAction = action;
  document.querySelector('#edit-dialog').showModal();
}
document.querySelector('#edit-form').addEventListener('submit', async event => {
  if (event.submitter?.value !== 'save' || !editAction) return;
  event.preventDefault();
  try { await editAction(document.querySelector('#edit-value').value); document.querySelector('#edit-dialog').close(); }
  catch (error) { showMessage(error.message); }
});
document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const data = await api('/api/login', {method: 'POST', body: JSON.stringify({username: document.querySelector('#login-username').value, password: document.querySelector('#login-password').value})});
    document.querySelector('#identity').textContent = `${data.user.username} (${data.user.role})`;
    document.querySelector('#auth-section').hidden = true;
    document.querySelector('#app-section').hidden = false;
    await loadProjects();
  } catch (error) { showMessage(error.message); }
});
document.querySelector('#register-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('/api/register', {method: 'POST', body: JSON.stringify({username: document.querySelector('#register-username').value, display_name: document.querySelector('#register-name').value, password: document.querySelector('#register-password').value})}); showMessage('Registered'); }
  catch (error) { showMessage(error.message); }
});
document.querySelector('#logout').onclick = async () => { await api('/api/logout', {method: 'POST', body: '{}'}); location.reload(); };
document.querySelector('#project-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/projects', {method: 'POST', body: JSON.stringify({name: document.querySelector('#project-name').value, submission_id: projectSubmissionId})});
    projectSubmissionId = crypto.randomUUID();
    await loadProjects();
  } catch (error) { showMessage(error.message); }
});
document.querySelector('#task-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api(`/api/projects/${document.querySelector('#task-project').value}/tasks`, {method: 'POST', body: JSON.stringify({title: document.querySelector('#task-title').value})}); await loadTasks(); }
  catch (error) { showMessage(error.message); }
});
document.querySelector('#member-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api(`/api/projects/${document.querySelector('#member-project').value}/members`, {method: 'POST', body: JSON.stringify({username: document.querySelector('#member-username').value})}); await loadMembers(); }
  catch (error) { showMessage(error.message); }
});
document.querySelector('#nav-projects').onclick = () => { showView('projects'); loadProjects().catch(error => showMessage(error.message)); };
document.querySelector('#nav-tasks').onclick = () => { showView('tasks'); loadTasks().catch(error => showMessage(error.message)); };
document.querySelector('#nav-members').onclick = () => { showView('members'); loadMembers().catch(error => showMessage(error.message)); };
document.querySelector('#task-project').onchange = () => loadTasks().catch(error => showMessage(error.message));
document.querySelector('#member-project').onchange = () => loadMembers().catch(error => showMessage(error.message));

const canvas = document.querySelector('#dependency-canvas');
const drawing = canvas.getContext('2d');
drawing.fillStyle = '#d9e6ff'; drawing.fillRect(50, 70, 120, 60); drawing.fillRect(310, 70, 120, 60);
drawing.strokeStyle = '#263f72'; drawing.beginPath(); drawing.moveTo(170, 100); drawing.lineTo(310, 100); drawing.stroke();
drawing.fillStyle = '#172033'; drawing.font = '16px sans-serif'; drawing.fillText('Project A', 72, 106); drawing.fillText('Project B', 332, 106);
canvas.addEventListener('click', event => {
  const box = canvas.getBoundingClientRect();
  const x = (event.clientX - box.left) * canvas.width / box.width;
  const selected = x < canvas.width / 2 ? 'Project A' : 'Project B';
  document.querySelector('#visual-status').textContent = `Selected dependency: ${selected}`;
});
async function restoreSession() {
  try {
    const data = await api('/api/session');
    document.querySelector('#identity').textContent = `${data.username} (${data.role})`;
    document.querySelector('#auth-section').hidden = true;
    document.querySelector('#app-section').hidden = false;
    await loadProjects();
  } catch (error) {
    document.querySelector('#auth-section').hidden = false;
  }
}
fetch('/version').then(response => response.json()).then(data => document.querySelector('#version').textContent = `Version: ${data.version}`);
restoreSession();
</script>
</body>
</html>
"""
