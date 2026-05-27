import asyncio
import logging
import os
import time
from typing import Dict, Set

from bleak import BleakClient
from bleak.backends import BleakBackend
from bleak.backends.device import BLEDevice
from dbus_utils import remove_device, reset_adapter
from event_broadcaster import EventBroadcaster
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Recovery tunables. A short-lived connection (connect succeeds then drops
# within SHORT_CONNECTION_SECONDS) counts as a failure — that's the signature
# of BlueZ wedged state, which is the failure mode this recovery exists for.
SHORT_CONNECTION_SECONDS = 15
REMOVE_DEVICE_THRESHOLD = 3
RESET_ADAPTER_THRESHOLD = 6
EXIT_THRESHOLD = 10


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
                await client.disconnect()
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
            value = await client.read_gatt_char(char_uuid)
            return value.hex()
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
            if response:
                await client.write_gatt_char(char_uuid, value, response=True)
            else:
                await client.write_gatt_char(char_uuid, value, response=False)
            logger.info(f"Wrote to {mac} on {char_uuid}: {value.hex()}")
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
            await client._backend._acquire_mtu()  # type: ignore
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
            await client.stop_notify(char_uuid)
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
            await client.start_notify(char_uuid, notification_handler)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        logger.info(f"Enabled notifications for {mac} on {char_uuid}")

    async def disable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Disable notifications on a specific characteristic of a connected device.

        :param mac: MAC address of the device
        :param char_uuid: UUID of the characteristic
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)
        try:
            await client.stop_notify(char_uuid)
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
                        await client.stop_notify(char.uuid)
                    except Exception:
                        pass

    async def _connect_to_device(self, device: BLEDevice) -> None:
        """
        Connect to a specific BLE device.

        :param device: The BLE device to connect to.
        """
        mac = device.address

        if self.recovery_lock.locked():
            logger.info(f"Skipping {mac}: BLE recovery in progress.")
            return
        if mac not in self.desired_connections:
            logger.info(f"Skipping {mac}: not in desired connections.")
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
                            logger.info(f"Read device name for {mac}: {device_name}")
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
                logger.error(f"Error connecting to {mac}: {e}", exc_info=True)
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
                    logger.info(
                        f"Device {mac} is not in desired connections, not attempting to reconnect."
                    )
                await self._broadcast_disconnection(mac)

    async def _delayed_reconnect(self, device: BLEDevice) -> None:
        """
        Wait a bit before attempting to reconnect to avoid rapid reconnection attempts.
        :param device: The device to reconnect to.
        """
        await asyncio.sleep(5)  # Wait 5 seconds before reconnecting
        if device.address in self.desired_connections:
            await self._connect_to_device(device)

    async def _register_failure(self, mac: str) -> None:
        """
        Record a connect failure (or short-lived drop) for ``mac`` and run the
        recovery escalation ladder when thresholds are crossed:

          * REMOVE_DEVICE_THRESHOLD — clear this device from BlueZ's cache
          * RESET_ADAPTER_THRESHOLD — power-cycle the BLE adapter
          * EXIT_THRESHOLD          — os._exit(1) so Balena restarts the container

        Only one recovery runs at a time (``recovery_lock``); concurrent
        failures from other MACs just bump their counters and return.
        """
        n = self.connection_failures.get(mac, 0) + 1
        self.connection_failures[mac] = n
        logger.warning(f"BLE failure #{n} for {mac}")

        if self.recovery_lock.locked():
            return

        async with self.recovery_lock:
            if n >= EXIT_THRESHOLD:
                logger.error(
                    f"BLE wedged after {n} consecutive failures for {mac}; "
                    f"exiting for container restart"
                )
                os._exit(1)
            elif n >= RESET_ADAPTER_THRESHOLD:
                logger.warning(
                    f"Resetting BLE adapter after {n} consecutive failures for {mac}"
                )
                # An adapter power-cycle invalidates every BleakClient and
                # wipes BlueZ's view of all devices.
                self.connected_devices.clear()
                self.connect_times.clear()
                try:
                    await reset_adapter()
                except Exception as e:
                    logger.error(f"reset_adapter failed: {e}", exc_info=True)
                self.connection_failures.clear()
            elif n >= REMOVE_DEVICE_THRESHOLD:
                logger.warning(
                    f"Removing {mac} from BlueZ cache after {n} consecutive failures"
                )
                try:
                    await remove_device(mac)
                except Exception as e:
                    logger.error(f"remove_device({mac}) failed: {e}", exc_info=True)
                self.connection_failures[mac] = 0

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
