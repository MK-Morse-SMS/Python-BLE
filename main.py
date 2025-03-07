import asyncio
import json
from typing import List, Dict, Any
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException, Body, Path, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from bleak import BleakClient, BleakScanner


# --- Data Models ---

class BatchConnectRequest(BaseModel):
    list: List[str]


class StartScanRequest(BaseModel):
    service_uuids: List[str]


# --- BLE Manager Implementation --

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
        # An event queue for SSE events.
        self.event_queue: asyncio.Queue = asyncio.Queue()
    
    async def detection_callback(self, device, advertisement_data):
        if (not self.scan_filters or any(uuid in advertisement_data.service_uuids for uuid in self.scan_filters)) and device.name is not None and device.name != "":
            event = {
                "type": "scan",
                "results": [{"bdaddr": device.address, "name": device.name}]
            }
            await self.event_queue.put(event)

    async def scan_loop(self):
        while True:
            try:
                scanner = BleakScanner(
                    detection_callback=self.detection_callback,
                    service_uuids=self.scan_filters,
                    scanning_mode="active"
                )
                await scanner.start()
                await asyncio.sleep(300)
                await scanner.stop()
            except Exception as e:
                pass
            await asyncio.sleep(1)

    async def connection_loop(self, mac: str):
        """Continuously try to connect/reconnect to a device."""
        logger = logging.getLogger(__name__)
        
        logger.info(f"Starting connection loop for device: {mac}")
        while mac in self.connection_list:
            try:
                logger.debug(f"Attempting to connect to {mac}")
                client = BleakClient(mac)
                await client.connect()
                self.connected_devices[mac] = client

                logger.info(f"Successfully connected to {mac}")
                
                # Report connection event via SSE.
                await self.event_queue.put({
                    "type": "connection",
                    "bdaddr": mac,
                    "status": "connected"
                })

                # Stay connected until the client disconnects.
                while client.is_connected:
                    await asyncio.sleep(1)

                # Connection dropped.
                logger.warning(f"Connection to {mac} dropped")
                await self.event_queue.put({
                    "type": "connection",
                    "bdaddr": mac,
                    "status": "disconnected"
                })
                self.connected_devices.pop(mac, None)

            except Exception as e:
                logger.error(f"Error connecting to {mac}: {str(e)}")
                await asyncio.sleep(5)  # Back-off before retrying.

            # A brief pause before attempting reconnection.
            logger.debug(f"Waiting before reconnection attempt to {mac}")
            await asyncio.sleep(1)

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
            await self.event_queue.put({
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

    async def enable_notification(self, mac: str, char_uuid: str):
        if mac not in self.connected_devices:
            raise HTTPException(status_code=404, detail="Device not connected")
        client = self.connected_devices[mac]

        # Notification callback: push event to SSE queue.
        def notification_handler(sender, data):
            asyncio.create_task(self.event_queue.put({
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

# Create a single instance of BLEManager.
ble_manager = BLEManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await ble_manager.disconnect_all()


app = FastAPI(lifespan=lifespan)


# --- SSE Event Generator ---
async def event_generator():
    while True:
        event = await ble_manager.event_queue.get()
        yield f"data: {json.dumps(event)}\n\n"


# --- API Endpoints ---

# 1. List connected devices (filter by connection_state query parameter)
@app.get("/gap/nodes")
async def list_nodes(connection_state: str = Query(None)):
    if connection_state and connection_state.lower() == "connected":
        nodes = []
        for mac, client in ble_manager.connected_devices.items():
            nodes.append({
                "bdaddr": mac,
                "name": client.details.get("name", "Unknown") if hasattr(client, "details") else "Unknown",
                "connectionState": "connected"
            })
        return {"nodes": nodes}
    else:
        return {"nodes": []}


# 2. Add devices to connection list and start automatic connection management.
@app.post("/gap/batch-connect")
async def batch_connect(request: BatchConnectRequest):
    for mac in request.list:
        ble_manager.add_device(mac)
    return JSONResponse(content={"status": "devices added to connection list", "devices": request.list})


# 3. List Characteristics on a connected device.
@app.get("/gatt/nodes/{mac}/characteristics")
async def get_characteristics(mac: str = Path(...)):
    characteristics = await ble_manager.list_characteristics(mac)
    return {"characteristics": characteristics}


# 4. Enable notifications on a characteristic.
@app.post("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def enable_char_notification(
    mac: str = Path(...), char_uuid: str = Path(...)
):
    await ble_manager.enable_notification(mac, char_uuid)
    return JSONResponse(content={"status": "notification enabled", "bdaddr": mac, "characteristic": char_uuid})


# 5. Disable notifications on a characteristic.
@app.delete("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def disable_char_notification(
    mac: str = Path(...), char_uuid: str = Path(...)
):
    await ble_manager.disable_notification(mac, char_uuid)
    return JSONResponse(content={"status": "notification disabled", "bdaddr": mac, "characteristic": char_uuid})


# 6. Disable notifications on all characteristics.
@app.delete("/gatt/nodes/notifications")
async def disable_all_notifications():
    await ble_manager.disable_all_notifications()
    return JSONResponse(content={"status": "all notifications disabled"})


# 7. Disconnect from a device.
@app.delete("/gatt/nodes/{mac}")
async def disconnect_device(mac: str = Path(...)):
    await ble_manager.disconnect_device(mac)
    return JSONResponse(content={"status": "device disconnected", "bdaddr": mac})


# 8. Start scanning with filters (service UUIDs provided in the JSON body).
@app.post("/gap/start-scan")
async def start_scan(request: StartScanRequest = Body(None)):
    service_uuids = request.service_uuids if request and request.service_uuids else []
    result = await ble_manager.start_scan(service_uuids)
    return JSONResponse(content=result)


# 9. SSE endpoint to stream scan results, connection events, and notifications.
@app.get("/events")
async def sse_events():
    return StreamingResponse(event_generator(), media_type="text/event-stream")



# To run the service, use:
# uvicorn main:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
