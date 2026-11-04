import asyncio
import os
import time
import unittest
from unittest import mock

import ble_connections
from ble_connections import BLEConnectionsManager
from fakes import FakeBlueZ, FakeBroadcaster, FakeClientFactory, wait_until

A = "AA:BB:CC:DD:EE:01"
B = "AA:BB:CC:DD:EE:02"

FAST = dict(
    SUPERVISOR_TICK_SECONDS=0.01,
    RECONNECT_BASE_SECONDS=0.01,
    RECONNECT_MAX_SECONDS=0.02,
    QUARANTINE_SECONDS=0.5,
    SHORT_CONNECTION_SECONDS=0.2,
    CONNECT_HARD_TIMEOUT_SECONDS=0.2,
    ADAPTER_STALL_SECONDS=0.3,
    ADAPTER_RESET_MIN_INTERVAL_SECONDS=0.5,
    REDISCOVERY_RESTART_SECONDS=0.1,
    REDISCOVERY_RESTART_MAX_SECONDS=0.4,
)


class ConnectionsTestCase(unittest.IsolatedAsyncioTestCase):
    forget_on_remove = True

    async def asyncSetUp(self):
        patcher = mock.patch.multiple(ble_connections, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.bluez = FakeBlueZ(forget_on_remove=self.forget_on_remove)
        self.clients = FakeClientFactory()
        self.events = FakeBroadcaster()
        self.restarts = []
        self.mgr = BLEConnectionsManager(
            self.events, bluez=self.bluez, client_factory=self.clients
        )
        self.mgr.request_scan_restart = self.restarts.append
        self.mgr.scan_session_age = lambda: 10.0
        self.mgr.start()

    async def asyncTearDown(self):
        await self.mgr.stop()

    def state(self, mac):
        return self.mgr.devices[mac]

    def connected(self, mac):
        return self.state(mac).client is not None


class TestConnecting(ConnectionsTestCase):
    async def test_waits_for_bluez_instead_of_failing(self):
        self.mgr.desire(A)
        await wait_until(lambda: self.state(A).missing_since is not None)
        await asyncio.sleep(0.05)
        self.assertEqual(self.clients.connects, [])
        self.assertEqual(self.state(A).consecutive_failures, 0)

        self.bluez.present.add(A)
        await wait_until(lambda: self.connected(A))
        self.assertEqual(self.events.connection_events(A), ["connected"])
        self.assertEqual(await self.mgr.get_connected_devices(), {A: "Sensor EE:01"})

    async def test_no_connects_while_scan_not_ready(self):
        ready = False
        self.mgr.scan_ready_for_connects = lambda: ready
        self.bluez.present.add(A)
        self.mgr.desire(A)
        await asyncio.sleep(0.1)
        self.assertEqual(self.clients.connects, [])
        ready = True
        await wait_until(lambda: self.connected(A))

    async def test_not_found_is_not_a_failure(self):
        self.bluez.present.add(A)
        self.clients.scripts[A] = ["not_found"]
        self.mgr.desire(A)
        await wait_until(lambda: self.connected(A))
        self.assertEqual(self.clients.outcomes(A), ["not_found", "ok"])
        self.assertEqual(self.state(A).failures, {})
        self.assertEqual(self.state(A).missing_events, 1)
        self.assertEqual(self.mgr._stall_failures, 0)

    async def test_link_up_timeout_retries_once(self):
        self.bluez.present.add(A)
        self.clients.scripts[A] = ["link_up_timeout"]
        self.mgr.desire(A)
        await wait_until(lambda: self.connected(A))
        await asyncio.sleep(0.05)
        self.assertEqual(self.clients.outcomes(A), ["link_up_timeout", "ok"])
        self.assertEqual(dict(self.state(A).failures), {"timeout_link_up": 1})
        self.assertEqual(self.events.connection_events(A), ["connected"])

    async def test_hung_connect_is_bounded(self):
        self.bluez.present.add(A)
        self.clients.scripts[A] = ["hang"]
        self.mgr.desire(A)
        await wait_until(lambda: self.connected(A), timeout=1.0)
        self.assertEqual(dict(self.state(A).failures), {"timeout": 1})

    async def test_round_robin_between_failing_sensors(self):
        self.bluez.present.update({A, B})
        self.clients.default = "timeout"
        self.mgr.desire(A)
        self.mgr.desire(B)
        await wait_until(lambda: len(self.clients.connects) >= 4)
        first_four = [mac for mac, _ in self.clients.connects[:4]]
        self.assertEqual(sorted(first_four), [A, A, B, B])

    async def test_deliberate_disconnect_does_not_reconnect(self):
        self.bluez.present.add(A)
        self.mgr.desire(A)
        await wait_until(lambda: self.connected(A))
        await self.mgr.disconnect_device(A)
        await asyncio.sleep(0.1)
        self.assertEqual(self.clients.outcomes(A), ["ok"])
        self.assertEqual(self.events.connection_events(A), ["connected", "disconnected"])
        self.assertEqual(self.state(A).consecutive_failures, 0)


class TestDrops(ConnectionsTestCase):
    async def test_short_drop_counts_long_drop_does_not(self):
        self.bluez.present.add(A)
        self.mgr.desire(A)
        await wait_until(lambda: self.connected(A))

        self.clients.last_client(A).drop()
        self.assertEqual(dict(self.state(A).failures), {"short_connection": 1})
        self.assertEqual(self.state(A).consecutive_failures, 1)
        await wait_until(lambda: self.connected(A))

        await asyncio.sleep(ble_connections.SHORT_CONNECTION_SECONDS + 0.05)
        self.assertEqual(self.state(A).consecutive_failures, 0)
        self.clients.last_client(A).drop()
        self.assertEqual(self.state(A).consecutive_failures, 0)
        await wait_until(lambda: self.connected(A))
        self.assertEqual(
            self.events.connection_events(A),
            ["connected", "disconnected", "connected", "disconnected", "connected"],
        )


class TestPerSensorRecovery(ConnectionsTestCase):
    async def test_remove_every_third_failure_then_rescan(self):
        self.bluez.present.add(A)
        self.clients.scripts[A] = ["timeout", "timeout", "timeout"]
        self.mgr.desire(A)
        await wait_until(lambda: self.bluez.removed == [A])
        self.assertEqual(len(self.clients.outcomes(A)), 3)
        self.assertIn("rediscover after RemoveDevice", self.restarts)
        self.assertIsNotNone(self.state(A).missing_since)

        self.bluez.present.add(A)
        await wait_until(lambda: self.connected(A))
        self.assertEqual(self.clients.outcomes(A), ["timeout"] * 3 + ["ok"])

    async def test_rediscovery_restarts_back_off(self):
        self.mgr.desire(A)
        await asyncio.sleep(1.5)
        self.assertEqual(self.restarts, ["rediscover missing sensors"] * 3)

    async def test_rediscovery_waits_for_a_long_enough_session(self):
        self.mgr.scan_session_age = lambda: 0.05
        self.mgr.desire(A)
        await asyncio.sleep(0.3)
        self.assertEqual(self.restarts, [])


class TestQuarantine(ConnectionsTestCase):
    forget_on_remove = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Keep the adapter-wide rule (which clears quarantine) out of the way.
        patcher = mock.patch.object(ble_connections, "ADAPTER_STALL_SECONDS", 60)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_long_backoff_after_six_failures(self):
        self.bluez.present.add(A)
        self.clients.scripts[A] = ["timeout"] * 6
        self.mgr.desire(A)
        await wait_until(lambda: len(self.clients.outcomes(A)) == 6)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.clients.outcomes(A)), 6)
        self.assertEqual(self.bluez.removed, [A, A])
        await wait_until(lambda: self.connected(A), timeout=1.0)

    async def test_one_bad_sensor_never_resets_the_adapter(self):
        self.bluez.present.update({A, B})
        self.clients.scripts[B] = ["timeout"] * 100
        self.mgr.desire(A)
        self.mgr.desire(B)
        await asyncio.sleep(1.2)
        self.assertTrue(self.connected(A))
        self.assertGreaterEqual(self.state(B).consecutive_failures, 6)
        self.assertEqual(self.bluez.resets, 0)


class TestAdapterRecovery(ConnectionsTestCase):
    forget_on_remove = False

    async def test_stall_resets_adapter_spaced_out_then_exits(self):
        exits = []
        reset_times = []
        real_reset = self.bluez.reset_adapter

        async def timed_reset():
            reset_times.append(time.monotonic())
            return await real_reset()

        self.bluez.reset_adapter = timed_reset
        self.bluez.present.update({A, B})
        self.clients.default = "timeout"
        self.mgr.desire(A)
        self.mgr.desire(B)
        with mock.patch.object(os, "_exit", side_effect=lambda code: exits.append(self.bluez.resets)):
            await wait_until(lambda: exits, timeout=4.0)
            await self.mgr.stop()

        self.assertEqual(exits[0], ble_connections.EXIT_AFTER_FAILED_RESETS)
        self.assertGreaterEqual(
            reset_times[1] - reset_times[0], ble_connections.ADAPTER_RESET_MIN_INTERVAL_SECONDS
        )
        self.assertIn("adapter reset", self.restarts)

    async def test_success_between_resets_prevents_exit(self):
        self.bluez.present.add(A)
        self.clients.default = "timeout"
        self.mgr.desire(A)
        await wait_until(lambda: self.bluez.resets == 1, timeout=2.0)
        self.clients.default = "ok"
        await wait_until(lambda: self.connected(A))
        self.assertEqual(self.mgr._resets_since_success, 0)


if __name__ == "__main__":
    unittest.main()
