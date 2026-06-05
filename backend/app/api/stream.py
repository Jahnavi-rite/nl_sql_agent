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
async def session_stream(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    conn_start = asyncio.get_event_loop().time()
    ws_lock = asyncio.Lock()

    logger.info("ws_connected", session_id=session_id)

    stream = stream_manager.register_connection(session_id, ws)

    # Replay buffered events for reconnection support
    buffered = stream.replay_buffer()
    for event in buffered:
        try:
            async with ws_lock:
                await ws.send_text(json.dumps(event))
        except Exception:
            logger.warning("ws_replay_failed", session_id=session_id)
            break

    # Background task to read from WebSocket (handles client pings and disconnects)
    async def read_from_client() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                    if data.get("type") == "ping":
                        async with ws_lock:
                            await ws.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    pass
        except WebSocketDisconnect:
            raise
        except Exception:
            pass

    client_reader = asyncio.create_task(read_from_client())

    try:
        while not client_reader.done():
            # Check if pipeline is done
            if stream.is_done:
                final_events = stream.replay_buffer()[-5:]
                for ev in final_events:
                    try:
                        async with ws_lock:
                            await ws.send_text(json.dumps(ev))
                    except Exception:
                        break
                break

            # Wait for next event from stream queue, but also check if client disconnected
            try:
                event = await asyncio.wait_for(stream._queue.get(), timeout=1.0)  # type: ignore[arg-type]
                if event is None:
                    break
                async with ws_lock:
                    await ws.send_text(json.dumps(event))
            except TimeoutError:
                # No new event, check client_reader.done() at top of loop
                continue

        # If client reader finished with an exception (e.g. WebSocketDisconnect), raise it
        if client_reader.done():
            client_reader.result()

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
        client_reader.cancel()
        conn_duration_ms = (asyncio.get_event_loop().time() - conn_start) * 1000
        stream_manager.unregister_connection(session_id, ws, duration_ms=conn_duration_ms)
        logger.info(
            "ws_closed",
            session_id=session_id,
            duration_ms=round(conn_duration_ms, 1),
        )
