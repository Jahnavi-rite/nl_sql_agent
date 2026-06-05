from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from typing import Any

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()


class SessionStream:
    def __init__(self, session_id: str, buffer_size: int = 100) -> None:
        self.session_id = session_id
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._done = False
        self._created_at = time.time()
        self._event_count = 0
        self._websockets: set[WebSocket] = set()

    def publish(self, event: dict[str, Any]) -> None:
        self._buffer.append(event)
        self._event_count += 1
        self._queue.put_nowait(event)

    def mark_done(self) -> None:
        if not self._done:
            self._done = True
            self._queue.put_nowait(None)

    @property
    def is_done(self) -> bool:
        return self._done

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def age_seconds(self) -> float:
        return time.time() - self._created_at

    def replay_buffer(self) -> list[dict[str, Any]]:
        return list(self._buffer)

    def register_ws(self, ws: WebSocket) -> None:
        self._websockets.add(ws)

    def unregister_ws(self, ws: WebSocket) -> None:
        self._websockets.discard(ws)

    @property
    def ws_count(self) -> int:
        return len(self._websockets)


class StreamManager:
    _instance: StreamManager | None = None
    _initialized: bool = False

    def __new__(cls) -> StreamManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._sessions: dict[str, SessionStream] = {}
        self._metrics: dict[str, Any] = {
            "active_sessions": 0,
            "total_sessions_created": 0,
            "active_connections": 0,
            "total_connections": 0,
            "events_emitted": 0,
            "reconnect_count": 0,
            "dropped_connections": 0,
            "stream_duration_total_ms": 0.0,
        }
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        logger.info("stream_manager_initialized")

    def get_or_create_stream(self, session_id: str) -> SessionStream:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionStream(session_id)
            self._metrics["total_sessions_created"] += 1
            self._metrics["active_sessions"] = len(self._sessions)
        return self._sessions[session_id]

    def get_stream(self, session_id: str) -> SessionStream | None:
        return self._sessions.get(session_id)

    def publish_event(self, session_id: str, event: dict[str, Any]) -> None:
        stream = self.get_or_create_stream(session_id)
        stream.publish(event)
        self._metrics["events_emitted"] += 1

    def mark_done(self, session_id: str) -> None:
        stream = self.get_stream(session_id)
        if stream:
            stream.mark_done()

    def register_connection(
        self, session_id: str, ws: WebSocket, is_reconnect: bool = False
    ) -> SessionStream:
        stream = self.get_or_create_stream(session_id)
        stream.register_ws(ws)
        self._metrics["active_connections"] = sum(
            s.ws_count for s in self._sessions.values()
        )
        self._metrics["total_connections"] += 1
        if is_reconnect:
            self._metrics["reconnect_count"] += 1
        return stream

    def unregister_connection(
        self,
        session_id: str,
        ws: WebSocket,
        duration_ms: float | None = None,
    ) -> None:
        stream = self.get_stream(session_id)
        if stream:
            stream.unregister_ws(ws)
            self._metrics["active_connections"] = sum(
                s.ws_count for s in self._sessions.values()
            )
            if duration_ms is not None:
                self._metrics["stream_duration_total_ms"] += duration_ms
            else:
                self._metrics["stream_duration_total_ms"] += stream.age_seconds * 1000
            if stream.ws_count == 0 and stream.is_done:
                self._cleanup_stream(session_id)

    def _cleanup_stream(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._metrics["active_sessions"] = len(self._sessions)
        logger.info("stream_cleaned_up", session_id=session_id)

    def get_metrics(self) -> dict[str, Any]:
        return {**self._metrics, "active_sessions": len(self._sessions)}

    async def _cleanup_stale_connections(self) -> None:
        while True:
            await asyncio.sleep(30)
            stale_sessions = []
            for sid, stream in list(self._sessions.items()):
                if stream.is_done and stream.ws_count == 0 and stream.age_seconds > 120:
                    stale_sessions.append(sid)
            for sid in stale_sessions:
                self._cleanup_stream(sid)
            if stale_sessions:
                logger.info(
                    "stale_streams_cleaned",
                    count=len(stale_sessions),
                    sessions=stale_sessions,
                )

    async def start_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_stale_connections())

    async def stop_cleanup_task(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task


stream_manager = StreamManager()
