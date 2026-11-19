import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from bleak.backends.device import BLEDevice
from dbus_fast import DBusError, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType

logger = logging.getLogger(__name__)

# D-Bus service and interface constants
BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
DEVICE_INTERFACE = "org.bluez.Device1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEFAULT_ADAPTER_PATH = "/org/bluez/hci0"

SYSTEMD_SERVICE = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
BLUETOOTH_UNIT = "bluetooth.service"


async def disconnect_all_devices() -> None:
    """
    Disconnect all currently connected BLE devices at the D-Bus (BlueZ) level.

    This utility can be used at startup or shutdown to ensure a clean slate.
    """
    # Connect to the system bus (BlueZ runs on the system bus)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
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
    finally:
        bus.disconnect()


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


async def _adapter_interface(bus: MessageBus, adapter_path: str):
    adapter_intro = await bus.introspect(BLUEZ_SERVICE, adapter_path)
    adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adapter_intro)
    return adapter_obj.get_interface(ADAPTER_INTERFACE)


async def remove_device(mac: str) -> bool:
    """
    Remove a device from BlueZ's internal cache via Adapter1.RemoveDevice.

    This clears BlueZ's per-device state, including its cached GATT database,
    so the next connect has to rediscover every service. The device comes back
    on its next advertisement seen by an active scan.

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
            adapter = await _adapter_interface(bus, path.rsplit("/", 1)[0])
            await adapter.call_remove_device(path)
            logger.debug(f"BlueZ RemoveDevice succeeded for {mac} ({path})")
            return True

        logger.debug(f"RemoveDevice: no BlueZ device found for {mac}")
        return False
    finally:
        bus.disconnect()


async def set_adapter_powered(powered: bool) -> bool:
    """
    Set Adapter1.Powered on the first adapter.

    :return: True if an adapter was found and the property was set.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        adapter_path = await find_adapter_path(bus)
        if adapter_path is None:
            logger.error("set_adapter_powered: no BlueZ adapter found")
            return False
        adapter = await _adapter_interface(bus, adapter_path)
        await adapter.set_powered(powered)
        return True
    finally:
        bus.disconnect()


async def reset_adapter(power_off_delay: float = 2.0, power_on_attempts: int = 3) -> bool:
    """
    Power-cycle the BLE adapter (Adapter1.Powered = False, sleep, then True).

    Clears all of BlueZ's in-memory connection/device state without restarting
    bluetoothd. It also ends every discovery session, so the scanner must start
    a new one afterwards. Power-on is retried, since a reset that leaves the
    adapter off strands every sensor.

    :param power_off_delay: Seconds to wait between power-off and power-on.
    :param power_on_attempts: How many times to try powering back on.
    :return: True if the cycle was issued against an adapter.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        adapter_path = await find_adapter_path(bus)
        if adapter_path is None:
            logger.error("reset_adapter: no BlueZ adapter found")
            return False

        adapter = await _adapter_interface(bus, adapter_path)

        logger.warning(f"Powering off BLE adapter at {adapter_path}")
        await adapter.set_powered(False)
        await asyncio.sleep(power_off_delay)
        for attempt in range(1, power_on_attempts + 1):
            logger.warning(f"Powering on BLE adapter at {adapter_path}")
            try:
                await adapter.set_powered(True)
                return True
            except Exception as e:
                if attempt == power_on_attempts:
                    raise
                logger.error(f"Power-on attempt {attempt} failed: {e}")
                await asyncio.sleep(power_off_delay)
        return False
    finally:
        bus.disconnect()



async def _call_systemd(
    bus: MessageBus, path: str, interface: str, member: str, signature: str, body: list
) -> list:
    reply = await bus.call(
        Message(
            destination=SYSTEMD_SERVICE,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
    return reply.body


async def bluetooth_service_uptime() -> Optional[float]:
    """Seconds since the host's systemd last started bluetoothd, or None if unknown."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        (unit_path,) = await _call_systemd(
            bus, SYSTEMD_PATH, SYSTEMD_MANAGER_INTERFACE, "GetUnit", "s", [BLUETOOTH_UNIT]
        )
        (started,) = await _call_systemd(
            bus,
            unit_path,
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            ["org.freedesktop.systemd1.Unit", "ActiveEnterTimestampMonotonic"],
        )
    finally:
        bus.disconnect()
    if not started.value:
        return None
    # Containers share the host kernel's CLOCK_MONOTONIC, which systemd uses.
    return time.monotonic() - started.value / 1e6


async def restart_bluetooth_service() -> None:
    """
    Ask the host's systemd to restart bluetoothd. Returns once the job is
    queued. bluetoothd powers the adapter off as it stops, so every connection
    and BlueZ object goes with it.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        await _call_systemd(
            bus,
            SYSTEMD_PATH,
            SYSTEMD_MANAGER_INTERFACE,
            "RestartUnit",
            "ss",
            [BLUETOOTH_UNIT, "replace"],
        )
    finally:
        bus.disconnect()

# The helpers below read bleak's in-process mirror of BlueZ's object tree
# (BlueZManager._properties / _adapters). bleak keeps it current from D-Bus
# signals, so reads cost no D-Bus traffic, and it is exactly what bleak checks
# before raising "device '...' not found". The attributes are private; if
# bleak's internals change, each helper falls back to a conservative answer.


async def _bluez_cache() -> Optional[Tuple[Dict[str, Dict[str, Any]], List[str]]]:
    manager = await get_global_bluez_manager()
    properties = getattr(manager, "_properties", None)
    adapters = getattr(manager, "_adapters", None)
    if not isinstance(properties, dict) or adapters is None:
        return None
    return properties, sorted(adapters)


def _device_path(adapter_path: str, mac: str) -> str:
    return f"{adapter_path}/dev_{mac.upper().replace(':', '_')}"


async def bluez_device(mac: str) -> Optional[BLEDevice]:
    """
    Return a connectable BLEDevice for ``mac`` if BlueZ currently has an
    object for it, otherwise None.

    BlueZ object paths are deterministic, so this replaces holding on to the
    BLEDevice from the scanner: what decides whether a connect can work is
    whether the object exists, not which BLEDevice instance is passed.
    """
    mac = mac.upper()
    cache = await _bluez_cache()
    if cache is None:
        # Can't tell; let the connect attempt find out.
        path = _device_path(DEFAULT_ADAPTER_PATH, mac)
        return BLEDevice(mac, None, {"path": path, "props": {}})

    properties, adapters = cache
    for adapter_path in adapters:
        path = _device_path(adapter_path, mac)
        props = properties.get(path, {}).get(DEVICE_INTERFACE)
        if props is not None:
            return BLEDevice(mac, props.get("Name"), {"path": path, "props": dict(props)})
    return None


async def adapter_state() -> Optional[Tuple[bool, bool]]:
    """
    Return (Powered, Discovering) for the first adapter, or None if unknown.
    """
    cache = await _bluez_cache()
    if cache is None:
        return None
    properties, adapters = cache
    for adapter_path in adapters:
        props = properties.get(adapter_path, {}).get(ADAPTER_INTERFACE)
        if props is not None:
            return bool(props.get("Powered")), bool(props.get("Discovering"))
    return None
