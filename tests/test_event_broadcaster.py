import unittest

from event_broadcaster import EventBroadcaster


class TestEventBroadcaster(unittest.IsolatedAsyncioTestCase):
    async def test_full_queue_drops_oldest(self):
        broadcaster = EventBroadcaster(max_queued_events=3)
        client = broadcaster.register_client()
        for i in range(5):
            broadcaster.publish({"n": i})

        received = [(await broadcaster.get_client_event(client))["n"] for _ in range(3)]
        self.assertEqual(received, [2, 3, 4])
        self.assertEqual(broadcaster.dropped_events, 2)

    async def test_unregistered_client_gets_nothing(self):
        broadcaster = EventBroadcaster()
        client = broadcaster.register_client()
        broadcaster.unregister_client(client)
        broadcaster.publish({"n": 1})
        self.assertEqual(broadcaster.client_count, 0)


if __name__ == "__main__":
    unittest.main()
