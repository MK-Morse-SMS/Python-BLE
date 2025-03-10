from fastapi import APIRouter, Body, Path, Query, Depends
from fastapi.responses import StreamingResponse, JSONResponse
import json

from models import BatchConnectRequest, StartScanRequest
from ble_manager import BLEManager

# Create router instance
router = APIRouter()

# Dependency to get BLE manager instance
def get_ble_manager():
    # This imports here to avoid circular import issues
    from main import ble_manager
    return ble_manager

# --- SSE Event Generator ---
async def event_generator(ble_manager: BLEManager):
    while True:
        event = await ble_manager.event_queue.get()
        yield f"data: {json.dumps(event)}\n\n"

# --- API Endpoints ---

# 1. List connected devices (filter by connection_state query parameter)
@router.get("/gap/nodes")
async def list_nodes(
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    nodes = []
    for mac, client in ble_manager.connected_devices.items():
        nodes.append({
            "bdaddr": mac,
            "name": ble_manager.device_names.get(mac, ""),
            "connectionState": "connected"
        })
    return {"nodes": nodes}

# 2. Add devices to connection list and start automatic connection management.
@router.post("/gap/batch-connect")
async def batch_connect(
    request: BatchConnectRequest, 
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    for mac in request.mac_addresses:
        ble_manager.add_device(mac)
    return JSONResponse(content={"status": "devices added to connection list", "devices": request.mac_addresses})

# 3. List Characteristics on a connected device.
@router.get("/gatt/nodes/{mac}/characteristics")
async def get_characteristics(
    mac: str = Path(...), 
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    characteristics = await ble_manager.list_characteristics(mac)
    return {"characteristics": characteristics}

# 4. Enable notifications on a characteristic.
@router.post("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def enable_char_notification(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.enable_notification(mac, char_uuid)
    return JSONResponse(content={"status": "notification enabled", "bdaddr": mac, "characteristic": char_uuid})

# 5. Disable notifications on a characteristic.
@router.delete("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def disable_char_notification(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.disable_notification(mac, char_uuid)
    return JSONResponse(content={"status": "notification disabled", "bdaddr": mac, "characteristic": char_uuid})

# 6. Disable notifications on all characteristics.
@router.delete("/gatt/nodes/notifications")
async def disable_all_notifications(
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.disable_all_notifications()
    return JSONResponse(content={"status": "all notifications disabled"})

# 7. Disconnect from a device.
@router.delete("/gap/nodes/{mac}")
async def disconnect_device(
    mac: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.disconnect_device(mac)
    return JSONResponse(content={"status": "device disconnected", "bdaddr": mac})

# 8. Start scanning with filters (service UUIDs provided in the JSON body).
@router.post("/gap/start-scan")
async def start_scan(
    request: StartScanRequest = Body(None),
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    service_uuids = request.service_uuids if request and request.service_uuids else []
    result = await ble_manager.start_scan(service_uuids)
    return JSONResponse(content=result)

# 9. Stop scanning.
@router.delete("/gap/stop-scan")
async def stop_scan(
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.stop_scan()
    return JSONResponse(content={"status": "scan stopped"})

# 9. SSE endpoint to stream scan results, connection events, and notifications.
@router.get("/events")
async def sse_events(
    ble_manager: BLEManager = Depends(get_ble_manager)
):
    return StreamingResponse(event_generator(ble_manager), media_type="text/event-stream")