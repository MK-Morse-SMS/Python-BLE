from fastapi import APIRouter, Body, Path, Depends, Request
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
async def event_generator(ble_manager: BLEManager, request: Request):
    client_id = ble_manager.event_broadcaster.register_client()
    try:
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            event = await ble_manager.event_broadcaster.get_client_event(client_id)
            yield f"data: {json.dumps(event)}\n\n"
    finally:
        # Always unregister the client when the connection ends
        ble_manager.event_broadcaster.unregister_client(client_id)


# --- API Endpoints ---


# 1. List connected devices (filter by connection_state query parameter)
@router.get("/gap/nodes")
async def list_nodes(ble_manager: BLEManager = Depends(get_ble_manager)):
    nodes = []
    connected_devices = await ble_manager.get_connected_devices()
    for mac, client in connected_devices.items():
        nodes.append({"bdaddr": mac, "name": client, "connectionState": "connected"})
    return {"nodes": nodes}


# 2. Add devices to connection list and start automatic connection management.
@router.post("/gap/batch-connect")
async def batch_connect(
    request: BatchConnectRequest, ble_manager: BLEManager = Depends(get_ble_manager)
):
    for mac in request.mac_addresses:
        await ble_manager.add_device(mac)
    return JSONResponse(
        content={
            "status": "devices added to connection list",
            "devices": request.mac_addresses,
        }
    )


# 3. List Characteristics on a connected device.
@router.get("/gatt/nodes/{mac}/characteristics")
async def get_characteristics(
    mac: str = Path(...), ble_manager: BLEManager = Depends(get_ble_manager)
):
    characteristics = await ble_manager.list_characteristics(mac)
    return {"characteristics": characteristics}


# Read a characteristic value.
@router.get("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def read_characteristic(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager),
):
    value = await ble_manager.read_characteristic(mac, char_uuid)
    return JSONResponse(content={"value": value})


# Write a characteristic value.
@router.get("/gatt/nodes/{mac}/characteristic/{char_uuid}/value/{value}")
async def write_characteristic(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    value: str = Path(...),  # Expecting a hex string
    noresponse: bool = False,  # Optional parameter to indicate no response needed
    ble_manager: BLEManager = Depends(get_ble_manager),
):
    """
    Write a value to a characteristic on a connected device.

    :param mac: Device MAC address.
    :param char_uuid: Characteristic UUID to write to.
    :param value: Hex string value to write to the characteristic.
    :param noresponse: If True, do not wait for a response from the device (default is False).
    :return: JSON response indicating success or failure.
    """
    # Convert hex string to bytes
    try:
        byte_value = bytes.fromhex(value)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid hex string"})

    await ble_manager.write_characteristic(
        mac, char_uuid, byte_value, resp=not noresponse
    )
    return JSONResponse(
        content={
            "status": "write successful",
            "bdaddr": mac,
            "characteristic": char_uuid,
        }
    )


# Get MTU (Maximum Transmission Unit) for a connected device.
@router.get("/gatt/nodes/{mac}/mtu")
async def get_mtu(
    mac: str = Path(...), ble_manager: BLEManager = Depends(get_ble_manager)
):
    """
    Get the Maximum Transmission Unit (MTU) size for a connected device.

    :param mac: Device MAC address.
    :return: JSON response with the MTU size.
    """
    mtu_size = await ble_manager.get_mtu(mac)
    return JSONResponse(content={"mtu": mtu_size, "bdaddr": mac})


# 4. Enable notifications on a characteristic.
@router.post("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def enable_char_notification(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager),
):
    await ble_manager.enable_notification(mac, char_uuid)
    return JSONResponse(
        content={
            "status": "notification enabled",
            "bdaddr": mac,
            "characteristic": char_uuid,
        }
    )


# 5. Disable notifications on a characteristic.
@router.delete("/gatt/nodes/{mac}/characteristic/{char_uuid}")
async def disable_char_notification(
    mac: str = Path(...),
    char_uuid: str = Path(...),
    ble_manager: BLEManager = Depends(get_ble_manager),
):
    await ble_manager.disable_notification(mac, char_uuid)
    return JSONResponse(
        content={
            "status": "notification disabled",
            "bdaddr": mac,
            "characteristic": char_uuid,
        }
    )


# 6. Disable notifications on all characteristics.
@router.delete("/gatt/nodes/notifications")
async def disable_all_notifications(ble_manager: BLEManager = Depends(get_ble_manager)):
    await ble_manager.disable_all_notifications()
    return JSONResponse(content={"status": "all notifications disabled"})


# 7. Disconnect from a device.
@router.delete("/gap/nodes/{mac}")
async def disconnect_device(
    mac: str = Path(...), ble_manager: BLEManager = Depends(get_ble_manager)
):
    await ble_manager.disconnect_device(mac)
    return JSONResponse(content={"status": "device disconnected", "bdaddr": mac})


# 8. Start scanning with filters (service UUIDs provided in the JSON body).
@router.post("/gap/start-scan")
async def start_scan(
    request: StartScanRequest = Body(None),
    ble_manager: BLEManager = Depends(get_ble_manager),
):
    service_uuids = request.service_uuids if request and request.service_uuids else []
    result = await ble_manager.start_scan(service_uuids)
    return JSONResponse(content=result)


# 9. Stop scanning.
@router.delete("/gap/stop-scan")
async def stop_scan(ble_manager: BLEManager = Depends(get_ble_manager)):
    await ble_manager.stop_scan()
    return JSONResponse(content={"status": "scan stopped"})


# 9. SSE endpoint to stream scan results, connection events, and notifications.
@router.get("/events")
async def sse_events(
    request: Request, ble_manager: BLEManager = Depends(get_ble_manager)
):
    return StreamingResponse(
        event_generator(ble_manager, request), media_type="text/event-stream"
    )


