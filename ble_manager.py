import asyncio
import contextlib
from typing import List, Dict, Any
import logging
from bleak import BleakClient, BleakScanner, BLEDevice, AdvertisementData
from fastapi import HTTPException
from event_broadcaster import EventBroadcaster

logger = logging.getLogger(__name__)

class BLEManager:
    def __init__(self):
        # The set of MAC addresses that we want to be connected to.
        self.connection_list = set()  # type: set[str]
        # Currently connected devices: MAC address -> BleakClient
        self.connected_devices: Dict[str, BleakClient] = {}
        # Tasks managing device connection loops.
        self.connection_tasks: Dict[str, asyncio.Task] = {}
        # Background scanning task.
        self.scan_task: asyncio.Task = None
        # The current list of service UUID filters for scanning.
        self.scan_filters: List[str] = []
        # Replace the queue with a broadcaster
        self.event_broadcaster = EventBroadcaster()
        # Mac address -> BLEDevice mapping.
        self.devices: Dict[str, BLEDevice] = {}
        # Mac address -> device name mapping.
        self.device_names: Dict[str, str] = {}
        # Lock for managing connection attempts
        self.connection_lock = asyncio.Lock()
        # Keep a reference to the active scanner
        self.active_scanner = None
    
    async def detection_callback(self, device: BLEDevice, advertisement_data: AdvertisementData):
        if device.name and (not self.scan_filters or any(uuid in advertisement_data.service_uuids for uuid in self.scan_filters)):
            # Only add the device to the list if it's not already there
            if device.address not in self.devices:
                self.devices[device.address] = device
                self.device_names[device.address] = device.name
            
            # Push the scan event to the event queue.
            event = {
                "type": "scan",
                "results": [{"bdaddr": device.address, "name": device.name, "rssi": advertisement_data.rssi}]
            }
            await self.event_broadcaster.broadcast(event)

    async def scan_loop(self):
        while True:
            try:
                # Create a new scanner instance
                self.active_scanner = BleakScanner(
                    detection_callback=self.detection_callback,
                    service_uuids=self.scan_filters,
                    scanning_mode="active"
                )
                
                # Start scanning
                await self.active_scanner.start()
                
                # Wait for scan period or until cancelled
                try:
                    await asyncio.sleep(300)  # 5 minute scan cycle
                except asyncio.CancelledError:
                    logger.info("Scan loop cancelled while scanning")
                    raise  # Re-raise to exit the task
                
                # Stop the scanner
                await self.active_scanner.stop()
                self.active_scanner = None
                
                # Clear the devices map after each scan cycle
                self.devices.clear()
                
            except asyncio.CancelledError:
                logger.info("Scan loop cancelled")
                # Make sure to stop the scanner if it's active
                if self.active_scanner:
                    await self.active_scanner.stop()
                    self.active_scanner = None
                raise  # Re-raise to exit the task
                
            except Exception as e:
                logger.error(f"Error in scan loop: {str(e)}", exc_info=True)
                if self.active_scanner:
                    try:
                        await self.active_scanner.stop()
                    except Exception:
                        pass
                    self.active_scanner = None
                
            await asyncio.sleep(1)

    async def connection_loop(self, mac: str):
        """Continuously try to connect/reconnect to a device."""
        # check if the device is still in the connection list
        if mac not in self.connection_list:
            return
        
        # check if the device is already connected
        if mac in self.connected_devices:
            return
        
        logger.info(f"Starting connection loop for device: {mac}")
        
        # Disconnection event to signal when the callback has processed the disconnection
        disconnect_event = asyncio.Event()
        
        # Disconnection callback function
        def disconnection_handler(client):
            logger.warning(f"Connection to {mac} dropped")
            # Mark the device as disconnected in our data structures
            self.connected_devices.pop(mac, None)
            # Create a task to send the disconnection event via SSE
            # (can't use await directly in a callback)
            asyncio.create_task(self._handle_disconnection(mac))
            # Signal the connection loop that disconnection has been processed
            disconnect_event.set()
        
        while mac in self.connection_list:
            try:
                # Reset the disconnection event
                disconnect_event.clear()
                
                # Use an AsyncExitStack for proper resource management
                async with contextlib.AsyncExitStack() as stack:
                    # Acquire lock before scanning and connecting
                    async with self.connection_lock:
                        logger.debug(f"Attempting to connect to {mac}")
                        
                        # If the device is in the devices map, use it directly.
                        device = self.devices.get(mac)
                        if device is None:
                            # If the device is not found in the devices map, scan for it.
                            logger.debug(f"Scanning for device {mac}")
                            device = await BleakScanner.find_device_by_address(mac)
                            if device is None:
                                logger.warning(f"Device {mac} not found during scan")
                                await asyncio.sleep(5)  # Back-off before retrying
                                continue
                        
                        # Create the client with the disconnection callback
                        client = BleakClient(device, disconnected_callback=disconnection_handler)
                        await client.connect()
                        self.connected_devices[mac] = client
                        
                        # Register cleanup callback
                        stack.callback(logger.debug, f"Releasing connection to {mac}")
                        
                        logger.info(f"Successfully connected to {mac}")
                        
                        # Report connection event via SSE.
                        await self.event_broadcaster.broadcast({
                            "type": "connection",
                            "bdaddr": mac,
                            "status": "connected"
                        })
                    
                    # Lock is released here but client remains connected
                    
                    # Wait for the disconnection event instead of polling
                    await disconnect_event.wait()
                    
                    # Connection has been dropped and handled by the callback
                    # We just exit the context manager now to clean up

            except Exception as e:
                logger.error(f"Error connecting to {mac}: {str(e)}")
                # If the device was added to connected_devices before the exception,
                # make sure we remove it
                self.connected_devices.pop(mac, None)
                await asyncio.sleep(5)  # Back-off before retrying

            # A brief pause before attempting reconnection
            logger.debug(f"Waiting before reconnection attempt to {mac}")
            await asyncio.sleep(1)

    # Helper method to handle disconnection (called by the callback)
    async def _handle_disconnection(self, mac: str):
        """Send disconnection event to the event queue."""
        await self.event_broadcaster.broadcast({
            "type": "connection",
            "bdaddr": mac,
            "status": "disconnected"
        })

    def add_device(self, mac: str):
        if mac not in self.connection_list:
            self.connection_list.add(mac)
            # Start a background task to maintain connection.
            self.connection_tasks[mac] = asyncio.create_task(self.connection_loop(mac))

    async def disconnect_device(self, mac: str):
        if mac in self.connection_list:
            self.connection_list.remove(mac)
        # Cancel the connection task if it exists.
        if mac in self.connection_tasks:
            task = self.connection_tasks.pop(mac)
            task.cancel()
        # If the device is connected, disconnect it.
        if mac in self.connected_devices:
            client = self.connected_devices.pop(mac)
            try:
                await client.disconnect()
            except Exception:
                pass
            await self.event_broadcaster.broadcast({
                "type": "connection",
                "bdaddr": mac,
                "status": "disconnected"
            })

    async def list_characteristics(self, mac: str) -> List[Dict[str, Any]]:
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        client = self.connected_devices[mac]
        services = await client.get_services()
        characteristics = []
        for service in services:
            for char in service.characteristics:
                characteristics.append({
                    "uuid": char.uuid,
                    "properties": char.properties,
                    "description": getattr(char, "description", "")
                })
        return characteristics
    
    async def read_characteristic(self, mac: str, char_uuid: str) -> str:
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        client = self.connected_devices[mac]
        try:
            value = await client.read_gatt_char(char_uuid)
            return value.hex()
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def enable_notification(self, mac: str, char_uuid: str):
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        client = self.connected_devices[mac]

        # Notification callback: push event to SSE queue.
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

    async def disable_notification(self, mac: str, char_uuid: str):
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        client = self.connected_devices[mac]
        try:
            await client.stop_notify(char_uuid)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def disable_all_notifications(self):
        for mac, client in self.connected_devices.items():
            services = await client.get_services()
            for service in services:
                for char in service.characteristics:
                    try:
                        await client.stop_notify(char.uuid)
                    except Exception:
                        pass

    async def start_scan(self, service_uuids: List[str]):
        self.scan_filters = service_uuids
        # Start the scan loop if not already running.
        if self.scan_task is None or self.scan_task.done():
            self.scan_task = asyncio.create_task(self.scan_loop())
        return {"status": "scanning started", "filters": self.scan_filters}

    async def disconnect_all(self):
        disconnect_tasks = [self.disconnect_device(mac) for mac in list(self.connection_list)]
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks)

    async def stop_scan(self):
        """Stop any active scanning."""
        self.scan_filters = []
        
        # First stop the active scanner if it exists
        if self.active_scanner:
            try:
                logger.info("Stopping active scanner")
                await self.active_scanner.stop()
                self.active_scanner = None
            except Exception as e:
                logger.error(f"Error stopping scanner: {str(e)}")
        
        # Then cancel the scan task
        if self.scan_task and not self.scan_task.done():
            logger.info("Cancelling scan task")
            self.scan_task.cancel()
            try:
                # Wait for task cancellation to complete
                await asyncio.wait_for(self.scan_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self.scan_task = None
            
        return {"status": "scanning stopped"}