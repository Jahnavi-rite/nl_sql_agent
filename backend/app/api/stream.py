from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.stream_manager import stream_manager

logger = structlog.get_logger()
router = APIRouter(tags=["stream"])

PING_INTERVAL = 30.0
BUFFERED_EVENT_BATCH_SIZE = 20


@router.websocket("/sessions/{session_id}/stream")
async def session_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    conn_start = asyncio.get_event_loop().time()

    logger.info("ws_connected", session_id=session_id)

    stream = stream_manager.register_connection(session_id, ws)

    # Replay buffered events for reconnection support
    buffered = stream.replay_buffer()
    for event in buffered:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            logger.warning("ws_replay_failed", session_id=session_id)
            break

    last_ping = asyncio.get_event_loop().time()

    try:
        while True:
            # Check if pipeline is done
            if stream.is_done:
                final_events = stream.replay_buffer()[-5:]
                for ev in final_events:
                    try:
                        await ws.send_text(json.dumps(ev))
                    except Exception:
                        break
                break

            # Send ping if interval elapsed
            now = asyncio.get_event_loop().time()
            if now - last_ping >= PING_INTERVAL:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                last_ping = now

            # Wait for next event or client message with short timeout
            try:
                event = await asyncio.wait_for(
                    _next_event_or_message(stream, ws), timeout=1.0
                )
            except TimeoutError:
                continue

            if event is None:
                break

            # Forward event to client
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
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
        stream_manager.unregister_connection(session_id, ws)
        logger.info(
            "ws_closed",
            session_id=session_id,
            duration_ms=round((asyncio.get_event_loop().time() - conn_start) * 1000, 1),
        )


async def _next_event_or_message(stream, ws):
    """Wait for either a queue event or a client message.

    Returns event dict if from queue, None if stream is done,
    or continues looping on client messages.
    """
    while True:
        try:
            event = await asyncio.wait_for(stream._queue.get(), timeout=0.5)
            if event is None:
                return None
            return event
        except TimeoutError:
            # Check for client messages during timeout
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=0.1)
                if msg:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "ping":
                            await ws.send_text(json.dumps({"type": "pong"}))
                    except (json.JSONDecodeError, Exception):
                        pass
            except TimeoutError:
                # No client message, check if stream is done and retry queue
                if stream.is_done:
                    return None
                continue
            except Exception:
                return None
