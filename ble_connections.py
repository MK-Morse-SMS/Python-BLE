import asyncio
import logging
from typing import Dict, Set

from bleak import BleakClient, BleakScanner, BLEDevice
from event_broadcaster import EventBroadcaster
from fastapi import HTTPException

logger = logging.getLogger(__name__)


class BLEConnectionsManager:
    """
    Manages and maintains active BLE connections. It handles:
      - Desired connection set
      - Connection loops for each device
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

        # Tasks that manage the connection loops, keyed by MAC
        self.connection_tasks: Dict[str, asyncio.Task] = {}

        # Lock to prevent race conditions when scanning and connecting
        self.connection_lock = asyncio.Lock()

        # Store device names separately from the BleakClient objects
        self.device_names: Dict[str, str] = {}

    def add_device(self, device: BLEDevice) -> None:
        """
        Add a device to desired connections and start a connection task.

        :param device: BLEDevice instance. We'll use device.address as the key.
        """
        mac = device.address
        if mac not in self.desired_connections:
            self.desired_connections.add(mac)
            # Start background task for connection management
            self.connection_tasks[mac] = asyncio.create_task(
                self._connection_loop(device)
            )

    async def disconnect_device(self, mac: str) -> None:
        """
        Disconnect from a specified device and remove it from desired connections.

        :param mac: The MAC address of the target device.
        """
        if mac in self.desired_connections:
            self.desired_connections.remove(mac)

        # Cancel the connection loop if it exists
        if mac in self.connection_tasks:
            task = self.connection_tasks.pop(mac)
            task.cancel()

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
                characteristics.append({
                    "uuid": char.uuid,
                    "properties": char.properties,
                    "description": getattr(char, "description", "")
                })
        return characteristics

    async def enable_notification(self, mac: str, char_uuid: str) -> None:
        """
        Enable notifications on a specific characteristic of a connected device.

        :param mac: MAC address of the device
        :param char_uuid: UUID of the characteristic
        :raises HTTPException: If device not connected
        """
        client = self._get_connected_client(mac)

        def notification_handler(sender, data):
            asyncio.create_task(self.event_broadcaster.broadcast({
                "type": "notification",
                "bdaddr": mac,
                "characteristic": char_uuid,
                "data": data.hex()
            }))

        try:
            await client.start_notify(char_uuid, notification_handler)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

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

    async def _connection_loop(self, device: BLEDevice) -> None:
        """
        Continuously try to connect/reconnect to the given device.
        """
        mac = device.address
        logger.info(f"Starting connection loop for {mac}.")

        # This event is used to wait for disconnection callbacks
        disconnect_event = asyncio.Event()

        def _disconnection_handler(_client):
            logger.warning(f"Connection to {mac} dropped.")
            self.connected_devices.pop(mac, None)
            # Broadcast disconnection asynchronously
            asyncio.create_task(self._broadcast_disconnection(mac))
            disconnect_event.set()

        while mac in self.desired_connections:
            try:
                disconnect_event.clear()
                async with self.connection_lock:
                    # Attempt to connect
                    client = BleakClient(device, disconnected_callback=_disconnection_handler)
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
                        device_name_bytes = await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")
                        if device_name_bytes:
                            try:
                                # Convert bytes to string and store it
                                device_name = device_name_bytes.decode('utf-8')
                                self.device_names[mac] = device_name
                                logger.info(f"Read device name for {mac}: {device_name}")
                            except UnicodeDecodeError:
                                logger.warning(f"Could not decode device name for {mac}")
                    except Exception as e:
                        logger.debug(f"Could not read device name characteristic for {mac}: {e}")
                        # Use advertised name or address as fallback if we couldn't read the name
                        if mac not in self.device_names:
                            self.device_names[mac] = device.name or mac
                    
                    logger.info(f"Successfully connected to {mac}.")
                    await self._broadcast_connection(mac, "connected")

                # Wait for disconnection
                await disconnect_event.wait()

                # Connection was dropped, loop repeats to attempt reconnect
            except asyncio.CancelledError:
                logger.info(f"Connection loop cancelled for {mac}.")
                # Clean up if needed
                self.connected_devices.pop(mac, None)
                break
            except Exception as e:
                logger.error(f"Error connecting to {mac}: {e}", exc_info=True)
                self.connected_devices.pop(mac, None)
                # Brief pause before retry
                await asyncio.sleep(5)

            await asyncio.sleep(1)

        logger.debug(f"Exiting connection loop for {mac}.")

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
        await self.event_broadcaster.broadcast({
            "type": "connection",
            "bdaddr": mac,
            "status": status
        })

    async def _broadcast_disconnection(self, mac: str) -> None:
        """
        Helper to broadcast a disconnection event.
        """
        await self._broadcast_connection(mac, "disconnected")

    async def get_connected_devices(self) -> Dict[str, str]:
        """
        Return a dictionary of connected devices (MAC: name).
        """
        return {mac: name for mac, name in self.device_names.items()}
