import asyncio
import logging
from typing import Optional

from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType

logger = logging.getLogger(__name__)

# D-Bus service and interface constants
BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
DEVICE_INTERFACE = "org.bluez.Device1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"


async def disconnect_all_devices() -> None:
    """
    Disconnect all currently connected BLE devices at the D-Bus (BlueZ) level.

    This utility can be used at startup or shutdown to ensure a clean slate.
    """
    # Connect to the system bus (BlueZ runs on the system bus)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Get a proxy for the root object to access the ObjectManager interface
    introspection = await bus.introspect(BLUEZ_SERVICE, "/")
    obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
    manager = obj.get_interface(OBJECT_MANAGER_INTERFACE)

    # Get all managed objects (devices, adapters, etc.)
    managed_objects = await manager.call_get_managed_objects()

    for path, interfaces in managed_objects.items():
        # Check if this object implements the Device1 interface
        if DEVICE_INTERFACE in interfaces:
            properties = interfaces[DEVICE_INTERFACE]
            # Check if the device is currently connected
            if "Connected" in properties and properties["Connected"].value:
                logger.info(f"Disconnecting device at D-Bus path: {path}")
                # Get a proxy for the device to call its methods
                device_introspection = await bus.introspect(BLUEZ_SERVICE, path)
                device_obj = bus.get_proxy_object(
                    BLUEZ_SERVICE, path, device_introspection
                )
                device = device_obj.get_interface(DEVICE_INTERFACE)
                try:
                    # Call the Disconnect method
                    await device.call_disconnect()
                    logger.info(f"Disconnected device at {path}")
                except Exception as e:
                    logger.error(f"Failed to disconnect device {path}: {e}")


async def find_adapter_path(bus: MessageBus) -> Optional[str]:
    """
    Return the D-Bus object path of the first BlueZ adapter found
    (typically /org/bluez/hci0), or None if no adapter is present.
    """
    introspection = await bus.introspect(BLUEZ_SERVICE, "/")
    root = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
    manager = root.get_interface(OBJECT_MANAGER_INTERFACE)
    managed_objects = await manager.call_get_managed_objects()

    for path, interfaces in managed_objects.items():
        if ADAPTER_INTERFACE in interfaces:
            return path
    return None


async def remove_device(mac: str) -> bool:
    """
    Remove a device from BlueZ's internal cache via Adapter1.RemoveDevice.

    This clears BlueZ's per-device state (cached GATT, "Connected" flag, etc.)
    and is the cheapest recovery step when a specific device gets wedged.
    The device will be rediscovered on its next advertisement.

    :param mac: MAC address (colon-separated, e.g. "F9:A6:37:7A:DD:32").
    :return: True if a matching device was found and RemoveDevice was called.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(BLUEZ_SERVICE, "/")
        root = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER_INTERFACE)
        managed_objects = await manager.call_get_managed_objects()

        mac_upper = mac.upper()
        for path, interfaces in managed_objects.items():
            if DEVICE_INTERFACE not in interfaces:
                continue
            address = interfaces[DEVICE_INTERFACE].get("Address")
            if not address or address.value.upper() != mac_upper:
                continue

            # Adapter path is the device path's parent
            # (e.g. /org/bluez/hci0/dev_XX -> /org/bluez/hci0)
            adapter_path = path.rsplit("/", 1)[0]
            adapter_intro = await bus.introspect(BLUEZ_SERVICE, adapter_path)
            adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adapter_intro)
            adapter = adapter_obj.get_interface(ADAPTER_INTERFACE)
            await adapter.call_remove_device(path)
            logger.debug(f"BlueZ RemoveDevice succeeded for {mac} ({path})")
            return True

        logger.debug(f"RemoveDevice: no BlueZ device found for {mac}")
        return False
    finally:
        bus.disconnect()


async def reset_adapter(power_off_delay: float = 2.0) -> bool:
    """
    Power-cycle the BLE adapter (Adapter1.Powered = False, sleep, then True).

    Clears all of BlueZ's in-memory connection/device state without restarting
    bluetoothd. Use when individual RemoveDevice calls aren't recovering the
    stack (e.g. multiple devices wedged, or bluetoothd internal state stuck).

    :param power_off_delay: Seconds to wait between power-off and power-on.
    :return: True if the cycle was issued against an adapter.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        adapter_path = await find_adapter_path(bus)
        if adapter_path is None:
            logger.error("reset_adapter: no BlueZ adapter found")
            return False

        adapter_intro = await bus.introspect(BLUEZ_SERVICE, adapter_path)
        adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adapter_intro)
        adapter = adapter_obj.get_interface(ADAPTER_INTERFACE)

        logger.warning(f"Powering off BLE adapter at {adapter_path}")
        await adapter.set_powered(False)
        await asyncio.sleep(power_off_delay)
        logger.warning(f"Powering on BLE adapter at {adapter_path}")
        await adapter.set_powered(True)
        return True
    finally:
        bus.disconnect()
