import asyncio
from typing import Any, cast

from majordom_integration_sdk.schemas.notification import Notification, NotificationPriority, NotificationType
from majordom_integration_sdk.testing import RecordingControllerOutput


def test_notification_defaults():
    n = Notification(message="Firmware update available")
    assert n.type is NotificationType.info
    assert n.priority is NotificationPriority.normal
    assert n.ttl is None


def test_output_records_notification():
    out = RecordingControllerOutput()
    n = Notification(
        message="Re-plug the radio",
        type=NotificationType.warning,
        priority=NotificationPriority.urgent,
        ttl=30,
    )
    asyncio.run(out.controller_did_emit_notification(cast(Any, None), n))
    assert out.notifications == [n]
