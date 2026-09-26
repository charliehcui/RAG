"""Capture critical browser evidence into a small local file store."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from playwright.async_api import BrowserContext, ConsoleMessage, Page, Response

from web_testing_system.security import (
    REDACTED,
    is_sensitive_name,
    redact_sensitive_data,
)
from web_testing_system.state import StateStore

MASK_SELECTOR = "input[type='password'], input[name*='password' i], input[name*='token' i], input[name*='secret' i], [autocomplete='current-password']"


@dataclass
class EvidenceBuffer:
    network_records: list[dict[str, Any]] = field(default_factory=list)
    console_errors: list[dict[str, Any]] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("console", self._on_console)

    def mark(self) -> tuple[int, int]:
        return len(self.network_records), len(self.console_errors)

    def _on_response(self, response: Response) -> None:
        self.network_records.append({"url": EvidenceStore.safe_url(response.url), "method": response.request.method, "status": response.status})

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.console_errors.append({"type": message.type, "text": message.text})


class EvidenceStore:
    """Write evidence first, then create SQLite metadata only after success."""

    def __init__(self, *, store: StateStore, artifacts_root: Path, temporary_sensitive_root: Path, secrets: tuple[str, ...] = ()) -> None:
        self.store = store
        self.artifacts_root = artifacts_root.resolve()
        self.temporary_sensitive_root = temporary_sensitive_root.resolve()
        self.secrets = tuple(secret for secret in secrets if secret)

    async def capture_screenshot(self, *, page: Page, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, name: str) -> tuple[dict[str, Any], bytes]:
        masks = [page.locator(MASK_SELECTOR)]
        masks.extend(page.get_by_text(secret, exact=False) for secret in self.secrets)
        screenshot = await page.screenshot(full_page=True, mask=masks, animations="disabled")
        metadata = self.save_bytes(content=screenshot, extension="png", evidence_type="SCREENSHOT", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=page.url, name=name)
        return metadata, screenshot

    async def capture_dom(self, *, page: Page, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, name: str = "dom") -> dict[str, Any]:
        return self.save_text(content=self.sanitize_text(await page.content()), extension="html", evidence_type="DOM", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=page.url, name=name)

    async def capture_accessibility(self, *, page: Page, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, name: str = "accessibility") -> dict[str, Any]:
        snapshot = await page.locator("body").aria_snapshot()
        return self.save_text(content=self.sanitize_text(snapshot), extension="txt", evidence_type="ACCESSIBILITY", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=page.url, name=name)

    def capture_network(self, *, buffer: EvidenceBuffer, marker: tuple[int, int], run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str, relevant_url: str | None = None) -> dict[str, Any]:
        records = buffer.network_records[marker[0]:]
        if relevant_url is not None:
            records = [record for record in records if relevant_url in str(record["url"])]
        return self.save_json(content=records, evidence_type="NETWORK", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url, name="network")

    def capture_console(self, *, buffer: EvidenceBuffer, marker: tuple[int, int], run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str) -> dict[str, Any]:
        records = buffer.console_errors[marker[1]:]
        return self.save_json(content=records, evidence_type="CONSOLE", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url, name="console")

    async def start_trace(self, context: BrowserContext) -> None:
        await context.tracing.start(screenshots=False, snapshots=False, aria_snapshots=False, screen_snapshots=False, sources=False)

    async def capture_trace(self, *, context: BrowserContext, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str) -> dict[str, Any]:
        raw_path = self._temporary_path(f"trace-{uuid4().hex}.zip")
        destination = self._evidence_path(run_id, finding_id, attempt_id, "playwright-trace", "zip")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await context.tracing.stop(path=raw_path)
            self._sanitize_trace(raw_path, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            raw_path.unlink(missing_ok=True)
        return self._add_metadata(destination=destination, evidence_type="TRACE", run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url)

    def save_json(self, *, content: Any, evidence_type: str, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str | None, name: str) -> dict[str, Any]:
        safe_content = redact_sensitive_data(content)
        text = json.dumps(safe_content, ensure_ascii=False, indent=2)
        return self.save_text(content=self.sanitize_text(text), extension="json", evidence_type=evidence_type, run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url, name=name)

    def save_text(self, *, content: str, extension: str, evidence_type: str, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str | None, name: str) -> dict[str, Any]:
        return self.save_bytes(content=self.sanitize_text(content).encode("utf-8"), extension=extension, evidence_type=evidence_type, run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url, name=name)

    def save_bytes(self, *, content: bytes, extension: str, evidence_type: str, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str | None, name: str) -> dict[str, Any]:
        destination = self._evidence_path(run_id, finding_id, attempt_id, name, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return self._add_metadata(destination=destination, evidence_type=evidence_type, run_id=run_id, task_id=task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, url=url)

    def sanitize_text(self, text: str) -> str:
        sanitized = text
        for secret in self.secrets:
            sanitized = sanitized.replace(secret, REDACTED)
        sanitized = re.sub(r"(?i)(authorization|cookie|password|secret|token)(\s*[:=]\s*)([^\s\"'<>;,]+)", rf"\1\2{REDACTED}", sanitized)
        return sanitized

    @staticmethod
    def safe_url(url: str) -> str:
        parts = urlsplit(url)
        query = [(key, REDACTED if is_sensitive_name(key) else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _add_metadata(self, *, destination: Path, evidence_type: str, run_id: str, task_id: str, finding_id: str, attempt_id: str, browser_session_id: str, url: str | None) -> dict[str, Any]:
        relative_path = destination.relative_to(self.artifacts_root).as_posix()
        return self.store.add_evidence(evidence_id=f"evidence-{uuid4().hex}", run_id=run_id, task_id=task_id, finding_id=finding_id, evidence_type=evidence_type, attempt_id=attempt_id, relative_file_path=relative_path, url=self.safe_url(url) if url else None, browser_session_id=browser_session_id)

    def _evidence_path(self, run_id: str, finding_id: str, attempt_id: str, name: str, extension: str) -> Path:
        safe_parts = [self._safe_segment(value) for value in (run_id, finding_id, attempt_id, name)]
        return self.artifacts_root / safe_parts[0] / "evidence" / safe_parts[1] / safe_parts[2] / f"{safe_parts[3]}.{extension}"

    def _temporary_path(self, filename: str) -> Path:
        return self.temporary_sensitive_root / self._safe_segment(filename)

    @staticmethod
    def _safe_segment(value: str) -> str:
        safe_value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
        if not safe_value or safe_value in {".", ".."}:
            raise ValueError("invalid evidence path segment")
        return safe_value

    def _sanitize_trace(self, source: Path, destination: Path) -> None:
        with zipfile.ZipFile(source, "r") as source_zip, zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as destination_zip:
            for item in source_zip.infolist():
                if "network" in item.filename.casefold() or item.filename.startswith("resources/"):
                    continue
                data = source_zip.read(item.filename)
                try:
                    data = self.sanitize_text(data.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError:
                    pass
                destination_zip.writestr(item, data)
