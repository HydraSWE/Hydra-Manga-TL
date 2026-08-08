"""Desktop notification service (v1: QSystemTrayIcon.showMessage).

Architecture:
- NOTIFICATION_SERVICE is a module-level singleton (mirrors WORKSPACE / APP_STATE pattern).
- initialize() must be called once from MangaApplication after QApplication exists.
- All settings are read from the live SETTINGS object at call time (no caching).
- Foreground suppression: non-error events are muted when the main window is active
  and not minimized. Error events always fire.
- V2 notes: native Windows toast action buttons (Open Output Folder, Open Logs, etc.)
  require a Windows toast dependency and PyInstaller app identity. Defer until v1 is
  proven in frozen builds.
"""
from __future__ import annotations

from enum import Enum, auto


class NotificationEvent(Enum):
    TRANSLATION_COMPLETED = auto()
    TRANSLATION_FAILED = auto()
    EXPORT_COMPLETED = auto()
    EXPORT_FAILED = auto()
    REVIEW_QUEUE = auto()
    BUILD_FINISHED = auto()
    UPDATES_AVAILABLE = auto()


_ERROR_EVENTS = frozenset({
    NotificationEvent.TRANSLATION_FAILED,
    NotificationEvent.EXPORT_FAILED,
})

_FOREGROUND_ALLOWED_EVENTS = _ERROR_EVENTS | frozenset({
    NotificationEvent.EXPORT_COMPLETED,
    NotificationEvent.UPDATES_AVAILABLE,
})

_EVENT_SETTING_ATTR: dict[NotificationEvent, str] = {
    NotificationEvent.TRANSLATION_COMPLETED: "notif_translation_completed",
    NotificationEvent.TRANSLATION_FAILED: "notif_translation_failed",
    NotificationEvent.EXPORT_COMPLETED: "notif_export_completed",
    NotificationEvent.EXPORT_FAILED: "notif_export_failed",
    NotificationEvent.REVIEW_QUEUE: "notif_review_queue",
    NotificationEvent.BUILD_FINISHED: "notif_build_finished",
    NotificationEvent.UPDATES_AVAILABLE: "notif_updates_available",
}


class NotificationService:
    """Owns the QSystemTrayIcon and routes notification requests.

    Must be initialized via initialize() after QApplication is created.
    Safe to construct before QApplication (tray is created lazily).
    """

    def __init__(self) -> None:
        self._tray = None  # QSystemTrayIcon; created in initialize()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, icon=None) -> None:
        """Create and show the tray icon.

        Call once from MangaApplication.run() after QApplication exists and
        the application icon has been set. icon may be a QIcon or None; if
        None the application's window icon is used.
        """
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        if icon is None:
            icon = QApplication.windowIcon()

        self._tray = QSystemTrayIcon(icon)
        self._tray.setToolTip("Hydra Manga TL")
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(
        self,
        event: NotificationEvent,
        title: str,
        message: str,
        *,
        duration_ms: int = 5000,
    ) -> None:
        """Show a notification if gating conditions pass."""
        if self._tray is None:
            return
        if not self._should_notify(event):
            return
        
        self._tray.showMessage(
            title,
            message,
            self._icon_for(event),
            duration_ms,
        )

    def _icon_for(self, event: NotificationEvent):
        if self._tray is not None and not self._tray.icon().isNull():
            return self._tray.icon()
        from PySide6.QtWidgets import QSystemTrayIcon
        return QSystemTrayIcon.MessageIcon.NoIcon

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    def _should_notify(self, event: NotificationEvent) -> bool:
        """Return True if this event should produce a visible notification.

        Rules (in order):
        1. Global master switch off -> suppress.
        2. Per-event switch off -> suppress.
        3. Non-attention event + Hydra window is in foreground (active, not minimized) -> suppress.
        4. Otherwise -> allow.
        """
        from hydra_manga_tl.core.settings import SETTINGS

        if not SETTINGS.notif_enabled:
            return False

        attr = _EVENT_SETTING_ATTR.get(event)
        if attr and not getattr(SETTINGS, attr, False):
            return False

        if event not in _FOREGROUND_ALLOWED_EVENTS:
            from PySide6.QtWidgets import QApplication
            window = QApplication.activeWindow()
            if window is not None and not window.isMinimized():
                return False

        return True

    # ------------------------------------------------------------------
    # Tray interaction
    # ------------------------------------------------------------------

    def _on_tray_activated(self, reason) -> None:
        """Clicking the tray icon or its notification bubble focuses Hydra."""
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        if reason not in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.MiddleClick,
        ):
            return

        for widget in QApplication.topLevelWidgets():
            if widget.isWindow() and "Hydra" in (widget.windowTitle() or ""):
                widget.show()
                widget.raise_()
                widget.activateWindow()
                break


# Module-level singleton -- mirrors WORKSPACE / APP_STATE pattern.
NOTIFICATION_SERVICE = NotificationService()
