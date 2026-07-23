from .base import Base, StrEnum


class NotificationType(StrEnum):
    """The callout style the app renders the notification with."""

    info = "info"
    warning = "warning"
    error = "error"


class NotificationPriority(StrEnum):
    """How insistently the notification is delivered (maps to the OS interruption levels)."""

    silent = "silent"  # appears in the list, no alert
    normal = "normal"  # default banner
    time_sensitive = "time_sensitive"  # breaks through focus/quiet modes
    urgent = "urgent"  # demands attention


class Notification(Base):
    """A general user-facing message an integration surfaces to the user as a floating tile in the
    app. Distinct from ``controller_did_encounter_error`` (which is specifically for integration
    failures/health) — use this for anything informational or advisory the user should see.

    ``ttl`` is how many seconds the tile stays before auto-dismissing; ``None`` keeps it until the
    user dismisses it.
    """

    message: str
    type: NotificationType = NotificationType.info
    priority: NotificationPriority = NotificationPriority.normal
    ttl: int | None = None
