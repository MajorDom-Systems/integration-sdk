"""Every row of the peripheral state machine, the one lifetime rule, retries and readiness."""

import asyncio
import json

import pytest
from pydantic import BaseModel
from support import changed, run, settle, state, until

from majordom_integration_sdk import Context, Endpoint, Need, PeripheralChange
from majordom_integration_sdk.runtime import MemoryDisabledStore, PeripheralStatus, Runtime
from majordom_integration_sdk.runtime import PeripheralState as S
from majordom_integration_sdk.runtime import StatusKind as K
from majordom_integration_sdk.runtime import runtime as module
from majordom_integration_sdk.testing import FakePeripheral, RecordingRuntimeOutput

ZIGBEE = Endpoint("zigbee", "ezsp", "/dev/ttyUSB0")
MOVED = Endpoint("zigbee", "ezsp", "/dev/ttyUSB1")
THREAD = Endpoint("thread", "otbr-rest", "http://127.0.0.1:8081", is_shared=True)
SERVER = Endpoint("matter", "matter-server", "ws://127.0.0.1:5580", is_shared=True)
UNBOUND = "need zigbee/*: nothing offers it"


def radio(*provides: Endpoint) -> FakePeripheral:
    return FakePeripheral("radio", provides=provides)


def zigbee(**kwargs) -> FakePeripheral:
    return FakePeripheral("zigbee", [Need("zigbee")], **kwargs)


# boot


async def test_a_peripheral_with_nothing_to_wait_for_runs_and_stops_with_the_runtime(tmp_path):
    host = FakePeripheral("host", provides=[Endpoint("ip", is_shared=True)])
    runtime = await run(tmp_path, host)
    assert runtime.status(host) == PeripheralStatus(S.RUNNING)

    await runtime.stop()
    assert host.calls == ["start", "stop"]


async def test_boot_without_a_required_need_waits_and_says_why(tmp_path):
    output = RecordingRuntimeOutput()
    integration = zigbee()
    runtime = await run(tmp_path, integration, output=output)

    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, UNBOUND)
    assert runtime.unbound(integration) == {"zigbee": UNBOUND}
    assert output.statuses == [("zigbee", PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, UNBOUND))]
    assert str(runtime.status(integration)) == f"waiting: {UNBOUND}"  # for logs and the CLI
    assert integration.calls == []


async def test_it_starts_once_its_need_appears_and_waits_again_while_it_is_lost(tmp_path):
    stick, integration = radio(), zigbee()
    output = RecordingRuntimeOutput()
    runtime = await run(tmp_path, stick, integration, output=output)

    stick.provided = [ZIGBEE]  # identified
    await changed(runtime, stick)
    assert state(runtime, integration) == S.RUNNING
    assert integration.peripherals.endpoint("zigbee") == ZIGBEE
    assert runtime.providers(integration) == {"zigbee": "radio"}
    assert [status.state for name, status in output.statuses if name == "zigbee"] == [S.WAITING, S.STARTING, S.RUNNING]

    stick.provided = []  # unplugged
    await changed(runtime, stick)
    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, UNBOUND)
    assert integration.changes == []  # a lost required need stops it without asking

    stick.provided = [ZIGBEE]  # plugged in again
    await changed(runtime, stick)
    assert state(runtime, integration) == S.RUNNING
    assert integration.calls == ["start", "stop", "start"]


# running: what moved


async def test_a_moved_need_is_handed_over_or_restarts_it(tmp_path):
    stick, integration = radio(ZIGBEE), zigbee(answer=True)
    runtime = await run(tmp_path, stick, integration)

    stick.provided = [MOVED]
    await changed(runtime, stick)
    assert integration.changes == [PeripheralChange("zigbee", ZIGBEE, MOVED)]
    assert integration.calls == ["start"]  # handled in place
    assert integration.peripherals.endpoint("zigbee") == MOVED  # already there when it heard

    integration.answer = False
    stick.provided = [ZIGBEE]
    await changed(runtime, stick)
    assert integration.calls == ["start", "stop", "start"]
    assert state(runtime, integration) == S.RUNNING


async def test_an_optional_need_lost_or_back_is_handed_over(tmp_path):
    border = FakePeripheral("otbr", provides=[THREAD])
    matter = FakePeripheral("matter", [Need("thread", is_required=False)], answer=True)
    runtime = await run(tmp_path, border, matter)

    await runtime.disable(border)
    await settle(runtime)
    assert matter.changes == [PeripheralChange("thread", THREAD, None)]
    assert state(runtime, matter) == S.RUNNING
    assert runtime.unbound(matter) == {"thread": "need thread/*: otbr is disabled"}

    await runtime.enable(border)
    await settle(runtime)
    assert matter.changes[-1] == PeripheralChange("thread", None, THREAD)
    assert matter.calls == ["start"]


async def test_a_hook_that_hangs_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "HOOK_TIMEOUT", 0.01)

    class Hangs(FakePeripheral):
        async def needed_peripheral_did_change(self, change):
            await asyncio.Event().wait()

    stick, integration = radio(ZIGBEE), Hangs("zigbee", [Need("zigbee")])
    runtime = await run(tmp_path, stick, integration)
    stick.provided = [MOVED]
    await changed(runtime, stick)
    expected = PeripheralStatus(S.WAITING, K.RETRYING, "didn't take the change within 0.01 s; trying again in 0.01 s")
    assert runtime.status(integration) == expected


async def test_a_hook_that_raises_restarts_it_through_the_retries(tmp_path):
    stick, integration = radio(ZIGBEE), zigbee(answer=RuntimeError("confused"))
    runtime = await run(tmp_path, stick, integration)

    stick.provided = [MOVED]
    await changed(runtime, stick)
    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.RETRYING, "confused; trying again in 0.01 s")
    assert integration.calls == ["start", "stop"]

    await until(runtime, lambda: state(runtime, integration) == S.RUNNING)
    assert integration.peripherals.endpoint("zigbee") == MOVED


async def moves(runtime, stick: FakePeripheral, integration: FakePeripheral, times: int):
    """The stick moves ``times``, each once the integration runs again."""
    for endpoint in [MOVED, ZIGBEE] * times:
        await until(runtime, lambda: state(runtime, integration) == S.RUNNING)
        stick.provided = [endpoint]
        await changed(runtime, stick)
        times -= 1
        if not times:
            return


async def test_hook_failures_in_a_row_give_it_up(tmp_path):
    stick, integration = radio(ZIGBEE), zigbee(answer=RuntimeError("confused"))
    runtime = await run(tmp_path, stick, integration)
    await moves(runtime, stick, integration, len(module.RETRY_DELAYS) + 1)
    assert runtime.status(integration) == PeripheralStatus(S.FAILED, K.GAVE_UP, "confused")


async def test_a_failure_after_a_stable_run_starts_the_retries_over(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STABLE_RUN", 0)
    stick, integration = radio(ZIGBEE), zigbee(answer=RuntimeError("confused"))
    runtime = await run(tmp_path, stick, integration)
    await moves(runtime, stick, integration, len(module.RETRY_DELAYS) + 1)
    assert state(runtime, integration) == S.WAITING


# starting: retries


async def test_a_start_that_raises_is_tried_again_then_given_up_on(tmp_path):
    output = RecordingRuntimeOutput()
    broken = FakePeripheral("broken", failures=10)
    runtime = await run(tmp_path, broken, output=output)
    assert runtime.status(broken) == PeripheralStatus(S.WAITING, K.RETRYING, "broken broke; trying again in 0.01 s")

    await until(runtime, lambda: state(runtime, broken) == S.FAILED)
    assert runtime.status(broken) == PeripheralStatus(S.FAILED, K.GAVE_UP, "broken broke")
    assert broken.calls == ["start", "stop"] * 4  # the first, then one per retry delay; each releases what it holds
    assert output.last("broken") == PeripheralStatus(S.FAILED, K.GAVE_UP, "broken broke")


async def test_a_start_that_hangs_times_out_and_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "START_TIMEOUT", 0.01)

    class Hangs(FakePeripheral):
        async def start(self):
            await super().start()
            if len(self.calls) == 1:
                await asyncio.Event().wait()

    hangs = Hangs("hangs")
    runtime = await run(tmp_path, hangs)
    assert runtime.status(hangs) == PeripheralStatus(
        S.WAITING, K.RETRYING, "didn't start within 0.01 s; trying again in 0.01 s"
    )
    await until(runtime, lambda: state(runtime, hangs) == S.RUNNING)
    assert hangs.calls == ["start", "stop", "start"]


async def test_a_start_cancelled_from_inside_is_a_failure(tmp_path):
    class Cancelled(FakePeripheral):
        async def start(self):
            await super().start()
            if len(self.calls) == 1:
                raise asyncio.CancelledError  # e.g. a task it awaited was cancelled

    cancelled = Cancelled("cancelled")
    runtime = await run(tmp_path, cancelled)
    assert runtime.status(cancelled) == PeripheralStatus(
        S.WAITING, K.RETRYING, "CancelledError; trying again in 0.01 s"
    )
    await until(runtime, lambda: state(runtime, cancelled) == S.RUNNING)


async def test_a_start_that_ignores_being_stopped_is_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STOP_TIMEOUT", 0.01)
    started = asyncio.Event()

    class Stubborn(FakePeripheral):
        async def start(self):
            await super().start()
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # goes on regardless

    stubborn = Stubborn("stubborn")
    runtime = Runtime(tmp_path / "data", [stubborn])
    await runtime.start()
    await started.wait()
    await asyncio.wait_for(runtime.disable(stubborn), 1)
    assert state(runtime, stubborn) == S.DISABLED
    await settle(runtime)
    assert state(runtime, stubborn) == S.DISABLED


async def test_a_start_that_raises_once_runs_on_the_retry(tmp_path):
    flaky = FakePeripheral("flaky", failures=1)
    runtime = await run(tmp_path, flaky)
    await until(runtime, lambda: state(runtime, flaky) == S.RUNNING)
    assert flaky.calls == ["start", "stop", "start"]


async def test_a_failed_one_isnt_revived_by_a_replug_only_by_the_user(tmp_path):
    stick, integration = radio(ZIGBEE), zigbee(failures=10)
    runtime = await run(tmp_path, stick, integration)
    await until(runtime, lambda: state(runtime, integration) == S.FAILED)

    stick.provided = []
    await changed(runtime, stick)
    stick.provided = [ZIGBEE]
    await changed(runtime, stick)
    assert state(runtime, integration) == S.FAILED
    assert integration.calls == ["start", "stop"] * 4

    integration.failures = 0
    await runtime.restart(integration)
    await settle(runtime)
    assert state(runtime, integration) == S.RUNNING


async def test_a_failed_one_tries_again_once_its_settings_change(tmp_path):
    class Channel(BaseModel):
        channel: int = 11

    integration = FakePeripheral("zigbee", failures=4, settings=Channel)
    runtime = await run(tmp_path, integration)
    await until(runtime, lambda: state(runtime, integration) == S.FAILED)

    await runtime.update_settings(integration, {"channel": 15})
    await settle(runtime)
    assert state(runtime, integration) == S.RUNNING
    assert integration.settings == Channel(channel=15)


# disabled


async def test_disabled_is_kept_and_never_revived_by_a_replug(tmp_path):
    store = MemoryDisabledStore()
    stick, integration = radio(ZIGBEE), zigbee()
    runtime = await run(tmp_path, stick, integration, disabled=store)

    await runtime.disable(integration)
    assert runtime.status(integration) == PeripheralStatus(S.DISABLED)
    assert integration.calls == ["start", "stop"]
    assert store.is_disabled("zigbee")
    stick.provided = []
    await changed(runtime, stick)
    stick.provided = [ZIGBEE]
    await changed(runtime, stick)
    assert state(runtime, integration) == S.DISABLED
    with pytest.raises(ValueError, match="enable it"):
        await runtime.restart(integration)

    again = zigbee()  # after a reboot
    runtime = await run(tmp_path / "again", radio(ZIGBEE), again, disabled=store)
    assert state(runtime, again) == S.DISABLED and again.calls == []

    await runtime.enable(again)
    await settle(runtime)
    assert state(runtime, again) == S.RUNNING
    assert not store.is_disabled("zigbee")


# self-stop and crashes

STOPPED = "stopped by itself: waits for its needs to change or a restart"


async def test_one_that_stops_by_itself_waits_for_its_needs_to_change(tmp_path):
    stick, integration = radio(ZIGBEE), zigbee()
    runtime = await run(tmp_path, stick, integration)

    await integration.runtime.peripheral_did_stop(integration)
    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.STOPPED_ITSELF, STOPPED)
    assert integration.calls == ["start", "stop"]
    await runtime.refresh()
    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.STOPPED_ITSELF, STOPPED)

    stick.provided = []  # replugged
    await changed(runtime, stick)
    stick.provided = [ZIGBEE]
    await changed(runtime, stick)
    assert state(runtime, integration) == S.RUNNING


async def test_one_that_stopped_by_itself_starts_once_restarted(tmp_path):
    integration = FakePeripheral("hue")
    runtime = await run(tmp_path, integration)
    await integration.runtime.peripheral_did_stop(integration)

    await runtime.restart(integration)
    await settle(runtime)
    assert state(runtime, integration) == S.RUNNING


async def test_one_that_crashes_is_started_again_then_given_up(tmp_path):
    integration = FakePeripheral("zigbee")
    runtime = await run(tmp_path, integration)
    for _ in range(len(module.RETRY_DELAYS) + 1):
        await until(runtime, lambda: state(runtime, integration) == S.RUNNING)
        await integration.runtime.peripheral_did_fail(integration, "Lost the connection to the Zigbee radio.")
    assert runtime.status(integration) == PeripheralStatus(
        S.FAILED, K.GAVE_UP, "Lost the connection to the Zigbee radio."
    )

    await runtime.restart(integration)
    await settle(runtime)
    assert state(runtime, integration) == S.RUNNING


async def test_a_failure_told_from_inside_its_start_fails_that_start(tmp_path):
    class GivesUp(FakePeripheral):
        async def start(self):
            await super().start()
            if len(self.calls) == 1:
                await self.runtime.peripheral_did_fail(self, "Wrong credentials: re-enter them.")
                raise RuntimeError("can't go on")

    hue = GivesUp("hue")
    runtime = await run(tmp_path, hue)
    assert runtime.status(hue) == PeripheralStatus(
        S.WAITING, K.RETRYING, "Wrong credentials: re-enter them.; trying again in 0.01 s"
    )
    await until(runtime, lambda: state(runtime, hue) == S.RUNNING)
    assert hue.calls == ["start", "stop", "start"]


# calls from what a peripheral runs in the background, and from the runtime's own output


async def test_a_call_from_a_task_a_start_spawned_returns_once_acted_on(tmp_path):
    seen: list[S] = []

    class Stick(FakePeripheral):
        async def start(self):
            await super().start()
            self.task = asyncio.create_task(self.flash_later())

        async def flash_later(self):
            await asyncio.sleep(0.01)
            self.provided = []  # busy: being flashed
            await self.runtime.peripheral_did_change_provided(self)
            seen.append(state(runtime, integration))

    stick, integration = Stick("stick", provides=[ZIGBEE]), zigbee()
    runtime = await run(tmp_path, stick, integration)
    await stick.task
    assert seen == [S.WAITING]  # it let go of the stick before the flash goes on


async def test_a_call_scheduled_by_a_status_callback_returns_once_acted_on(tmp_path):
    seen: list[S] = []
    integration, ran = FakePeripheral("zigbee"), asyncio.Event()

    class Output(RecordingRuntimeOutput):
        def peripheral_did_change_status(self, peripheral, status):
            super().peripheral_did_change_status(peripheral, status)
            if status.state == S.RUNNING:
                self.task = asyncio.ensure_future(disable())

    async def disable():
        await ran.wait()
        await runtime.disable(integration)
        seen.append(state(runtime, integration))

    output = Output()
    runtime = await run(tmp_path, integration, output=output)
    ran.set()
    await output.task
    assert seen == [S.DISABLED]


# a server next to what needs it, and what a peripheral brings along while it runs


def matter_and_server() -> tuple[FakePeripheral, FakePeripheral]:
    return FakePeripheral("matter", [Need("matter")]), FakePeripheral(
        "matter-server", provides=[SERVER], is_on_demand=True
    )


async def test_a_server_runs_while_what_needs_it_runs(tmp_path):
    matter, server = matter_and_server()
    runtime = await run(tmp_path, server, matter)
    assert (state(runtime, matter), state(runtime, server)) == (S.RUNNING, S.RUNNING)

    await runtime.disable(matter)
    assert server.calls == ["start", "stop"]
    assert runtime.status(server) == PeripheralStatus(S.WAITING, K.UNUSED, "nothing that runs uses it")

    await runtime.enable(matter)
    await settle(runtime)
    assert (state(runtime, matter), state(runtime, server)) == (S.RUNNING, S.RUNNING)


async def test_a_server_only_a_failed_one_needs_stops(tmp_path):
    matter, server = matter_and_server()
    runtime = await run(tmp_path, server, matter)
    for _ in range(len(module.RETRY_DELAYS) + 1):
        await until(runtime, lambda: state(runtime, matter) == S.RUNNING)
        await matter.runtime.peripheral_did_fail(matter, "gave up")
    assert runtime.status(server) == PeripheralStatus(S.WAITING, K.UNUSED, "nothing that runs uses it")


async def test_what_it_brought_along_while_running_goes_when_it_stops(tmp_path):
    esphome = FakePeripheral("esphome")
    proxy = FakePeripheral("esphome:aa", provides=[Endpoint("ble", "esphome-proxy", "esphome:aa")])
    router = FakePeripheral("router", [Need("ble", ["esphome-proxy"], is_many=True, is_required=False)], answer=True)
    runtime = await run(tmp_path, router, esphome)

    esphome.provided.append(proxy)  # a node connected
    await changed(runtime, esphome)
    assert state(runtime, proxy) == S.RUNNING
    assert router.peripherals.endpoints("ble") == list(proxy.provides)

    await runtime.disable(esphome)  # it holds on to it: it goes all the same
    assert proxy not in runtime.peripherals
    assert proxy.calls == ["start", "stop"]
    assert router.peripherals.endpoints("ble") == []


async def test_what_it_brings_along_is_known_under_its_name(tmp_path):
    proxy = FakePeripheral("aa")
    esphome = FakePeripheral("esphome", provides=[proxy])
    runtime = await run(tmp_path, esphome)
    assert runtime.name(proxy) == "esphome/aa" and runtime.peripheral("esphome/aa") is proxy


async def test_each_one_keeps_its_data_in_a_folder_of_its_own(tmp_path):
    proxy = FakePeripheral("aa")
    esphome = FakePeripheral("esphome", provides=[proxy])
    await run(tmp_path, esphome)
    assert esphome.data == tmp_path / "data" / "peripherals" / "esphome"
    assert proxy.data == tmp_path / "data" / "peripherals" / "esphome" / "aa"


async def test_the_owner_hears_when_one_goes(tmp_path):
    output, radio = RecordingRuntimeOutput(), FakePeripheral("radio")
    scanner = FakePeripheral("usb", provides=[radio])
    runtime = await run(tmp_path, scanner, output=output)

    scanner.provided.clear()  # unplugged
    await changed(runtime, scanner)
    assert output.disappeared == [radio]
    assert radio not in runtime.peripherals


async def test_one_that_failed_stays_failed_when_brought_along_anew(tmp_path):
    esphome = FakePeripheral("esphome", provides=[FakePeripheral("aa", failures=10)])
    runtime = await run(tmp_path, esphome)
    await until(runtime, lambda: state(runtime, runtime.peripheral("esphome/aa")) == S.FAILED)

    again = FakePeripheral("aa")  # the node connected again
    esphome.provided[:] = [again]
    await changed(runtime, esphome)
    assert runtime.status(again) == PeripheralStatus(S.FAILED, K.GAVE_UP, "aa broke")

    await runtime.restart(again)
    await settle(runtime)
    assert state(runtime, again) == S.RUNNING


async def test_a_second_one_of_a_name_fails_saying_why(tmp_path):
    first = FakePeripheral("mqtt", provides=[Endpoint("mqtt", "a", "x", is_shared=True)], is_on_demand=True)
    second = FakePeripheral("mqtt", provides=[Endpoint("mqtt", "b", "y", is_shared=True)], is_on_demand=True)
    one, two = FakePeripheral("one", [Need("mqtt", ["a"])]), FakePeripheral("two", [Need("mqtt", ["b"])])
    runtime = await run(tmp_path, first, one, second, two)

    assert runtime.status(second) == PeripheralStatus(S.FAILED, K.NAME_TAKEN, "another peripheral is named mqtt")
    assert second in runtime.peripherals
    assert (state(runtime, one), runtime.status(two)) == (
        S.RUNNING,
        PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, "need mqtt/b: nothing offers it"),
    )


async def test_what_uses_a_provider_stops_before_it(tmp_path):
    stopped: list[str] = []

    class Logged(FakePeripheral):
        async def stop(self):
            stopped.append(self.name)

    server = Logged("matter-server", provides=[SERVER], is_on_demand=True)
    bridge = Logged("bridge", [Need("matter")], [Endpoint("bridge", is_shared=True)], is_on_demand=True)
    app = Logged("app", [Need("bridge")])
    runtime = await run(tmp_path, app, bridge, server)  # listed users first

    await runtime.disable(app)
    assert stopped == ["app", "bridge", "matter-server"]

    await runtime.enable(app)
    await settle(runtime)
    stopped.clear()
    await runtime.stop()
    assert stopped == ["app", "bridge", "matter-server"]


# on demand


async def test_one_on_demand_runs_only_while_something_that_runs_binds_it(tmp_path):
    router = FakePeripheral("router", provides=[Endpoint("ble", "router", is_shared=True)], is_on_demand=True)
    matter = FakePeripheral("matter", [Need("ble", ["router"], is_required=False)])
    runtime = await run(tmp_path, router, matter)
    assert state(runtime, router) == S.RUNNING

    await runtime.disable(matter)
    assert runtime.status(router) == PeripheralStatus(S.WAITING, K.UNUSED, "nothing that runs uses it")
    assert router.calls == ["start", "stop"]


async def test_one_on_demand_that_nothing_binds_never_starts(tmp_path):
    router = FakePeripheral("router", provides=[Endpoint("ble", "router", is_shared=True)], is_on_demand=True)
    runtime = await run(tmp_path, router)
    assert runtime.status(router) == PeripheralStatus(S.WAITING, K.UNUSED, "nothing that runs uses it")
    assert router.calls == []


async def test_one_on_demand_stops_once_what_needs_it_cant_run(tmp_path):
    server = FakePeripheral("matter-server", provides=[SERVER], is_on_demand=True)
    matter = FakePeripheral("matter", [Need("matter"), Need("zigbee")])
    runtime = await run(tmp_path, server, matter)
    assert state(runtime, server) == S.WAITING  # matter waits for zigbee: no reason to run its server
    assert runtime.status(matter).detail == UNBOUND


# readiness


class Gated(FakePeripheral):
    """Serves once ``gate`` opens: its start returns then."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()

    async def start(self):
        await super().start()
        await self.gate.wait()


async def test_what_needs_a_provider_starts_once_its_start_returned(tmp_path):
    server = Gated("matter-server", provides=[SERVER], is_on_demand=True)
    matter = FakePeripheral("matter", [Need("matter")])
    runtime = await run(tmp_path, server, matter, settled=False)
    await until(runtime, lambda: state(runtime, server) == S.STARTING, settled=False)
    assert runtime.status(matter) == PeripheralStatus(S.WAITING, K.NEEDS_STARTING, "waiting for matter-server")

    server.gate.set()
    await settle(runtime)
    assert state(runtime, matter) == S.RUNNING


async def test_a_running_peripheral_rides_out_its_provider_restarting(tmp_path):
    server = Gated("matter-server", provides=[SERVER], is_on_demand=True)
    server.gate.set()
    matter = FakePeripheral("matter", [Need("matter")])
    runtime = await run(tmp_path, server, matter)

    server.gate.clear()
    await runtime.restart(server)
    assert state(runtime, server) == S.STARTING
    assert state(runtime, matter) == S.RUNNING and matter.calls == ["start"]
    server.gate.set()


async def test_an_optional_need_bound_to_a_provider_still_starting_doesnt_hold_it_back(tmp_path):
    border = Gated("otbr", provides=[THREAD], is_on_demand=True)
    matter = FakePeripheral("matter", [Need("thread", is_required=False)])
    runtime = await run(tmp_path, border, matter, settled=False)
    await until(runtime, lambda: state(runtime, matter) == S.RUNNING, settled=False)
    assert state(runtime, border) == S.STARTING
    assert matter.peripherals.endpoint("thread") == THREAD  # handed over at once: it copes until it answers
    border.gate.set()


# a start cut short


async def starting(tmp_path, *peripherals: FakePeripheral):
    """A runtime of ``peripherals`` with the last one in the middle of its start, held there."""
    runtime = await run(tmp_path, *peripherals, settled=False)
    await until(runtime, lambda: peripherals[-1].calls == ["start"], settled=False)
    return runtime


async def test_a_start_cut_short_by_disabling_it_stays_disabled(tmp_path):
    slow = Gated("slow")
    runtime = await starting(tmp_path, slow)
    await runtime.disable(slow)
    slow.gate.set()
    await settle(runtime)
    assert (state(runtime, slow), slow.calls) == (S.DISABLED, ["start", "stop"])


async def test_a_start_cut_short_by_a_restart_or_new_settings_starts_anew(tmp_path):
    slow = Gated("slow", settings=Channel)
    runtime = await starting(tmp_path, slow)
    await runtime.restart(slow)
    await until(runtime, lambda: slow.calls == ["start", "stop", "start"], settled=False)
    await runtime.update_settings(slow, {"channel": 15})
    slow.gate.set()
    await settle(runtime)
    assert (state(runtime, slow), slow.calls) == (S.RUNNING, ["start", "stop", "start", "stop", "start"])


async def test_a_start_cut_short_by_losing_its_need_waits_for_it(tmp_path):
    stick, slow = radio(ZIGBEE), Gated("zigbee", [Need("zigbee")])
    runtime = await starting(tmp_path, stick, slow)
    stick.provided = []
    await changed(runtime, stick)
    assert runtime.status(slow) == PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, UNBOUND)
    assert slow.calls == ["start", "stop"]


async def test_a_start_cut_short_by_the_runtime_stopping_is_stopped(tmp_path):
    slow = Gated("slow")
    runtime = await starting(tmp_path, slow)
    await runtime.stop()
    assert slow.calls == ["start", "stop"]


async def test_one_replaced_by_another_of_its_name_goes_and_the_other_runs(tmp_path):
    output, old, new = RecordingRuntimeOutput(), FakePeripheral("aa"), FakePeripheral("aa")
    esphome = FakePeripheral("esphome", provides=[old])
    runtime = await run(tmp_path, esphome, output=output)

    esphome.provided[:] = [new]  # the node reconnected
    await changed(runtime, esphome)
    assert (old.calls, output.disappeared) == (["start", "stop"], [old])
    assert state(runtime, new) == S.RUNNING and runtime.peripheral("esphome/aa") is new


# settings and context


async def test_settings_are_kept_and_handed_over_or_restart_it(tmp_path):
    class Channel(BaseModel):
        channel: int = 11

    integration = FakePeripheral("zigbee", settings=Channel, answer=True)
    runtime = await run(tmp_path, integration)
    assert integration.settings == Channel()

    assert await runtime.update_settings(integration, {"channel": 15}) == Channel(channel=15)
    assert integration.changes == [("settings", Channel())]
    assert integration.calls == ["start"]

    integration.answer = False
    await runtime.update_settings(integration, {"channel": 20})
    await settle(runtime)
    assert integration.calls == ["start", "stop", "start"]

    again = FakePeripheral("zigbee", settings=Channel)
    await run(tmp_path, again)  # the same data: after a reboot
    assert again.settings == Channel(channel=20)


async def test_one_without_settings_has_none_to_change(tmp_path):
    host = FakePeripheral("host")
    runtime = await run(tmp_path, host)
    assert host.settings is None
    with pytest.raises(KeyError):
        await runtime.update_settings(host, {"x": 1})


async def test_a_changed_context_is_handed_over_or_restarts_it(tmp_path):
    handles, restarts = FakePeripheral("handles", answer=True), FakePeripheral("restarts")
    runtime = await run(tmp_path, handles, restarts)

    await runtime.context_did_change(Context(region="Europe"))
    await settle(runtime)
    assert handles.changes == restarts.changes == [("context", Context(region="Europe"))]
    assert (handles.calls, restarts.calls) == (["start"], ["start", "stop", "start"])


async def test_a_changed_context_starts_anew_one_still_starting(tmp_path):
    gate = asyncio.Event()

    class Slow(FakePeripheral):
        async def start(self):
            await super().start()
            await gate.wait()

    slow = Slow("slow")
    runtime = Runtime(tmp_path / "data", [slow])
    await runtime.start()
    await asyncio.sleep(0)
    assert state(runtime, slow) == S.STARTING

    await runtime.context_did_change(Context(region="Europe"))  # it may have read the one before
    gate.set()
    await settle(runtime)
    assert slow.calls == ["start", "stop", "start"] and state(runtime, slow) == S.RUNNING
    await runtime.stop()


async def test_any_peripheral_reads_the_context_and_tells_what_it_learned(tmp_path):
    output, region = RecordingRuntimeOutput(), Context(region="Europe")
    router = FakePeripheral("my-border-router")  # one of the user's own
    await run(tmp_path, router, output=output, context=lambda: region)
    assert router.runtime.context == region

    await router.runtime.peripheral_did_learn(router, {"thread_dataset": "0e08"})
    assert output.learned == [("my-border-router", {"thread_dataset": "0e08"})]


async def test_one_added_while_it_runs_runs_and_one_removed_is_gone(tmp_path):
    output, hue = RecordingRuntimeOutput(), FakePeripheral("hue")
    runtime = await run(tmp_path, FakePeripheral("host"), output=output)

    await runtime.add(hue)  # installed while the hub runs
    await settle(runtime)
    assert state(runtime, hue) == S.RUNNING

    await runtime.remove(hue)
    assert hue.calls == ["start", "stop"] and hue not in runtime.peripherals
    assert output.disappeared == [hue]


# one that misbehaves stops nothing else


class Channel(BaseModel):
    channel: int = 11


async def test_invalid_stored_settings_fail_only_their_peripheral(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps({"settings": {"zigbee": {"channel": "eleven"}}}))
    host, integration = FakePeripheral("host"), FakePeripheral("zigbee", settings=Channel)
    runtime = await run(tmp_path, host, integration)
    assert state(runtime, host) == S.RUNNING
    assert runtime.status(integration) == PeripheralStatus(
        S.FAILED,
        K.INVALID_SETTINGS,
        "Invalid settings: channel: Input should be a valid integer, unable to parse string as an integer",
    )

    await runtime.update_settings(integration, {"channel": 15})
    await settle(runtime)
    assert state(runtime, integration) == S.RUNNING and integration.settings == Channel(channel=15)


async def test_one_that_cant_say_what_it_provides_provides_nothing(tmp_path):
    class Broken(FakePeripheral):
        @property
        def provides(self):
            raise RuntimeError("confused")

    host, integration = FakePeripheral("host"), zigbee()
    runtime = await run(tmp_path, host, Broken("broken"), integration)
    assert state(runtime, host) == S.RUNNING
    assert runtime.status(integration) == PeripheralStatus(S.WAITING, K.NEEDS_UNBOUND, UNBOUND)


async def test_an_output_that_raises_stops_nothing(tmp_path):
    class Output(RecordingRuntimeOutput):
        def peripheral_did_change_status(self, peripheral, status):
            raise RuntimeError("the Hub broke")

    host = FakePeripheral("host")
    runtime = await run(tmp_path, host, output=Output())
    assert state(runtime, host) == S.RUNNING


async def test_a_corrupt_state_starts_afresh(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text('{"settings": {"zigbee": {"chan')  # cut by a crash
    integration = FakePeripheral("zigbee", settings=Channel)
    runtime = await run(tmp_path, integration)
    assert state(runtime, integration) == S.RUNNING and integration.settings == Channel()


async def test_the_state_is_written_whole_or_not_at_all(tmp_path, monkeypatch):
    integration = FakePeripheral("zigbee", settings=Channel)
    runtime = await run(tmp_path, integration)
    await runtime.update_settings(integration, {"channel": 15})
    kept = (tmp_path / "data" / "state.json").read_text()

    def crash(self, text: str):
        open(self, "w").close()  # the file is cut, then the power goes
        raise OSError("power lost")

    monkeypatch.setattr(type(tmp_path), "write_text", crash)
    with pytest.raises(OSError):
        await runtime.update_settings(integration, {"channel": 20})
    assert (tmp_path / "data" / "state.json").read_text() == kept
