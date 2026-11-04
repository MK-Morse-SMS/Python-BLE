import asyncio
import contextlib
import logging
from typing import List, Dict, Any, Mapping, Optional

from bleak.backends.device import BLEDevice

from event_broadcaster import EventBroadcaster

from dbus_utils import adapter_state, disconnect_all_devices
from ble_scanner import BLEScanner
from ble_connections import BLEConnectionsManager

logger = logging.getLogger(__name__)

STARTUP_DISCONNECT_TIMEOUT_SECONDS = 20
HEALTH_ADAPTER_TIMEOUT_SECONDS = 5


class BLEManager:
    """
    A high-level manager that composes scanning, connection management, and
    the D-Bus disconnect utility into a single API.
    """

    def __init__(self):
        """
        Initialize the BLEManager. Call `await initialize()` once the event
        loop is running to clear stale connections and start the supervisor.
        """
        # An event broadcaster (e.g., for SSE) used by both scanner & connections
        self.event_broadcaster = EventBroadcaster()

        # Serializes connect attempts, recovery steps and scan-session restarts.
        self.radio_lock = asyncio.Lock()

        # Create sub-managers
        self.scanner = BLEScanner(
            event_broadcaster=self.event_broadcaster, radio_lock=self.radio_lock
        )
        self.connections = BLEConnectionsManager(
            event_broadcaster=self.event_broadcaster, radio_lock=self.radio_lock
        )
        self.connections.request_scan_restart = self.scanner.request_restart
        self.connections.scan_session_age = self.scanner.session_age
        self.connections.scan_ready_for_connects = self.scanner.ready_for_connects

        self.scanner.set_device_found_callback(self.handle_device_found)

    def handle_device_found(self, device: BLEDevice, rssi: Optional[int]) -> None:
        """
        Callback for every matching advertisement seen while scanning.

        :param device: The discovered BLEDevice.
        :param rssi: The advertisement's RSSI.
        """
        self.connections.on_advertisement(device, rssi)

    async def initialize(self) -> None:
        """
        Forcibly disconnect all devices at the D-Bus level for a clean
        environment, then start the connection supervisor.
        """
        try:
            await asyncio.wait_for(
                disconnect_all_devices(), STARTUP_DISCONNECT_TIMEOUT_SECONDS
            )
        except Exception as e:
            logger.warning(f"Could not clear stale BlueZ connections at startup: {e}")
        self.connections.start()

    async def shutdown(self) -> None:
        """Stop reconnecting, disconnect everything and stop scanning."""
        await self.connections.stop()
        await self.connections.disconnect_all()
        await self.scanner.stop_scan()

    async def start_scan(self, service_uuids: List[str]) -> Mapping[str, Any]:
        """
        Public method to initiate BLE scanning with optional service UUID filters.

        :param service_uuids: A list of service UUIDs to filter by, or empty for none.
        :return: Status about the scan.
        """
        return await self.scanner.start_scan(service_uuids)

    async def stop_scan(self) -> Dict[str, str]:
        """
        Public method to stop BLE scanning.

        :return: Status about stopping the scan.
        """
        return await self.scanner.stop_scan()

    async def add_device(self, mac: str) -> None:
        """
        Add a device to the desired connections. The supervisor connects to it
        as soon as BlueZ knows it.

        :param mac: The MAC address of the BLE device to connect to.
        """
        mac = mac.upper()
        self.connections.desire(mac)

        device = await self.scanner.get_device(mac)
        if device:
            self.connections.on_advertisement(device, None)
        else:
            logger.info(
                f"Device {mac} added to desired connections, will connect when discovered."
            )

    async def disconnect_device(self, mac: str) -> None:
        """
        Disconnect from a single device by MAC.

        :param mac: The target device's MAC address.
        """
        await self.connections.disconnect_device(mac)

    async def disconnect_all(self) -> None:
        """
        Disconnect all known devices from the connection manager.
        """
        await self.connections.disconnect_all()

    async def list_characteristics(self, mac: str) -> List[Dict[str, Any]]:
        """
        List all characteristics for a connected device.

        :param mac: Device MAC address.
        :return: A list of characteristic dict objects (UUID, properties, etc.).
        :raises HTTPException: If device not connected
        """
        return await self.connections.list_characteristics(mac)

    async def read_characteristic(self, mac: str, char_uuid: str) -> str:
        """
        Read a characteristic from a connected device, returning its value in hex.

        :param mac: Device MAC address.
        :param char_uuid: Characteristic UUID to read from.
        :return: Hex string of the read value.
        :raises HTTPException: If device not connected or read fails.
        """
        return await self.connections.read_characteristic(mac, char_uuid)

    async def write_characteristic(
        self, mac: str, char_uuid: str, value: bytes, resp: bool = False
    ) -> None:
        """
        Write a value to a characteristic on a connected device.

        :param mac: Device MAC address.
        :param char_uuid: Characteristic UUID to write to.
        :param value: The bytes value to write to the characteristic.
        :param resp: Whether to wait for a response (default is False).
        :raises HTTPException: If device not connected or write fails.
        """
        await self.connections.write_characteristic(mac, char_uuid, value, resp)

    async def get_mtu(self, mac: str) -> int:
        """
        Get the Maximum Transmission Unit (MTU) size for a connected device.

        :param mac: Device MAC address.
        :return: The MTU size as an integer.
        :raises HTTPException: If device not connected or unable to retrieve MTU.
        """
        return await self.connections.get_mtu(mac)

    async def enable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Enable notifications for a particular characteristic on a connected device.

        :param mac: Device MAC address.
        :param char_uuid: Characteristic UUID to enable notifications for.
        """
        await self.connections.enable_notification(mac, char_uuid)

    async def disable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Disable notifications for a particular characteristic on a connected device.

        :param mac: Device MAC address.
        :param char_uuid: Characteristic UUID to disable notifications for.
        """
        await self.connections.disable_notification(mac, char_uuid)

    async def disable_all_notifications(self) -> None:
        """
        Disable notifications for all characteristics on all connected devices.
        """
        await self.connections.disable_all_notifications()

    async def get_connected_devices(self) -> Mapping[str, Any]:
        """
        Return a dictionary of connected devices (MAC: name).

        :return: A dictionary of connected devices.
        """
        return await self.connections.get_connected_devices()

    async def health(self) -> Dict[str, Any]:
        """
        Adapter, scanner, SSE and per-sensor state in one document.
        """
        state = None
        with contextlib.suppress(Exception):
            state = await asyncio.wait_for(adapter_state(), HEALTH_ADAPTER_TIMEOUT_SECONDS)
        return {
            "adapter": None if state is None else {"powered": state[0], "discovering": state[1]},
            "scanner": self.scanner.health(),
            "sse": {
                "clients": self.event_broadcaster.client_count,
                "dropped_events": self.event_broadcaster.dropped_events,
            },
            **self.connections.health(),
        }
