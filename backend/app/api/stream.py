from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.stream_manager import stream_manager

logger = structlog.get_logger()
router = APIRouter(tags=["stream"])

PING_INTERVAL = 25.0
PONG_TIMEOUT = 10.0
BUFFERED_EVENT_BATCH_SIZE = 20


async def _send_with_backpressure(ws: WebSocket, data: str) -> bool:
    try:
        await ws.send_text(data)
        return True
    except Exception:
        return False


@router.websocket("/sessions/{session_id}/stream")
async def session_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    conn_start = asyncio.get_event_loop().time()

    logger.info("ws_connected", session_id=session_id)

    stream = stream_manager.register_connection(session_id, ws)
    stream_metrics = stream_manager.get_metrics()

    logger.info(
        "ws_stream_attached",
        session_id=session_id,
        active_connections=stream_metrics["active_connections"],
    )

    buffered = stream.replay_buffer()
    for i in range(0, len(buffered), BUFFERED_EVENT_BATCH_SIZE):
        batch = buffered[i : i + BUFFERED_EVENT_BATCH_SIZE]
        for event in batch:
            import json as _json

            ok = await _send_with_backpressure(ws, _json.dumps(event))
            if not ok:
                logger.warning(
                    "ws_send_failed_during_replay",
                    session_id=session_id,
                    event_index=i,
                )
                break
        else:
            continue
        break

    ping_task: asyncio.Task[None] | None = None
    cancelled = False

    async def _ping_loop():
        nonlocal cancelled
        while not cancelled:
            await asyncio.sleep(PING_INTERVAL)
            try:
                import json as _json

                await ws.send_text(_json.dumps({"type": "ping"}))
                pong = await asyncio.wait_for(ws.receive_text(), timeout=PONG_TIMEOUT)
                if pong:
                    pass
            except Exception:
                break

    ping_task = asyncio.create_task(_ping_loop())

    try:
        while True:
            if stream.is_done:
                import json as _json

                final_events = stream.replay_buffer()[-5:]
                for ev in final_events:
                    await _send_with_backpressure(ws, _json.dumps(ev))
                break

            try:
                event = await asyncio.wait_for(
                    _get_next_event(stream, ws), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            if event is None:
                break

            import json as _json

            ok = await _send_with_backpressure(ws, _json.dumps(event))
            if not ok:
                logger.warning("ws_send_failed", session_id=session_id)
                break

    except WebSocketDisconnect:
        logger.info(
            "ws_disconnected",
            session_id=session_id,
            duration_ms=round((asyncio.get_event_loop().time() - conn_start) * 1000, 1),
        )
        stream_manager._metrics["dropped_connections"] += 1
    except Exception as exc:
        logger.error("ws_error", session_id=session_id, error=str(exc))
    finally:
        cancelled = True
        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        stream_manager.unregister_connection(session_id, ws)
        logger.info(
            "ws_closed",
            session_id=session_id,
            duration_ms=round((asyncio.get_event_loop().time() - conn_start) * 1000, 1),
        )


async def _get_next_event(stream, ws):
    receive_task = asyncio.create_task(ws.receive_text())
    queue_task = asyncio.create_task(_queue_get(stream))

    done, pending = await asyncio.wait(
        [receive_task, queue_task], return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    if receive_task in done:
        msg = receive_task.result()
        if msg:
            import json as _json

            try:
                data = _json.loads(msg)
                if data.get("type") == "ping":
                    import json as _json2

                    await ws.send_text(_json2.dumps({"type": "pong"}))
            except Exception:
                pass
        return await _queue_get(stream)

    if queue_task in done:
        return queue_task.result()

    return None


async def _queue_get(stream):
    import asyncio as _asyncio

    while True:
        try:
            event = await _asyncio.wait_for(stream._queue.get(), timeout=0.5)
            return event
        except _asyncio.TimeoutError:
            if stream.is_done:
                return None
            continue
