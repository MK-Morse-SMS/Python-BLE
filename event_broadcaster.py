import asyncio
import logging
import uuid
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Per-client backlog. A client that stops reading loses its oldest events
# instead of growing pyble's memory without limit.
MAX_QUEUED_EVENTS = 2048


class EventBroadcaster:
    def __init__(self, max_queued_events: int = MAX_QUEUED_EVENTS):
        self._client_queues: Dict[str, asyncio.Queue] = {}
        self._max_queued_events = max_queued_events
        self.dropped_events = 0

    def register_client(self) -> str:
        """Register a new client and return a unique client ID."""
        client_id = str(uuid.uuid4())
        self._client_queues[client_id] = asyncio.Queue(maxsize=self._max_queued_events)
        logger.info(
            f"Client {client_id} registered. Total clients: {len(self._client_queues)}"
        )
        return client_id

    def unregister_client(self, client_id: str) -> None:
        """Unregister a client."""
        self._client_queues.pop(client_id, None)
        logger.info(
            f"Client {client_id} unregistered. Remaining clients: {len(self._client_queues)}"
        )

    def publish(self, event: Dict[str, Any]) -> None:
        """Queue an event for every client, dropping a full queue's oldest event."""
        for client_id, queue in list(self._client_queues.items()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_events += 1
                if self.dropped_events == 1 or self.dropped_events % 1000 == 0:
                    logger.warning(
                        f"SSE client {client_id} is not keeping up; "
                        f"{self.dropped_events} events dropped so far"
                    )
            queue.put_nowait(event)

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """Async alias of publish()."""
        self.publish(event)

    async def get_client_event(self, client_id: str) -> Dict[str, Any]:
        """Wait for and return the next event for a specific client."""
        if client_id not in self._client_queues:
            raise KeyError(f"Client {client_id} not registered")

        return await self._client_queues[client_id].get()

    @property
    def client_count(self) -> int:
        """Return the number of active clients."""
        return len(self._client_queues)
