import asyncio
import unittest
from unittest import mock

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

import ble_scanner
from ble_scanner import BLEScanner
from fakes import FakeBlueZ, FakeBroadcaster, FakeScannerFactory, advertisement, wait_until

FAST = dict(
    WATCHDOG_INTERVAL_SECONDS=0.02,
    RESTART_PAUSE_SECONDS=0.01,
    ERROR_BACKOFF_MAX_SECONDS=0.01,
    SCAN_EVENT_MIN_INTERVAL_SECONDS=0.2,
)


class ScannerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.multiple(ble_scanner, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.factory = FakeScannerFactory()
        self.bluez = FakeBlueZ()
        self.events = FakeBroadcaster()
        self.scanner = BLEScanner(
            self.events, scanner_factory=self.factory, bluez=self.bluez
        )

    async def asyncTearDown(self):
        await self.scanner.stop_scan()

    async def start(self):
        await self.scanner.start_scan([])
        await wait_until(lambda: self.factory.starts == 1)


class TestSessions(ScannerTestCase):
    async def test_restart_request_starts_a_new_session(self):
        await self.start()
        self.scanner.request_restart("adapter reset")
        self.assertFalse(self.scanner.ready_for_connects())
        await wait_until(lambda: self.factory.starts == 2)
        self.assertEqual(self.factory.stops, 1)
        self.assertEqual(self.scanner.session_starts["adapter reset"], 1)
        self.assertTrue(self.scanner.ready_for_connects())

    async def test_restart_waits_for_the_radio_lock(self):
        await self.start()
        async with self.scanner.radio_lock:
            self.scanner.request_restart("rediscover missing sensors")
            await asyncio.sleep(0.05)
            self.assertEqual(self.factory.stops, 0)
        await wait_until(lambda: self.factory.starts == 2)

    async def test_no_connect_fits_between_stop_and_start(self):
        await self.start()

        async def competitor():
            while True:
                async with self.scanner.radio_lock:
                    self.factory.sequence.append("other")
                await asyncio.sleep(0.001)

        task = asyncio.create_task(competitor())
        try:
            for _ in range(3):
                self.scanner.request_restart("adapter reset")
                await asyncio.sleep(0.03)
        finally:
            task.cancel()
        seq = self.factory.sequence
        self.assertEqual(seq.count("stop"), 3)
        for i, step in enumerate(seq):
            if step == "stop":
                self.assertEqual(seq[i + 1], "start")

    async def test_not_ready_for_connects_until_running(self):
        self.assertTrue(self.scanner.ready_for_connects())
        async with self.scanner.radio_lock:
            await self.scanner.start_scan([])
            await asyncio.sleep(0.02)
            self.assertFalse(self.scanner.ready_for_connects())
        await wait_until(self.scanner.ready_for_connects)

    async def test_no_restart_without_a_request(self):
        await self.start()
        await asyncio.sleep(0.1)
        self.assertEqual((self.factory.starts, self.factory.stops), (1, 0))

    async def test_stop_error_after_reset_is_tolerated(self):
        self.factory.stop_error = BleakError("[org.bluez.Error.Failed] No discovery started")
        await self.start()
        self.scanner.request_restart("adapter reset")
        await wait_until(lambda: self.factory.starts == 2)

    async def test_reports_how_long_start_has_failed(self):
        error = "[org.bluez.Error.InProgress] Operation already in progress"
        self.factory.start_error = BleakError(error)
        await self.scanner.start_scan([])
        await wait_until(lambda: self.factory.starts >= 3)
        first = self.scanner.failing_for()
        await asyncio.sleep(0.05)
        self.assertGreater(self.scanner.failing_for(), first)
        self.assertEqual(self.scanner.health()["last_error"], error)
        self.assertFalse(self.scanner.ready_for_connects())

        self.factory.start_error = None
        await wait_until(self.scanner.ready_for_connects)
        self.assertIsNone(self.scanner.failing_for())

    async def test_no_restart_once_stopped(self):
        await self.start()
        await self.scanner.stop_scan()
        self.scanner.request_restart("adapter reset")
        await asyncio.sleep(0.05)
        self.assertEqual(self.factory.starts, 1)


class TestWatchdog(ScannerTestCase):
    async def test_restarts_when_discovery_stops(self):
        await self.start()
        self.bluez.discovering = False
        await wait_until(lambda: self.factory.starts == 2)
        self.assertEqual(self.scanner.session_starts["discovery stopped"], 1)

    async def test_powers_the_adapter_back_on(self):
        await self.start()
        self.bluez.powered = False
        await wait_until(lambda: self.factory.starts == 2)
        self.assertEqual(self.bluez.power_calls, [True])
        self.assertEqual(self.scanner.session_starts["adapter was powered off"], 1)

    async def test_ignores_a_busy_radio(self):
        await self.start()
        self.bluez.discovering = False
        async with self.scanner.radio_lock:
            await asyncio.sleep(0.15)
        self.assertEqual(self.factory.stops, 0)


class TestAdvertisements(ScannerTestCase):
    async def test_scan_events_are_throttled_per_device(self):
        seen = []
        self.scanner.set_device_found_callback(lambda device, rssi: seen.append(rssi))
        device = BLEDevice("AA:BB:CC:DD:EE:01", "Sensor", {"path": "/x", "props": {}})
        for rssi in (-60, -61, -62):
            self.scanner.detection_callback(device, advertisement(rssi=rssi))
        self.assertEqual(seen, [-60, -61, -62])
        self.assertEqual(len(self.events.scan_events()), 1)

        await asyncio.sleep(0.25)
        self.scanner.detection_callback(device, advertisement(rssi=-63))
        self.assertEqual(len(self.events.scan_events()), 2)

    async def test_filters_apply(self):
        self.scanner.scan_filters = ["0000aaaa-0000-1000-8000-00805f9b34fb"]
        device = BLEDevice("AA:BB:CC:DD:EE:01", "Sensor", {"path": "/x", "props": {}})
        self.scanner.detection_callback(device, advertisement())
        self.assertEqual(self.events.scan_events(), [])


if __name__ == "__main__":
    unittest.main()
