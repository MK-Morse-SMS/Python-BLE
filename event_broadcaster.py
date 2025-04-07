import asyncio
import logging
import uuid
from typing import Dict, Any

logger = logging.getLogger(__name__)


class EventBroadcaster:
    def __init__(self):
        self._client_queues = {}
        self._active_clients = set()

    def register_client(self) -> str:
        """Register a new client and return a unique client ID."""
        client_id = str(uuid.uuid4())
        self._client_queues[client_id] = asyncio.Queue()
        self._active_clients.add(client_id)
        logger.info(
            f"Client {client_id} registered. Total clients: {len(self._active_clients)}"
        )
        return client_id

    def unregister_client(self, client_id: str) -> None:
        """Unregister a client."""
        if client_id in self._active_clients:
            self._active_clients.remove(client_id)

        if client_id in self._client_queues:
            del self._client_queues[client_id]

        logger.info(
            f"Client {client_id} unregistered. Remaining clients: {len(self._active_clients)}"
        )

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """Broadcast an event to all active clients."""
        if not self._active_clients:
            return

        logger.debug(f"Broadcasting event to {len(self._active_clients)} clients")

        # Put the event in all client queues
        for client_id in list(self._active_clients):
            if client_id not in self._client_queues:
                continue

            try:
                await self._client_queues[client_id].put(event)
            except Exception as e:
                logger.error(f"Failed to queue event for client {client_id}: {str(e)}")
                self.unregister_client(client_id)

    async def get_client_event(self, client_id: str) -> Dict[str, Any]:
        """Wait for and return the next event for a specific client."""
        if client_id not in self._client_queues:
            raise KeyError(f"Client {client_id} not registered")

        return await self._client_queues[client_id].get()

    @property
    def client_count(self) -> int:
        """Return the number of active clients."""
        return len(self._active_clients)
