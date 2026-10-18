"""``/v1/events`` WebSocket: job progress, previews, downloads, live stats."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import API_VERSION
from .deps import check_ws

router = APIRouter(prefix="/v1")

HEARTBEAT_S = 15


@router.websocket("/events")
async def events(ws: WebSocket):
    if not check_ws(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    st = ws.app.state.sarness
    q = st.bus.subscribe()
    await ws.send_text(json.dumps({"type": "hello", "data": {"api_version": API_VERSION, "heartbeat_s": HEARTBEAT_S}}))

    async def reader():
        # Clients may send {"type":"ping"}; anything else is ignored.
        while True:
            msg = await ws.receive_text()
            if '"ping"' in msg:
                await ws.send_text('{"type":"pong"}')

    rtask = asyncio.create_task(reader())
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
                await ws.send_text(json.dumps(ev, default=str))
            except asyncio.TimeoutError:
                await ws.send_text('{"type":"heartbeat"}')
            if rtask.done():
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rtask.cancel()
        st.bus.unsubscribe(q)
