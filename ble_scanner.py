import asyncio
import contextlib
import logging
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

import dbus_utils
from event_broadcaster import EventBroadcaster

logger = logging.getLogger(__name__)

SCAN_CYCLE_SECONDS = 300
WATCHDOG_INTERVAL_SECONDS = 5
# Each SSE "scan" event costs the backend a write lock, and BlueZ reports a
# sensor many times a second; one event per device per interval is plenty.
SCAN_EVENT_MIN_INTERVAL_SECONDS = 1.0
DBUS_OP_TIMEOUT_SECONDS = 15
ERROR_BACKOFF_MAX_SECONDS = 30
RESTART_PAUSE_SECONDS = 1.0


class BLEScanner:
    """
    Handles BLE scanning logic:
      - Start/stop scanning
      - Restart the discovery session on request, every scan cycle, and
        whenever BlueZ discovery stops underneath it
      - Maintain a list of discovered devices
      - Broadcast scan events
      - Notify when devices are discovered
    """

    def __init__(
        self,
        event_broadcaster: EventBroadcaster,
        radio_lock: Optional[asyncio.Lock] = None,
        scanner_factory: Callable[..., BleakScanner] = BleakScanner,
        bluez: Any = dbus_utils,
    ) -> None:
        """
        :param event_broadcaster: The event broadcaster instance for SSE or other event dissemination.
        :param radio_lock: Lock shared with the connection supervisor, so a
            scan restart never lands in the middle of a connect.
        :param scanner_factory: BleakScanner or a stand-in with the same signature.
        :param bluez: BlueZ operations (``dbus_utils``; replaceable in tests).
        """
        self.event_broadcaster = event_broadcaster
        self.radio_lock = radio_lock or asyncio.Lock()
        self._scanner_factory = scanner_factory
        self._bluez = bluez

        self.scan_filters: List[str] = []
        self.devices: Dict[str, BLEDevice] = {}
        self.device_names: Dict[str, str] = {}

        self.active_scanner: Optional[BleakScanner] = None
        self.scan_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

        # A discovery session is restarted after this long even if nothing asks.
        self.scan_cycle: float = SCAN_CYCLE_SECONDS

        # Called with (device, rssi) for every matching advertisement.
        self.device_found_callback: Optional[Callable[[BLEDevice, Optional[int]], None]] = None

        self._wanted = False
        self._restart_event = asyncio.Event()
        self._restart_reason: Optional[str] = None
        self.session_started_at: Optional[float] = None
        self.session_starts: Counter = Counter()
        self._last_scan_event: Dict[str, float] = {}

    async def start_scan(self, service_uuids: List[str]) -> Dict[str, str | List[str]]:
        """
        Begin scanning for BLE devices, optionally filtering by service UUIDs.

        :param service_uuids: A list of UUIDs to filter on. If empty, scans for all devices.
        :return: Status information about the scan.
        """
        logger.info("Starting BLE scan.")
        filters_changed = service_uuids != self.scan_filters
        self.scan_filters = service_uuids
        self._wanted = True

        if self.scan_task is None or self.scan_task.done():
            self.scan_task = asyncio.create_task(self._scan_loop(), name="ble-scan")
        elif filters_changed:
            self.request_restart("scan filters changed")
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog(), name="ble-scan-watchdog")

        return {"status": "scanning started", "filters": self.scan_filters}

    async def stop_scan(self) -> Dict[str, str]:
        """
        Stop any active scanning loop and clear filters.

        :return: Status information about the scan stop.
        """
        logger.info("Stopping BLE scan.")
        self._wanted = False
        self.scan_filters = []

        for task in (self.scan_task, self._watchdog_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=5.0)
        self.scan_task = None
        self._watchdog_task = None
        await self._stop_active()

        return {"status": "scanning stopped"}

    def request_restart(self, reason: str) -> None:
        """Ask for a fresh discovery session as soon as no connect is in flight."""
        if not self._wanted:
            return
        if self._restart_reason is None:
            self._restart_reason = reason
        self._restart_event.set()

    def ready_for_connects(self) -> bool:
        """False while a wanted scan is not running (starting, restarting, failing)."""
        if not self._wanted:
            return True
        # A pending restart usually means discovery already died (adapter reset).
        return self.session_started_at is not None and not self._restart_event.is_set()

    def session_age(self) -> Optional[float]:
        """Seconds since the current discovery session started, or None."""
        if self.session_started_at is None:
            return None
        return time.monotonic() - self.session_started_at

    def set_device_found_callback(
        self, callback: Callable[[BLEDevice, Optional[int]], None]
    ) -> None:
        """
        Set a callback to be called when a device is found during scanning.

        :param callback: A function taking the BLEDevice and its RSSI.
        """
        self.device_found_callback = callback

    def detection_callback(
        self, device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """
        The callback invoked by BleakScanner upon discovering (or seeing again) a device.

        :param device: The discovered BLEDevice.
        :param advertisement_data: Info about the advertisement packet.
        """
        # If service_uuids is not empty, we only push devices that match.
        if not device.name or (
            self.scan_filters
            and not any(
                uuid in advertisement_data.service_uuids for uuid in self.scan_filters
            )
        ):
            return

        self.devices[device.address] = device
        self.device_names[device.address] = device.name

        now = time.monotonic()
        last = self._last_scan_event.get(device.address)
        if last is None or now - last >= SCAN_EVENT_MIN_INTERVAL_SECONDS:
            self._last_scan_event[device.address] = now
            self.event_broadcaster.publish(
                {
                    "type": "scan",
                    "results": [
                        {
                            "bdaddr": device.address,
                            "name": device.name,
                            "rssi": advertisement_data.rssi,
                        }
                    ],
                }
            )

        if self.device_found_callback:
            self.device_found_callback(device, advertisement_data.rssi)

    async def _scan_loop(self) -> None:
        """
        Runs one discovery session at a time, restarting it every scan cycle or
        when request_restart() is called, until cancelled.
        """
        reason = "scan started"
        errors = 0
        while True:
            try:
                # Stop, pause and start under one hold of the lock: on kernel
                # 6.x a connect needs discovery running, so none may slip into
                # the gap between sessions.
                async with self.radio_lock:
                    if self.active_scanner is not None:
                        await self._stop_active()
                        self.devices.clear()
                        self.device_names.clear()
                        await asyncio.sleep(RESTART_PAUSE_SECONDS)
                    # Requests made before this point are served by this session.
                    self._restart_event.clear()
                    self._restart_reason = None
                    scanner = self._scanner_factory(
                        detection_callback=self.detection_callback,
                        service_uuids=self.scan_filters,
                        scanning_mode="active",
                    )
                    await asyncio.wait_for(scanner.start(), DBUS_OP_TIMEOUT_SECONDS)
                    self.active_scanner = scanner
                    self.session_started_at = time.monotonic()
                errors = 0
                self.session_starts[reason] += 1
                logger.log(
                    logging.DEBUG if reason == "cycle" else logging.INFO,
                    f"Scan session started ({reason})",
                )

                try:
                    await asyncio.wait_for(self._restart_event.wait(), self.scan_cycle)
                    reason = self._restart_reason or "restart requested"
                except asyncio.TimeoutError:
                    reason = "cycle"
            except asyncio.CancelledError:
                await self._stop_active()
                raise
            except Exception as e:
                errors += 1
                delay = min(ERROR_BACKOFF_MAX_SECONDS, 2**errors)
                # BlueZ races (e.g. "No powered Bluetooth adapters") are routine
                # here; only unexpected errors are worth a stack trace.
                logger.error(
                    f"Error in scan loop: {e}; retrying in {delay}s",
                    exc_info=not isinstance(e, (BleakError, asyncio.TimeoutError)),
                )
                await self._stop_active()
                reason = "retry after error"
                await asyncio.sleep(delay)

    async def _stop_active(self) -> None:
        scanner, self.active_scanner = self.active_scanner, None
        self.session_started_at = None
        if scanner is None:
            return
        try:
            await asyncio.wait_for(scanner.stop(), DBUS_OP_TIMEOUT_SECONDS)
        except Exception as e:
            # Expected after an adapter power-cycle, which ends discovery
            # underneath us ("No discovery started").
            logger.debug(f"Error stopping scanner: {e}")

    async def _watchdog(self) -> None:
        """
        Restart the session if BlueZ stops discovering while we think we're
        scanning, and power the adapter back on if something left it off.
        """
        misses = 0
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            try:
                # A connect or a restart in progress can legitimately pause
                # discovery; only judge an idle radio.
                if not self._wanted or self.radio_lock.locked():
                    continue
                age = self.session_age()
                if age is not None and age < WATCHDOG_INTERVAL_SECONDS:
                    continue
                state = await asyncio.wait_for(
                    self._bluez.adapter_state(), DBUS_OP_TIMEOUT_SECONDS
                )
                if state is None:
                    misses = 0
                    continue
                powered, discovering = state
                if powered and (discovering or age is None):
                    misses = 0
                    continue
                misses += 1
                if misses < 2:
                    continue
                misses = 0
                if not powered:
                    logger.warning("BLE adapter is powered off; powering it on")
                    async with self.radio_lock:
                        await asyncio.wait_for(
                            self._bluez.set_adapter_powered(True), DBUS_OP_TIMEOUT_SECONDS
                        )
                    self.request_restart("adapter was powered off")
                else:
                    logger.warning("BlueZ discovery stopped under the scanner; restarting it")
                    self.request_restart("discovery stopped")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Scan watchdog check failed: {e}")

    def health(self) -> Dict[str, Any]:
        """Scanner state, for /health."""
        age = self.session_age()
        return {
            "wanted": self._wanted,
            "running": self.session_started_at is not None,
            "session_age_s": None if age is None else round(age, 1),
            "session_starts": dict(self.session_starts),
            "filters": self.scan_filters,
        }

    async def get_device(self, mac: str) -> Optional[BLEDevice]:
        """
        Retrieve a discovered device by its MAC address.

        :param mac: The target device's MAC address.
        :return: BLEDevice if found, otherwise None.
        """
        device = self.devices.get(mac)
        if not device and self.active_scanner:
            logger.debug(f"Device {mac} not in scan cache yet.")
        return device
