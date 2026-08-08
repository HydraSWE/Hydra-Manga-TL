"""Lightweight in-app update checks backed by the hosted installer manifest."""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from hydra_manga_tl import __version__

MANIFEST_URL = "https://hydramangatl.annomous.com/offline_installer/v1/manifest.json"
CHECK_INTERVAL = timedelta(hours=24)

STATUS_IDLE = "idle"
STATUS_CHECKING = "checking"
STATUS_AVAILABLE = "available"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    file_name: str
    url: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class UpdateState:
    status: str = STATUS_IDLE
    current_version: str = __version__
    latest_version: str = ""
    file_name: str = ""
    url: str = ""
    sha256: str = ""
    size_bytes: int = 0
    checked_at: str = ""
    error: str = ""
    reason: str = ""
    dismissed: bool = False

    @property
    def is_available(self) -> bool:
        return self.status == STATUS_AVAILABLE and not self.dismissed


UpdateCheckResult = UpdateState


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(version or "").strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if digits == "":
            break
        parts.append(int(digits))
    if not parts:
        raise ValueError(f"Invalid version: {version!r}")
    return tuple(parts)


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    return version_key(candidate) > version_key(current)


def parse_manifest(payload: str | bytes | dict) -> UpdateManifest:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    elif isinstance(payload, str):
        payload = payload.lstrip("\ufeff")
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    version = str(data.get("version") or "").strip()
    file_name = str(data.get("fileName") or "").strip()
    url = str(data.get("url") or "").strip()
    sha256 = str(data.get("sha256") or "").strip().lower()
    try:
        size_bytes = int(data.get("sizeBytes") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("Manifest sizeBytes is invalid") from error
    if not version or not file_name or not url or not sha256:
        raise ValueError("Manifest is missing required update fields")
    if not url.lower().startswith("https://"):
        raise ValueError("Manifest update URL must be HTTPS")
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("Manifest SHA-256 is invalid")
    if size_bytes <= 0:
        raise ValueError("Manifest sizeBytes must be positive")
    version_key(version)
    return UpdateManifest(
        version=version,
        file_name=file_name,
        url=url,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def fetch_manifest(
    url: str = MANIFEST_URL,
    *,
    timeout: float = 8.0,
    opener: Callable[[str, float], bytes] | None = None,
) -> UpdateManifest:
    if opener is not None:
        return parse_manifest(opener(url, timeout))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"HydraMangaTL/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_manifest(response.read())


class _UpdateCheckWorker(QObject):
    finished = Signal(object)

    def __init__(self, manifest_url: str, reason: str) -> None:
        super().__init__()
        self._manifest_url = manifest_url
        self._reason = reason

    def run(self) -> None:
        try:
            manifest = fetch_manifest(self._manifest_url)
            self.finished.emit((manifest, None, self._reason))
        except Exception as error:  # pragma: no cover - exercised via service state tests
            self.finished.emit((None, error, self._reason))


class UpdaterService(QObject):
    update_state_changed = Signal(object)

    def __init__(self, manifest_url: str = MANIFEST_URL) -> None:
        super().__init__()
        self.manifest_url = manifest_url
        self._state = UpdateState()
        self._threads: list[QThread] = []
        self._workers: list[_UpdateCheckWorker] = []
        self._notified_versions: set[str] = set()

    def current_state(self) -> UpdateState:
        return self._state

    def should_check_now(self, settings, *, now: datetime | None = None) -> bool:
        if not getattr(settings, "updates_check_automatically", True):
            return False
        checked_at = parse_utc(getattr(settings, "updates_last_checked_at", ""))
        if checked_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        return now.astimezone(timezone.utc) - checked_at >= CHECK_INTERVAL

    def check_startup(self) -> None:
        from hydra_manga_tl.core.settings import SETTINGS

        if self.should_check_now(SETTINGS):
            self.start_background_check("startup")

    def start_background_check(self, reason: str = "manual") -> None:
        self._set_state(UpdateState(status=STATUS_CHECKING, reason=reason))
        thread = QThread()
        worker = _UpdateCheckWorker(self.manifest_url, reason)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._finish_background_check)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._threads.remove(t) if t in self._threads else None)
        thread.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._threads.append(thread)
        self._workers.append(worker)
        thread.start()

    def check_now(self, reason: str = "manual") -> UpdateCheckResult:
        self._set_state(UpdateState(status=STATUS_CHECKING, reason=reason))
        try:
            manifest = fetch_manifest(self.manifest_url)
        except Exception as error:
            state = self._failed_state(error, reason)
        else:
            state = self._state_from_manifest(manifest, reason)
        self._set_state(state)
        return state

    def dismiss_available_update(self) -> None:
        if not self._state.latest_version:
            return
        from hydra_manga_tl.core.settings import SETTINGS

        SETTINGS.updates_dismissed_version = self._state.latest_version
        try:
            SETTINGS.save()
        except OSError:
            logging.getLogger(__name__).exception("Failed to persist dismissed update")
        self._set_state(UpdateState(
            **{
                **self._state.__dict__,
                "dismissed": True,
            }
        ))

    def _finish_background_check(self, payload: object) -> None:
        manifest, error, reason = payload
        if error is not None:
            state = self._failed_state(error, reason)
        else:
            state = self._state_from_manifest(manifest, reason)
        self._set_state(state)

    def _state_from_manifest(self, manifest: UpdateManifest, reason: str) -> UpdateState:
        from hydra_manga_tl.core.settings import SETTINGS

        checked_at = utc_now_iso()
        SETTINGS.updates_last_checked_at = checked_at
        try:
            SETTINGS.save()
        except OSError:
            logging.getLogger(__name__).exception("Failed to persist update check timestamp")
        available = is_newer_version(manifest.version, __version__)
        dismissed = SETTINGS.updates_dismissed_version == manifest.version
        status = STATUS_AVAILABLE if available else STATUS_UP_TO_DATE
        state = UpdateState(
            status=status,
            current_version=__version__,
            latest_version=manifest.version,
            file_name=manifest.file_name,
            url=manifest.url,
            sha256=manifest.sha256,
            size_bytes=manifest.size_bytes,
            checked_at=checked_at,
            reason=reason,
            dismissed=dismissed,
        )
        if state.is_available:
            self._notify_update_available(state)
        return state

    def _failed_state(self, error: BaseException, reason: str) -> UpdateState:
        checked_at = utc_now_iso()
        from hydra_manga_tl.core.settings import SETTINGS

        SETTINGS.updates_last_checked_at = checked_at
        try:
            SETTINGS.save()
        except OSError:
            logging.getLogger(__name__).exception("Failed to persist failed update check timestamp")
        return UpdateState(
            status=STATUS_FAILED,
            current_version=__version__,
            checked_at=checked_at,
            error=str(error) or type(error).__name__,
            reason=reason,
        )

    def _notify_update_available(self, state: UpdateState) -> None:
        if state.latest_version in self._notified_versions and state.reason != "manual":
            return
        self._notified_versions.add(state.latest_version)
        from hydra_manga_tl.core.notifications import NOTIFICATION_SERVICE, NotificationEvent

        NOTIFICATION_SERVICE.notify(
            NotificationEvent.UPDATES_AVAILABLE,
            "Update available",
            f"Hydra Manga TL {state.latest_version} is ready.",
        )

    def _set_state(self, state: UpdateState) -> None:
        self._state = state
        self.update_state_changed.emit(state)


UPDATER = UpdaterService()
