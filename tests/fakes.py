import asyncio
from types import SimpleNamespace

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError


def device_path(mac: str) -> str:
    return "/org/bluez/hci0/dev_" + mac.replace(":", "_")


async def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class FakeBlueZ:
    """Stands in for dbus_utils."""

    def __init__(self, forget_on_remove: bool = True):
        self.present = set()
        self.removed = []
        self.resets = 0
        self.powered = True
        self.discovering = True
        self.power_calls = []
        self.forget_on_remove = forget_on_remove

    async def bluez_device(self, mac):
        if mac in self.present:
            return BLEDevice(mac, f"Sensor {mac[-5:]}", {"path": device_path(mac), "props": {}})
        return None

    async def remove_device(self, mac):
        self.removed.append(mac)
        if self.forget_on_remove:
            self.present.discard(mac)
        return True

    async def reset_adapter(self):
        self.resets += 1
        return True

    async def adapter_state(self):
        return self.powered, self.discovering

    async def set_adapter_powered(self, powered):
        self.power_calls.append(powered)
        self.powered = powered
        return True


class FakeClient:
    def __init__(self, factory, device, disconnected_callback, timeout):
        self.factory = factory
        self.device = device
        self.callback = disconnected_callback
        self.timeout = timeout
        self.connected = False

    async def connect(self):
        script = self.factory.scripts.get(self.device.address)
        step = script.pop(0) if script else self.factory.default
        self.factory.connects.append((self.device.address, step))
        if step == "ok":
            self.connected = True
            return
        if step == "timeout":
            raise asyncio.TimeoutError()
        if step == "not_found":
            name = self.device.details["path"].rsplit("/", 1)[-1]
            raise BleakError(f"device '{name}' not found")
        if step == "link_up_timeout":
            self.callback(self)
            raise asyncio.TimeoutError()
        if step == "hang":
            await asyncio.sleep(3600)
        raise AssertionError(f"unknown step {step}")

    async def disconnect(self):
        if self.connected:
            self.connected = False
            self.callback(self)
        return True

    def drop(self):
        """Simulate the link dropping."""
        self.connected = False
        self.callback(self)


class FakeClientFactory:
    """Stands in for BleakClient; ``scripts[mac]`` lists connect outcomes."""

    def __init__(self, default: str = "ok"):
        self.scripts = {}
        self.connects = []
        self.clients = []
        self.default = default

    def __call__(self, device, disconnected_callback=None, timeout=None):
        client = FakeClient(self, device, disconnected_callback, timeout)
        self.clients.append(client)
        return client

    def outcomes(self, mac):
        return [step for m, step in self.connects if m == mac]

    def last_client(self, mac):
        return [c for c in self.clients if c.device.address == mac][-1]


class FakeBroadcaster:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)

    async def broadcast(self, event):
        self.publish(event)

    def connection_events(self, mac):
        return [
            e["status"]
            for e in self.events
            if e.get("type") == "connection" and e["bdaddr"] == mac
        ]

    def scan_events(self):
        return [e for e in self.events if e.get("type") == "scan"]


class FakeScanner:
    def __init__(self, factory, detection_callback=None, service_uuids=None, scanning_mode=None):
        self.factory = factory
        self.detection_callback = detection_callback

    async def start(self):
        self.factory.starts += 1
        self.factory.sequence.append("start")

    async def stop(self):
        self.factory.stops += 1
        self.factory.sequence.append("stop")
        if self.factory.stop_error is not None:
            raise self.factory.stop_error


class FakeScannerFactory:
    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.stop_error = None
        self.sequence = []

    def __call__(self, **kwargs):
        return FakeScanner(self, **kwargs)


def advertisement(rssi=-60, service_uuids=("0000180f-0000-1000-8000-00805f9b34fb",)):
    return SimpleNamespace(rssi=rssi, service_uuids=list(service_uuids))
