import asyncio
import logging
import os
import time
from typing import Dict, Set

from bleak import BleakClient
from bleak.backends import BleakBackend
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice
from dbus_utils import remove_device, reset_adapter
from event_broadcaster import EventBroadcaster
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Recovery tunables. A short-lived connection (connect succeeds then drops
# within SHORT_CONNECTION_SECONDS) counts as a failure.
#
# On Linux kernel 6.x, BlueZ cannot connect to a device that has aged out of
# its cache, even while an active scan is running (bleak #1244) — the connect
# fails with TimeoutError or "device '...' not found". The reliable fix is to
# RemoveDevice from BlueZ's cache, forcing a fresh re-discovery on the next
# advertisement. Because that's the *common* cause on our edge kernel (not a
# rare wedge), we remove on the very first failure rather than waiting for a
# streak to build up.
SHORT_CONNECTION_SECONDS = 15
REMOVE_DEVICE_THRESHOLD = 1
RESET_ADAPTER_THRESHOLD = 6
EXIT_THRESHOLD = 10

# Reconnect-backoff tunables. Connects are serialized through a single lock,
# so a flaky sensor that keeps dropping can monopolise the slot and starve
# healthy ones. Each failure event bumps a per-device flakiness score; the
# reconnect delay grows exponentially with that score (base * 2**score),
# capped at RECONNECT_MAX_SECONDS. The score decays with wall-clock time
# (halving every FLAKINESS_HALF_LIFE_SECONDS), so a device that recovers
# drifts back to the base delay on its own — no explicit reset needed.
RECONNECT_BASE_SECONDS = 5
RECONNECT_MAX_SECONDS = 60
FLAKINESS_HALF_LIFE_SECONDS = 300

# Every GATT operation is a D-Bus round-trip to BlueZ, and BlueZ does not always
# reply. If the device disconnects mid-call — or the device object is removed
# from BlueZ's cache while a call is in flight, which is exactly what our own
# recovery path does on failure — the await can hang forever. That leaves the
# HTTP request open indefinitely, and the backend blocked on it. Bound every
# GATT call so a wedged operation surfaces as an error instead.
GATT_OP_TIMEOUT_SECONDS = 10


def _log_connect_error(mac: str, exc: BaseException) -> None:
    """
    Log a connect failure. Routine BLE errors (BlueZ cache misses, timeouts)
    are fully described by their message, so only unexpected ones get a
    traceback.
    """
    logger.error(
        f"Error connecting to {mac}: {exc}",
        exc_info=not isinstance(exc, (BleakError, asyncio.TimeoutError)),
    )


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


class BLEConnectionsManager:
    """
    Manages and maintains active BLE connections. It handles:
      - Desired connection set
      - On-demand connections to devices
      - Reading/writing/notifications for connected devices
    """

    def __init__(self, event_broadcaster: EventBroadcaster) -> None:
        """
        :param event_broadcaster: The broadcaster for SSE or similar event flow.
        """
        self.event_broadcaster = event_broadcaster

        # The set of MAC addresses we want to keep connected
        self.desired_connections: Set[str] = set()

        # Current connected device clients
        self.connected_devices: Dict[str, BleakClient] = {}

        # Lock to prevent race conditions when scanning and connecting
        self.connection_lock = asyncio.Lock()

        # Store device names separately from the BleakClient objects
        self.device_names: Dict[str, str] = {}

        # Store devices keyed by MAC
        self.known_devices: Dict[str, BLEDevice] = {}

        # Per-MAC consecutive failure count, driving the recovery escalation
        # ladder (see _register_failure).
        self.connection_failures: Dict[str, int] = {}

        # Per-MAC monotonic timestamp of the most recent successful connect.
        # Used to classify short-lived drops (< SHORT_CONNECTION_SECONDS) as
        # failures and long-lived drops as benign.
        self.connect_times: Dict[str, float] = {}

        # Per-MAC flakiness score and the monotonic time it was last updated.
        # Distinct from connection_failures (which drives the recovery ladder
        # and gets reset by recovery steps): this score is never reset by
        # recovery, only decayed with time, so it reflects a device's *recent*
        # reliability and feeds the reconnect backoff that deprioritises flaky
        # sensors without permanently starving them.
        self.flakiness: Dict[str, float] = {}
        self.flakiness_updated: Dict[str, float] = {}

        # Held while a recovery step (RemoveDevice / adapter power-cycle) is
        # in flight. Prevents concurrent recoveries and pauses new connect
        # attempts so we don't race with BlueZ state being torn down.
        self.recovery_lock = asyncio.Lock()

    def add_device(self, device: BLEDevice) -> None:
        """
        Add a device to desired connections and connect if not already connected.

        :param device: BLEDevice instance. We'll use device.address as the key.
        """
        mac = device.address

        # Store device in known devices
        self.known_devices[mac] = device

        # If this is a desired connection and not already connected, connect now
        if mac not in self.desired_connections:
            self.desired_connections.add(mac)

        if mac not in self.connected_devices and mac in self.desired_connections:
            # Start a task to connect to the device
            asyncio.create_task(self._connect_to_device(device))

    async def disconnect_device(self, mac: str) -> None:
        """
        Disconnect from a specified device and remove it from desired connections.

        :param mac: The MAC address of the target device.
        """
        if mac in self.desired_connections:
            self.desired_connections.remove(mac)

        # If the device is connected, disconnect it
        if mac in self.connected_devices:
            client = self.connected_devices.pop(mac)
            try:
                # Bounded: a disconnect that never returns would otherwise hang
                # the request. The client is already dropped from
                # connected_devices, so treat a timeout like any other failure
                # and carry on to the broadcast.
                await asyncio.wait_for(
                    client.disconnect(), timeout=GATT_OP_TIMEOUT_SECONDS
                )
            except Exception as e:
                logger.debug(f"Error disconnecting {mac}: {e}")
            await self._broadcast_disconnection(mac)

    async def disconnect_all(self) -> None:
        """
        Disconnect all devices that are currently in the desired connections.
        """
        disconnect_tasks = [
            self.disconnect_device(mac) for mac in list(self.desired_connections)
        ]
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks)

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
            asyncio.create_task(
                self.event_broadcaster.broadcast(
                    {
                        "type": "notification",
                        "bdaddr": mac,
                        "characteristic": char_uuid,
                        "data": data.hex(),
                    }
                )
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
        for mac, client in list(self.connected_devices.items()):
            for service in client.services:
                for char in service.characteristics:
                    try:
                        await _gatt_op(
                            client.stop_notify(char.uuid),
                            f"stop_notify {mac}/{char.uuid}",
                        )
                    except Exception:
                        pass

    async def _connect_to_device(self, device: BLEDevice) -> None:
        """
        Connect to a specific BLE device.

        :param device: The BLE device to connect to.
        """
        mac = device.address

        if self.recovery_lock.locked():
            logger.debug(f"Skipping {mac}: BLE recovery in progress.")
            return
        if mac not in self.desired_connections:
            logger.debug(f"Skipping {mac}: not in desired connections.")
            return
        if mac in self.connected_devices:
            logger.debug(f"Skipping {mac}: already connected.")
            return
        if self.connection_lock.locked():
            logger.debug(f"Skipping {mac}: another connect attempt in progress.")
            return
        logger.debug(f"Attempting to connect to {mac}...")

        async with self.connection_lock:
            # Skip if this device is already connected
            if mac in self.connected_devices:
                return

            def _disconnection_handler(_client):
                logger.warning(f"Connection to {mac} dropped.")
                self.connected_devices.pop(mac, None)
                connect_time = self.connect_times.pop(mac, None)
                # Broadcast disconnection asynchronously
                asyncio.create_task(self._broadcast_disconnection(mac))

                # A drop within SHORT_CONNECTION_SECONDS of a successful
                # connect is the BlueZ-wedge signature; a longer-lived
                # connection ending is normal and resets the counter.
                if connect_time is not None:
                    uptime = time.monotonic() - connect_time
                    if uptime < SHORT_CONNECTION_SECONDS:
                        asyncio.create_task(self._register_failure(mac))
                    else:
                        self.connection_failures.pop(mac, None)

                # Attempt to reconnect if still desired
                if mac in self.desired_connections and mac in self.known_devices:
                    # Add a small delay before reconnection attempt
                    asyncio.create_task(
                        self._delayed_reconnect(self.known_devices[mac])
                    )

            try:

                # Attempt to connect
                client = BleakClient(
                    device, disconnected_callback=_disconnection_handler, timeout=10.0
                )
                logger.debug(f"Connecting to {mac}...")
                await client.connect()
                self.connected_devices[mac] = client

                # Try to read the device name characteristic (0x2A00)
                try:
                    # First save the advertised name as fallback
                    if device.name:
                        self.device_names[mac] = device.name

                    # Now try to read the actual characteristic
                    # 0x2A00 is the Device Name characteristic in Generic Access service
                    device_name_bytes = await client.read_gatt_char(
                        "00002a00-0000-1000-8000-00805f9b34fb"
                    )
                    if device_name_bytes:
                        try:
                            # Convert bytes to string and store it
                            device_name = device_name_bytes.decode("utf-8")
                            self.device_names[mac] = device_name
                            logger.debug(f"Read device name for {mac}: {device_name}")
                        except UnicodeDecodeError:
                            logger.warning(f"Could not decode device name for {mac}")
                except Exception as e:
                    logger.debug(
                        f"Could not read device name characteristic for {mac}: {e}"
                    )
                    # Use advertised name or address as fallback if we couldn't read the name
                    if mac not in self.device_names:
                        self.device_names[mac] = device.name or mac

                self.connect_times[mac] = time.monotonic()
                logger.info(f"Successfully connected to {mac}.")
                await self._broadcast_connection(mac, "connected")

            except Exception as e:
                _log_connect_error(mac, e)
                self.connected_devices.pop(mac, None)
                self.connect_times.pop(mac, None)
                asyncio.create_task(self._register_failure(mac))
                # Attempt to reconnect if desired
                if mac in self.desired_connections and mac in self.known_devices:
                    # Add a small delay before reconnection attempt
                    asyncio.create_task(
                        self._delayed_reconnect(self.known_devices[mac])
                    )
                else:
                    logger.debug(
                        f"Device {mac} is not in desired connections, not attempting to reconnect."
                    )
                await self._broadcast_disconnection(mac)

    async def _delayed_reconnect(self, device: BLEDevice) -> None:
        """
        Wait before attempting to reconnect to avoid rapid reconnection attempts.
        The wait scales with the device's flakiness so flaky sensors back off
        and stop starving healthy ones of the single connection slot.

        :param device: The device to reconnect to.
        """
        delay = self._reconnect_delay(device.address)
        await asyncio.sleep(delay)
        if device.address in self.desired_connections:
            await self._connect_to_device(device)

    def _current_flakiness(self, mac: str) -> float:
        """
        Return ``mac``'s flakiness score decayed to the present moment. The
        score halves every FLAKINESS_HALF_LIFE_SECONDS, so a device that stops
        failing drifts back toward zero on its own.
        """
        score = self.flakiness.get(mac, 0.0)
        if score <= 0.0:
            return 0.0
        elapsed = time.monotonic() - self.flakiness_updated.get(mac, 0.0)
        return score * (0.5 ** (elapsed / FLAKINESS_HALF_LIFE_SECONDS))

    def _bump_flakiness(self, mac: str) -> float:
        """
        Increment ``mac``'s decayed flakiness score by one and stamp it now.
        Called on every connect failure / short-lived drop.
        """
        score = self._current_flakiness(mac) + 1.0
        self.flakiness[mac] = score
        self.flakiness_updated[mac] = time.monotonic()
        return score

    def _reconnect_delay(self, mac: str) -> float:
        """
        Reconnect backoff for ``mac``: RECONNECT_BASE_SECONDS for a healthy
        device, growing exponentially with its flakiness score and capped at
        RECONNECT_MAX_SECONDS.
        """
        delay = RECONNECT_BASE_SECONDS * (2 ** self._current_flakiness(mac))
        return min(delay, RECONNECT_MAX_SECONDS)

    async def _register_failure(self, mac: str) -> None:
        """
        Record a connect failure (or short-lived drop) for ``mac`` and run the
        recovery escalation ladder when thresholds are crossed:

          * REMOVE_DEVICE_THRESHOLD — clear this device from BlueZ's cache
          * RESET_ADAPTER_THRESHOLD — power-cycle the BLE adapter
          * EXIT_THRESHOLD          — os._exit(1) so Balena restarts the container

        All state mutation and recovery actions run under ``recovery_lock``
        so concurrent failures serialize cleanly — increments aren't lost and
        only one recovery runs at a time. The counter climbs with every failure
        and escalates up the rungs; it is reset only by an adapter-reset success
        (clears all counters) or by a healthy long-lived connection later
        dropping (see ``_disconnection_handler``). RemoveDevice runs on every
        failure (it's the kernel-6.x cache-aging fix, threshold == 1) and
        deliberately does NOT reset the counter — otherwise a device stuck at
        "remove, retry, fail" would peg the counter at 1 and never escalate to
        the adapter reset / exit rungs.

        This is also the single funnel for every connect failure / short-lived
        drop, so it's where we bump the per-device flakiness score that drives
        reconnect backoff.
        """
        # Bump flakiness regardless of recovery state; the reconnect backoff
        # reads this to deprioritise this device relative to healthy ones.
        self._bump_flakiness(mac)

        async with self.recovery_lock:
            n = self.connection_failures.get(mac, 0) + 1
            self.connection_failures[mac] = n

            if n >= EXIT_THRESHOLD:
                logger.error(
                    f"BLE wedged after {n} consecutive failures for {mac}; "
                    f"exiting for container restart"
                )
                os._exit(1)
            elif n >= RESET_ADAPTER_THRESHOLD:
                logger.warning(f"BLE failure #{n} for {mac}; resetting adapter")
                # An adapter power-cycle invalidates every BleakClient and
                # wipes BlueZ's view of all devices.
                self.connected_devices.clear()
                self.connect_times.clear()
                try:
                    await reset_adapter()
                except Exception as e:
                    logger.error(f"reset_adapter failed: {e}", exc_info=True)
                else:
                    self.connection_failures.clear()
            elif n >= REMOVE_DEVICE_THRESHOLD:
                logger.warning(f"BLE failure #{n} for {mac}; removing from BlueZ cache")
                try:
                    await remove_device(mac)
                except Exception as e:
                    logger.error(f"remove_device({mac}) failed: {e}", exc_info=True)
                # Intentionally do NOT reset connection_failures here: with
                # REMOVE_DEVICE_THRESHOLD == 1 every failure removes the device,
                # and resetting would peg the counter at 1, making the
                # adapter-reset / exit rungs above unreachable. Let it climb.

    def _get_connected_client(self, mac: str) -> BleakClient:
        """
        Retrieve a connected BleakClient or raise HTTPException if not connected.
        """
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        return self.connected_devices[mac]

    async def _broadcast_connection(self, mac: str, status: str) -> None:
        """
        Helper to broadcast a connection-status event.
        """
        await self.event_broadcaster.broadcast(
            {"type": "connection", "bdaddr": mac, "status": status}
        )

    async def _broadcast_disconnection(self, mac: str) -> None:
        """
        Helper to broadcast a disconnection event.
        """
        await self._broadcast_connection(mac, "disconnected")

    async def get_connected_devices(self) -> Dict[str, str]:
        """
        Return a dictionary of connected devices (MAC: name).
        """
        return {
            mac: name
            for mac, name in self.device_names.items()
            if mac in self.connected_devices
        }
