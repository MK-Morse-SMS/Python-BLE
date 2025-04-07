import logging

from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType

logger = logging.getLogger(__name__)

# D-Bus service and interface constants
BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
DEVICE_INTERFACE = "org.bluez.Device1"


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
