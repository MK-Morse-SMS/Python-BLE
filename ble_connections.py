import asyncio
import contextlib
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from bleak import BleakClient
from bleak.backends import BleakBackend
from bleak.exc import BleakDeviceNotFoundError, BleakError
from fastapi import HTTPException

import dbus_utils
from event_broadcaster import EventBroadcaster

logger = logging.getLogger(__name__)

# bleak's connect timeout covers the link *and* service discovery. Sensors ask
# for a 300-330 ms connection interval as soon as they connect, so discovering
# an uncached GATT database can run well past bleak's 10 s default.
CONNECT_TIMEOUT_SECONDS = 25
# Bounds the whole connect() call. bleak's own timeout does not cover its
# cleanup, which awaits a BlueZ Disconnect reply that may never come.
CONNECT_HARD_TIMEOUT_SECONDS = CONNECT_TIMEOUT_SECONDS + 10

# A connection that drops sooner than this counts as a failed connect.
SHORT_CONNECTION_SECONDS = 15

# Per-sensor recovery, driven by consecutive failures (timeouts, errors,
# short-lived connections). A sensor BlueZ has no object for is not a failure:
# no connect attempt can fix that, only a fresh advertisement can. RemoveDevice
# also discards BlueZ's GATT cache, forcing a full discovery on the next
# connect, so it is not done on every failure.
REMOVE_DEVICE_EVERY = 3
QUARANTINE_EVERY = 6
QUARANTINE_SECONDS = 300

# Reconnect backoff: base * 2**flakiness, capped. Flakiness rises by one per
# failure and halves every half-life, so a recovered sensor drifts back to the
# base delay on its own.
RECONNECT_BASE_SECONDS = 5
RECONNECT_MAX_SECONDS = 60
FLAKINESS_HALF_LIFE_SECONDS = 300

# Power-cycling the adapter drops every sensor, so it happens only when nothing
# has been connected for ADAPTER_STALL_SECONDS despite ADAPTER_STALL_MIN_FAILURES
# failed attempts, and never more often than ADAPTER_RESET_MIN_INTERVAL_SECONDS.
# If EXIT_AFTER_FAILED_RESETS resets in a row bring nothing back, exit so Balena
# restarts the container.
ADAPTER_STALL_SECONDS = 600
ADAPTER_STALL_MIN_FAILURES = 6
ADAPTER_RESET_MIN_INTERVAL_SECONDS = 900
EXIT_AFTER_FAILED_RESETS = 2

# A wanted sensor missing from BlueZ this long gets a fresh scan session, which
# is what gets it rediscovered. The interval doubles while nothing turns up;
# past the cap the scanner's regular cycle takes over.
REDISCOVERY_RESTART_SECONDS = 60
REDISCOVERY_RESTART_MAX_SECONDS = 240

# Every GATT operation is a D-Bus round-trip to BlueZ, and BlueZ does not always
# reply. If the device disconnects mid-call — or the device object is removed
# from BlueZ's cache while a call is in flight — the await can hang forever.
# That leaves the HTTP request open indefinitely, and the backend blocked on it.
# Bound every GATT call so a wedged operation surfaces as an error instead.
GATT_OP_TIMEOUT_SECONDS = 10
DBUS_OP_TIMEOUT_SECONDS = 15
SUPERVISOR_TICK_SECONDS = 1.0

_DEVICE_NOT_FOUND = re.compile(r"device '[^']*' not found")


async def _gatt_op(coro, description: str):
    """
    Await a GATT coroutine under a timeout.

    :param coro: The coroutine performing the GATT operation.
    :param description: Human-readable operation description, used in the error.
    :raises HTTPException: 504 if the operation did not complete in time.
    """
    try:
        return await asyncio.wait_for(coro, timeout=GATT_OP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            f"GATT operation timed out after {GATT_OP_TIMEOUT_SECONDS}s: {description}"
        )
        raise HTTPException(
            status_code=504,
            detail=f"GATT operation timed out: {description}",
        )


def _classify_failure(exc: BaseException, link_up: bool) -> str:
    """Name the kind of connect failure, for counters, logs and /health."""
    if isinstance(exc, BleakDeviceNotFoundError) or (
        isinstance(exc, BleakError) and _DEVICE_NOT_FOUND.search(str(exc))
    ):
        return "not_in_bluez"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout_link_up" if link_up else "timeout"
    return "dropped_during_connect" if link_up else "error"


def _fmt_duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f} min"


@dataclass
class DeviceState:
    """Everything pyble tracks about one sensor."""

    mac: str
    client: Optional[BleakClient] = None
    connected_at: Optional[float] = None
    connecting: bool = False
    next_attempt_at: float = 0.0
    last_attempt_started: float = 0.0
    # Set while BlueZ has no object for the sensor; cleared when it reappears.
    missing_since: Optional[float] = None
    pending_remove: bool = False
    consecutive_failures: int = 0
    flakiness: float = 0.0
    flakiness_updated: float = 0.0
    last_seen: Optional[float] = None
    rssi: Optional[int] = None
    adv_name: Optional[str] = None
    attempts: int = 0
    connects: int = 0
    drops: int = 0
    missing_events: int = 0
    failures: Counter = field(default_factory=Counter)
    last_error: Optional[str] = None
    last_attempt: Optional[Dict[str, Any]] = None


@dataclass
class _Attempt:
    link_up: bool = False


class BLEConnectionsManager:
    """
    Keeps the desired set of sensors connected, and serves GATT operations on
    them.

    One supervisor task makes every connect attempt and runs every recovery
    step, one at a time, under ``radio_lock`` (shared with the scanner's
    session restarts). Advertisements and drops only update per-sensor state
    and wake the supervisor, so backoff is authoritative and a retry can be
    neither lost nor duplicated.
    """

    def __init__(
        self,
        event_broadcaster: EventBroadcaster,
        radio_lock: Optional[asyncio.Lock] = None,
        bluez: Any = dbus_utils,
        client_factory: Callable[..., BleakClient] = BleakClient,
    ) -> None:
        """
        :param event_broadcaster: The broadcaster for SSE or similar event flow.
        :param radio_lock: Lock serializing connects, recovery and scan restarts.
        :param bluez: BlueZ operations (``dbus_utils``; replaceable in tests).
        :param client_factory: BleakClient or a stand-in with the same signature.
        """
        self.event_broadcaster = event_broadcaster
        self.radio_lock = radio_lock or asyncio.Lock()
        self._bluez = bluez
        self._client_factory = client_factory

        # The set of MAC addresses we want to keep connected
        self.desired_connections: Set[str] = set()
        self.devices: Dict[str, DeviceState] = {}
        self.device_names: Dict[str, str] = {}

        # Wired up by BLEManager to the scanner.
        self.request_scan_restart: Callable[[str], None] = lambda reason: None
        self.scan_session_age: Callable[[], Optional[float]] = lambda: None
        self.scan_ready_for_connects: Callable[[], bool] = lambda: True

        self._wake = asyncio.Event()
        self._supervisor: Optional[asyncio.Task] = None

        self._last_healthy = time.monotonic()
        self._stall_failures = 0
        self._last_reset_at: Optional[float] = None
        self._resets_since_success = 0
        self.reset_count = 0
        self.last_reset_reason: Optional[str] = None
        self._last_rediscovery_restart: Optional[float] = None
        self._rediscovery_interval = REDISCOVERY_RESTART_SECONDS

    # --- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the supervisor. Requires a running event loop."""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise(), name="ble-supervisor")

    async def stop(self) -> None:
        """Stop the supervisor; existing connections are left alone."""
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None

    # --- Inputs --------------------------------------------------------------

    def desire(self, mac: str) -> None:
        """Add ``mac`` to the set of sensors to keep connected."""
        mac = mac.upper()
        self.desired_connections.add(mac)
        self.devices.setdefault(mac, DeviceState(mac))
        self._wake.set()

    def on_advertisement(self, device, rssi: Optional[int]) -> None:
        """Record an advertisement from the scanner. Ignores unwanted devices."""
        st = self.devices.get(device.address.upper())
        if st is None:
            return
        st.last_seen = time.monotonic()
        if rssi is not None:
            st.rssi = rssi
        if device.name:
            st.adv_name = device.name
        if st.client is None:
            self._wake.set()

    async def disconnect_device(self, mac: str) -> None:
        """
        Disconnect from a specified device and remove it from desired connections.

        :param mac: The MAC address of the target device.
        """
        mac = mac.upper()
        self.desired_connections.discard(mac)
        st = self.devices.get(mac)
        if st is None or st.client is None:
            return

        client, st.client, st.connected_at = st.client, None, None
        try:
            # Bounded: a disconnect that never returns would otherwise hang
            # the request.
            await asyncio.wait_for(client.disconnect(), timeout=GATT_OP_TIMEOUT_SECONDS)
        except Exception as e:
            logger.debug(f"Error disconnecting {mac}: {e}")
        self._publish_connection(mac, "disconnected")

    async def disconnect_all(self) -> None:
        """
        Disconnect all devices that are currently in the desired connections.
        """
        await asyncio.gather(
            *(self.disconnect_device(mac) for mac in list(self.desired_connections))
        )

    # --- GATT operations -----------------------------------------------------

    async def read_characteristic(self, mac: str, char_uuid: str) -> str:
        """
        Read a characteristic from a connected device, returning hex data.

        :param mac: MAC of the device
        :param char_uuid: Characteristic UUID
        :return: Hexadecimal string of the read value
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        try:
            value = await _gatt_op(
                client.read_gatt_char(char_uuid), f"read {mac}/{char_uuid}"
            )
            return value.hex()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def write_characteristic(
        self, mac: str, char_uuid: str, value: bytes, response: bool = False
    ) -> None:
        """
        Write a value to a characteristic on a connected device.

        :param mac: MAC of the device
        :param char_uuid: Characteristic UUID
        :param value: The bytes to write to the characteristic
        :param response: If True, wait for a response from the device (if supported)
        :raises HTTPException: If device not connected or write fails
        """
        client = self._get_connected_client(mac)
        try:
            await _gatt_op(
                client.write_gatt_char(char_uuid, value, response=response),
                f"write {mac}/{char_uuid}",
            )
            logger.debug(f"Wrote to {mac} on {char_uuid}: {value.hex()}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def get_mtu(self, mac: str) -> int:
        """
        Get the Maximum Transmission Unit (MTU) size for a connected device.

        :param mac: MAC of the device
        :return: MTU size as an integer
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        if client.backend_id == BleakBackend.BLUEZ_DBUS:
            await _gatt_op(
                client._backend._acquire_mtu(),  # type: ignore
                f"acquire_mtu {mac}",
            )
        try:
            mtu = client.mtu_size
            logger.debug(f"MTU for {mac}: {mtu}")
            return mtu
        except Exception as e:
            logger.error(f"Failed to get MTU for {mac}: {e}")
            raise HTTPException(status_code=400, detail="Failed to get MTU size")

    async def list_characteristics(self, mac: str):
        """
        Return a list of all characteristics for a connected device.

        :param mac: MAC address of the device
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        characteristics = []
        for service in client.services:
            for char in service.characteristics:
                characteristics.append(
                    {
                        "uuid": char.uuid,
                        "properties": char.properties,
                        "description": getattr(char, "description", ""),
                    }
                )
        logger.debug(f"Characteristics for {mac}: {characteristics}")
        return characteristics

    async def enable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Enable notifications on a specific characteristic of a connected device.

        :param mac: MAC address of the device
        :param char_uuid: UUID of the characteristic
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        mac = mac.upper()

        # Try disabling notifications first to avoid potential issues
        try:
            await _gatt_op(
                client.stop_notify(char_uuid), f"stop_notify {mac}/{char_uuid}"
            )
        except HTTPException:
            # A timeout here means BlueZ is not answering for this device; the
            # start_notify below would hang the same way, so give up now rather
            # than holding the request open for a second timeout.
            raise
        except Exception:
            pass

        def notification_handler(sender, data):
            self.event_broadcaster.publish(
                {
                    "type": "notification",
                    "bdaddr": mac,
                    "characteristic": char_uuid,
                    "data": data.hex(),
                }
            )

        try:
            await _gatt_op(
                client.start_notify(char_uuid, notification_handler),
                f"start_notify {mac}/{char_uuid}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        logger.debug(f"Enabled notifications for {mac} on {char_uuid}")

    async def disable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Disable notifications on a specific characteristic of a connected device.

        :param mac: MAC address of the device
        :param char_uuid: UUID of the characteristic
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        try:
            await _gatt_op(
                client.stop_notify(char_uuid), f"stop_notify {mac}/{char_uuid}"
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def disable_all_notifications(self) -> None:
        """
        Disable all notifications on all connected devices.
        """
        for st in list(self.devices.values()):
            client = st.client
            if client is None:
                continue
            for service in client.services:
                for char in service.characteristics:
                    try:
                        await _gatt_op(
                            client.stop_notify(char.uuid),
                            f"stop_notify {st.mac}/{char.uuid}",
                        )
                    except Exception:
                        pass

    async def get_connected_devices(self) -> Dict[str, str]:
        """
        Return a dictionary of connected devices (MAC: name).
        """
        return {
            mac: self.device_names.get(mac, mac)
            for mac, st in self.devices.items()
            if st.client is not None
        }

    def _get_connected_client(self, mac: str) -> BleakClient:
        """
        Retrieve a connected BleakClient or raise HTTPException if not connected.
        """
        st = self.devices.get(mac.upper())
        if st is None or st.client is None:
            raise HTTPException(status_code=404, detail="Device not connected")
        return st.client

    # --- Supervisor ----------------------------------------------------------

    async def _supervise(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), SUPERVISOR_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BLE supervisor tick failed")
                await asyncio.sleep(SUPERVISOR_TICK_SECONDS)

    async def _tick(self) -> None:
        now = time.monotonic()
        wanted = [self.devices[mac] for mac in sorted(self.desired_connections)]

        if any(st.client is not None for st in wanted):
            self._last_healthy = now
            self._stall_failures = 0
        for st in wanted:
            if (
                st.client is not None
                and st.consecutive_failures
                and now - st.connected_at >= SHORT_CONNECTION_SECONDS
            ):
                st.consecutive_failures = 0

        for st in wanted:
            if st.pending_remove:
                async with self.radio_lock:
                    await self._remove_from_bluez(st)

        if self._adapter_stalled(now):
            await self._recover_adapter(now)
            return

        await self._refresh_missing(wanted)
        self._maybe_restart_scan_for_missing(wanted, time.monotonic())

        # Kernel 6.x BlueZ can't connect without discovery running.
        if not self.scan_ready_for_connects():
            return
        st = self._next_candidate(wanted, time.monotonic())
        if st is not None:
            async with self.radio_lock:
                await self._attempt(st)
            # Another sensor may be waiting; don't sit out the tick.
            self._wake.set()

    def _next_candidate(self, wanted: List[DeviceState], now: float) -> Optional[DeviceState]:
        candidates = [
            st
            for st in wanted
            if st.client is None
            and not st.connecting
            and st.missing_since is None
            and now >= st.next_attempt_at
        ]
        # Least recently tried first, so one sensor can't hog the radio.
        return min(candidates, key=lambda st: st.last_attempt_started, default=None)

    async def _attempt(self, st: DeviceState) -> None:
        mac = st.mac
        try:
            device = await self._bluez.bluez_device(mac)
        except Exception as e:
            error = str(e) or type(e).__name__
            self._record_failure(st, "bluez_error", error)
            logger.warning(f"Connect to {mac} skipped: BlueZ unavailable ({error})")
            return
        if device is None:
            self._mark_missing(st, "no BlueZ object")
            return

        attempt = _Attempt()
        client: Optional[BleakClient] = None

        def on_disconnect(_client: BleakClient) -> None:
            self._on_disconnect(st, client, attempt)

        client = self._client_factory(
            device, disconnected_callback=on_disconnect, timeout=CONNECT_TIMEOUT_SECONDS
        )
        st.connecting = True
        st.attempts += 1
        started = st.last_attempt_started = time.monotonic()
        context = self._seen_context(st, started)
        try:
            await asyncio.wait_for(client.connect(), CONNECT_HARD_TIMEOUT_SECONDS)
        except Exception as e:
            duration = time.monotonic() - started
            kind = _classify_failure(e, attempt.link_up)
            error = str(e) or type(e).__name__
            st.last_attempt = {"outcome": kind, "duration_s": round(duration, 1), "error": error}
            if kind == "not_in_bluez":
                self._mark_missing(st, error)
                return
            self._record_failure(st, kind, error)
            phase = "after link up" if attempt.link_up else "before link up"
            logger.warning(
                f"Connect to {mac} failed after {duration:.1f}s {phase}: {error} "
                f"({context}; {st.consecutive_failures} in a row)"
            )
            return
        finally:
            st.connecting = False

        duration = time.monotonic() - started
        if mac not in self.desired_connections:
            # Disconnect was requested while connecting.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), GATT_OP_TIMEOUT_SECONDS)
            return

        now = time.monotonic()
        st.client = client
        st.connected_at = now
        st.connects += 1
        st.last_error = None
        st.last_attempt = {"outcome": "connected", "duration_s": round(duration, 1)}
        self._last_healthy = now
        self._stall_failures = 0
        self._resets_since_success = 0
        # BlueZ's Name comes from the advertisement or the GAP read it does on
        # connect, so there's no need for a GATT read of our own here.
        self.device_names[mac] = device.name or st.adv_name or mac
        logger.info(f"Connected to {mac} in {duration:.1f}s ({context})")
        self._publish_connection(mac, "connected")

    def _on_disconnect(
        self, st: DeviceState, client: Optional[BleakClient], attempt: _Attempt
    ) -> None:
        """bleak's disconnected callback for ``client``."""
        if st.client is not client:
            # Either the link dropped mid-connect (the attempt handles it), or
            # we already let go of this client (deliberate disconnect, reset).
            if st.connecting:
                attempt.link_up = True
            return

        mac = st.mac
        now = time.monotonic()
        uptime = now - (st.connected_at or now)
        st.client = None
        st.connected_at = None
        st.drops += 1
        self._publish_connection(mac, "disconnected")
        if mac not in self.desired_connections:
            return

        if uptime < SHORT_CONNECTION_SECONDS:
            self._record_failure(st, "short_connection", f"dropped after {uptime:.1f}s")
            logger.warning(
                f"Connection to {mac} dropped after {uptime:.1f}s; counting it as a "
                f"failed connect ({st.consecutive_failures} in a row)"
            )
        else:
            st.consecutive_failures = 0
            st.next_attempt_at = now + self._reconnect_delay(st, now)
            logger.warning(f"Connection to {mac} dropped after {_fmt_duration(uptime)}")
        self._wake.set()

    # --- Per-sensor recovery -------------------------------------------------

    def _record_failure(self, st: DeviceState, kind: str, error: str) -> None:
        now = time.monotonic()
        st.failures[kind] += 1
        st.consecutive_failures += 1
        st.last_error = error
        self._stall_failures += 1
        self._bump_flakiness(st, now)

        delay = self._reconnect_delay(st, now)
        n = st.consecutive_failures
        if n % QUARANTINE_EVERY == 0:
            delay = max(delay, QUARANTINE_SECONDS)
            logger.warning(
                f"{st.mac}: {n} failed connects in a row; next try in {_fmt_duration(delay)}"
            )
        if n % REMOVE_DEVICE_EVERY == 0:
            st.pending_remove = True
        st.next_attempt_at = now + delay

    def _mark_missing(self, st: DeviceState, why: str) -> None:
        if st.missing_since is None:
            st.missing_since = time.monotonic()
            st.missing_events += 1
            logger.info(f"{st.mac} is not in BlueZ ({why}); waiting for it to advertise")

    async def _refresh_missing(self, wanted: List[DeviceState]) -> None:
        for st in wanted:
            if st.missing_since is None or st.client is not None:
                continue
            try:
                device = await self._bluez.bluez_device(st.mac)
            except Exception as e:
                logger.debug(f"BlueZ lookup for {st.mac} failed: {e}")
                continue
            if device is None:
                continue
            logger.info(
                f"{st.mac} is back in BlueZ after "
                f"{_fmt_duration(time.monotonic() - st.missing_since)}"
            )
            st.missing_since = None
            self._rediscovery_interval = REDISCOVERY_RESTART_SECONDS
            self._last_rediscovery_restart = None

    def _maybe_restart_scan_for_missing(self, wanted: List[DeviceState], now: float) -> None:
        overdue = [
            st.mac
            for st in wanted
            if st.client is None
            and st.missing_since is not None
            and now - st.missing_since >= REDISCOVERY_RESTART_SECONDS
        ]
        if not overdue or self._rediscovery_interval > REDISCOVERY_RESTART_MAX_SECONDS:
            return
        session_age = self.scan_session_age()
        if session_age is None or session_age < self._rediscovery_interval:
            return
        if (
            self._last_rediscovery_restart is not None
            and now - self._last_rediscovery_restart < self._rediscovery_interval
        ):
            return
        logger.info(
            f"{', '.join(overdue)} still missing after a "
            f"{_fmt_duration(session_age)} scan session; restarting the scan"
        )
        self._last_rediscovery_restart = now
        self._rediscovery_interval *= 2
        self.request_scan_restart("rediscover missing sensors")

    async def _remove_from_bluez(self, st: DeviceState) -> None:
        st.pending_remove = False
        if st.client is not None:
            return
        logger.warning(
            f"Removing {st.mac} from BlueZ after {st.consecutive_failures} failed connects in a row"
        )
        try:
            await asyncio.wait_for(self._bluez.remove_device(st.mac), DBUS_OP_TIMEOUT_SECONDS)
        except Exception as e:
            logger.error(f"RemoveDevice({st.mac}) failed: {e}")
            return
        self._mark_missing(st, "removed")
        self.request_scan_restart("rediscover after RemoveDevice")

    def _current_flakiness(self, st: DeviceState, now: float) -> float:
        if st.flakiness <= 0.0:
            return 0.0
        elapsed = now - st.flakiness_updated
        return st.flakiness * (0.5 ** (elapsed / FLAKINESS_HALF_LIFE_SECONDS))

    def _bump_flakiness(self, st: DeviceState, now: float) -> None:
        st.flakiness = self._current_flakiness(st, now) + 1.0
        st.flakiness_updated = now

    def _reconnect_delay(self, st: DeviceState, now: float) -> float:
        delay = RECONNECT_BASE_SECONDS * (2 ** self._current_flakiness(st, now))
        return min(delay, RECONNECT_MAX_SECONDS)

    # --- Adapter-wide recovery -----------------------------------------------

    def _adapter_stalled(self, now: float) -> bool:
        if not self.desired_connections:
            return False
        if any(self.devices[mac].client is not None for mac in self.desired_connections):
            return False
        if now - self._last_healthy < ADAPTER_STALL_SECONDS:
            return False
        if self._stall_failures < ADAPTER_STALL_MIN_FAILURES:
            return False
        return (
            self._last_reset_at is None
            or now - self._last_reset_at >= ADAPTER_RESET_MIN_INTERVAL_SECONDS
        )

    async def _recover_adapter(self, now: float) -> None:
        reason = (
            f"no sensor connected for {_fmt_duration(now - self._last_healthy)} "
            f"with {self._stall_failures} failed connects"
        )
        if self._resets_since_success >= EXIT_AFTER_FAILED_RESETS:
            logger.error(
                f"BLE still stalled after {self._resets_since_success} adapter resets "
                f"({reason}); exiting for container restart"
            )
            os._exit(1)
        async with self.radio_lock:
            await self._reset_adapter(reason)

    async def _reset_adapter(self, reason: str) -> None:
        self.reset_count += 1
        self._resets_since_success += 1
        self.last_reset_reason = reason
        logger.warning(f"Resetting BLE adapter: {reason}")

        # A power-cycle invalidates every BleakClient. Let go of them first so
        # their disconnect callbacks don't count as failures.
        for st in self.devices.values():
            if st.client is not None:
                st.client = None
                st.connected_at = None
                self._publish_connection(st.mac, "disconnected")
            st.consecutive_failures = 0
            st.pending_remove = False
            st.next_attempt_at = 0.0
        try:
            await asyncio.wait_for(self._bluez.reset_adapter(), DBUS_OP_TIMEOUT_SECONDS * 2)
        except Exception as e:
            logger.error(f"Adapter reset failed: {e}")

        now = time.monotonic()
        self._last_reset_at = now
        self._last_healthy = now
        self._stall_failures = 0
        # The power-cycle ended BlueZ discovery under the scanner.
        self.request_scan_restart("adapter reset")

    # --- Reporting -----------------------------------------------------------

    def _seen_context(self, st: DeviceState, now: float) -> str:
        parts = [
            f"seen {now - st.last_seen:.0f}s ago" if st.last_seen is not None else "not seen advertising yet"
        ]
        if st.rssi is not None:
            parts.append(f"rssi {st.rssi}")
        return ", ".join(parts)

    def _publish_connection(self, mac: str, status: str) -> None:
        self.event_broadcaster.publish({"type": "connection", "bdaddr": mac, "status": status})

    def health(self) -> Dict[str, Any]:
        """Per-sensor and adapter-recovery state, for /health."""
        now = time.monotonic()

        def ago(t: Optional[float]) -> Optional[float]:
            return None if t is None else round(now - t, 1)

        devices = {}
        for mac, st in sorted(self.devices.items()):
            devices[mac] = {
                "desired": mac in self.desired_connections,
                "connected": st.client is not None,
                "connected_for_s": ago(st.connected_at),
                "connecting": st.connecting,
                "missing_for_s": ago(st.missing_since),
                "last_seen_s_ago": ago(st.last_seen),
                "rssi": st.rssi,
                "attempts": st.attempts,
                "connects": st.connects,
                "drops": st.drops,
                "missing_events": st.missing_events,
                "failures": dict(st.failures),
                "consecutive_failures": st.consecutive_failures,
                "next_attempt_in_s": (
                    max(0.0, round(st.next_attempt_at - now, 1)) if st.client is None else None
                ),
                "flakiness": round(self._current_flakiness(st, now), 2),
                "last_error": st.last_error,
                "last_attempt": st.last_attempt,
            }
        any_connected = any(
            self.devices[mac].client is not None for mac in self.desired_connections
        )
        return {
            "devices": devices,
            "adapter_resets": {
                "count": self.reset_count,
                "last_reason": self.last_reset_reason,
                "last_s_ago": ago(self._last_reset_at),
                "since_last_connect": self._resets_since_success,
            },
            "stall": {
                "no_connection_for_s": 0.0 if any_connected else ago(self._last_healthy),
                "failed_connects": self._stall_failures,
            },
        }
