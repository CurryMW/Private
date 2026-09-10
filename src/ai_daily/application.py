from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from pathlib import Path
from typing import Generic, Protocol, TypeVar

import httpx

from ai_daily.delivery_state import DeliveryState
from ai_daily.state import SentState


class RunStatus(StrEnum):
    SENT = "sent"
    PREVIEW = "dry-run"
    EMPTY = "empty"
    ALREADY_PROCESSED = "already-sent"
    FAILED = "failed"


StateT = TypeVar("StateT")


class StateStore(Protocol, Generic[StateT]):
    def load(self) -> StateT: ...

    def save(self, state: StateT) -> None: ...


class MessageSender(Protocol):
    async def send(self, parts: Sequence[str], title: str) -> None: ...


class HTTPClientFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...


class SentStateFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> SentState:
        return SentState.load(self._path)

    def save(self, state: SentState) -> None:
        state.save(self._path)


class DeliveryStateFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> DeliveryState:
        return DeliveryState.load(self._path)

    def save(self, state: DeliveryState) -> None:
        state.save(self._path)
