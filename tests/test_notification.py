import asyncio
from typing import Any, cast

from majordom_integration_sdk import IntegrationError
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


def test_output_keeps_the_active_errors_by_id():
    out = RecordingControllerOutput()
    lost, again = (
        IntegrationError("server-unreachable", "Server lost"),
        IntegrationError("server-unreachable", "Still lost"),
    )
    unplugged = IntegrationError("stick-unplugged", "Re-plug the stick")

    async def report():
        for error in (lost, unplugged, again):
            await out.controller_did_encounter_error(cast(Any, None), error)
        await out.controller_did_resolve_error(cast(Any, None), "stick-unplugged")
        await out.controller_did_resolve_error(cast(Any, None), "never-reported")

    asyncio.run(report())
    assert out.errors == {"server-unreachable": again}
    assert out.error_log == [lost, unplugged, again, "stick-unplugged", "never-reported"]
