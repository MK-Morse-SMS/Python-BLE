import asyncio
import logging
from typing import List, Dict, Optional, Callable

from bleak import BleakScanner, BLEDevice, AdvertisementData
from event_broadcaster import EventBroadcaster

logger = logging.getLogger(__name__)


class BLEScanner:
    """
    Handles BLE scanning logic:
      - Start/stop scanning
      - Maintain a list of discovered devices
      - Broadcast scan events
      - Notify when desired devices are discovered
    """

    def __init__(self, event_broadcaster: EventBroadcaster) -> None:
        """
        :param event_broadcaster: The event broadcaster instance for SSE or other event dissemination.
        """
        self.event_broadcaster = event_broadcaster

        self.scan_filters: List[str] = []
        self.devices: Dict[str, BLEDevice] = {}
        self.device_names: Dict[str, str] = {}

        self.active_scanner: Optional[BleakScanner] = None
        self.scan_task: Optional[asyncio.Task] = None

        # Default scanning cycle duration (in seconds)
        self.scan_cycle: int = 300  # e.g., 5 minutes

        # Callback for when devices are detected
        self.device_found_callback: Optional[Callable[[BLEDevice], None]] = None

    async def start_scan(self, service_uuids: List[str]) -> Dict[str, str]:
        """
        Begin scanning for BLE devices, optionally filtering by service UUIDs.

        :param service_uuids: A list of UUIDs to filter on. If empty, scans for all devices.
        :return: Status information about the scan.
        """
        logger.info("Starting BLE scan.")
        self.scan_filters = service_uuids

        # Start the scan loop if not already running or if the task is done.
        if self.scan_task is None or self.scan_task.done():
            self.scan_task = asyncio.create_task(self._scan_loop())

        return {"status": "scanning started", "filters": self.scan_filters}

    async def stop_scan(self) -> Dict[str, str]:
        """
        Stop any active scanning loop and clear filters.

        :return: Status information about the scan stop.
        """
        logger.info("Stopping BLE scan.")
        self.scan_filters = []

        # First stop the active scanner
        if self.active_scanner:
            try:
                await self.active_scanner.stop()
            except Exception as e:
                logger.error(f"Error stopping scanner: {str(e)}")
            self.active_scanner = None

        # Cancel the scan task
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()
            try:
                await asyncio.wait_for(self.scan_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self.scan_task = None

        return {"status": "scanning stopped"}

    def set_device_found_callback(self, callback: Callable[[BLEDevice], None]) -> None:
        """
        Set a callback to be called when a device is found during scanning.

        :param callback: A function that takes a BLEDevice and handles it
        """
        self.device_found_callback = callback

    async def detection_callback(
        self, device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """
        The callback invoked by BleakScanner upon discovering (or seeing again) a device.

        :param device: The discovered BLEDevice.
        :param advertisement_data: Info about the advertisement packet.
        """
        # If service_uuids is not empty, we only push devices that match.
        logger.debug(
            f"Device found: {device.name} ({device.address}) with RSSI {advertisement_data.rssi}"
        )
        if device.name and (
            not self.scan_filters
            or any(
                uuid in advertisement_data.service_uuids for uuid in self.scan_filters
            )
        ):
            # Track the device if not already present
            if device.address not in self.devices:
                self.devices[device.address] = device
                self.device_names[device.address] = device.name

            # Broadcast scan event
            event = {
                "type": "scan",
                "results": [
                    {
                        "bdaddr": device.address,
                        "name": device.name,
                        "rssi": advertisement_data.rssi,
                    }
                ],
            }
            await self.event_broadcaster.broadcast(event)

            # Notify the callback if set
            if self.device_found_callback:
                self.device_found_callback(device)

    async def _scan_loop(self) -> None:
        """
        Periodically starts scanning, waits for a configured cycle time,
        stops scanning, and repeats until cancelled.
        """
        while True:
            try:
                # Create a new scanner
                self.active_scanner = BleakScanner(
                    detection_callback=self.detection_callback,
                    service_uuids=self.scan_filters,
                    scanning_mode="active",
                )

                # Start scanning
                await self.active_scanner.start()
                logger.debug("Scanner started.")

                try:
                    # Wait for scan_cycle seconds or until cancelled
                    await asyncio.sleep(self.scan_cycle)
                except asyncio.CancelledError:
                    logger.info("Scan loop cancelled while scanning")
                    raise

                # Stop scanning
                await self.active_scanner.stop()
                self.active_scanner = None

                # Optionally clear the devices map after each scan cycle.
                self.devices.clear()
                self.device_names.clear()

            except asyncio.CancelledError:
                logger.info("Scan loop cancelled.")
                if self.active_scanner:
                    await self.active_scanner.stop()
                    self.active_scanner = None
                raise
            except Exception as e:
                logger.error(f"Error in scan loop: {e}", exc_info=True)
                if self.active_scanner:
                    try:
                        await self.active_scanner.stop()
                    except Exception:
                        pass
                    self.active_scanner = None

            # Wait briefly before next scan cycle
            await asyncio.sleep(1)

    async def get_device(self, mac: str) -> Optional[BLEDevice]:
        """
        Retrieve a discovered device by its MAC address.

        :param mac: The target device's MAC address.
        :return: BLEDevice if found, otherwise None.
        """
        device = self.devices.get(mac)
        # If the device is not found, try to specifically find it
        if not device and self.active_scanner:
            logger.info(f"Device {mac} found: {device is not None}")
        return device
