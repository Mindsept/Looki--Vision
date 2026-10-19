"""Short-lived, single-use browser credentials; never expose the API key in a URL."""

import secrets
from time import monotonic
from uuid import UUID

from app.core.errors import DomainError


class SocketTickets:
    def __init__(self, ttl=30, capacity=256):
        self.ttl = ttl
        self.capacity = capacity
        self._tickets = {}

    def issue(self, session_id: UUID) -> str:
        now = monotonic()
        self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
        if len(self._tickets) >= self.capacity:
            raise DomainError(429, "too_many_tickets", "Trop de connexions en attente.")
        token = secrets.token_urlsafe(32)
        self._tickets[token] = (session_id, now + self.ttl)
        return token

    def consume(self, token: str | None, session_id: UUID) -> bool:
        entry = self._tickets.pop(token, None) if token else None
        return bool(entry and entry[0] == session_id and entry[1] > monotonic())
