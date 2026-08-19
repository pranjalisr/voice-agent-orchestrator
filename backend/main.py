"""
FastAPI server: serves the voice UI and bridges the browser to the
orchestrator over a WebSocket.

Wire protocol (JSON both ways) is defined in models.py. Each browser tab gets
its own Orchestrator instance, so state (and the pending-effects ledger) is
per-connection.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from models import ClientEvent, ClientEventType, ServerEvent, ServerEventType
from orchestrator import Orchestrator

app = FastAPI(title="Voice-to-Agent Orchestrator")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()

    async def emit(event: ServerEvent) -> None:
        try:
            await websocket.send_text(event.model_dump_json())
        except Exception:  # noqa: BLE001 — client vanished mid-send
            pass

    orch = Orchestrator(emit)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                event = ClientEvent(**json.loads(raw))
            except Exception as exc:  # noqa: BLE001 — malformed frame
                await emit(ServerEvent(type=ServerEventType.ERROR,
                                       data={"message": f"bad event: {exc}"}))
                continue

            if event.type == ClientEventType.USER_UTTERANCE:
                await orch.handle_user_utterance(event.text)
            elif event.type == ClientEventType.BARGE_IN:
                await orch.handle_barge_in(event.text)
            elif event.type == ClientEventType.CANCEL:
                await orch.handle_cancel()
    except WebSocketDisconnect:
        # Tear down any in-flight turn so tasks don't leak.
        if orch.is_busy() and orch._turn_task:
            orch._turn_task.cancel()


# Serve the rest of the frontend (app.js, styles.css) as static files.
app.mount("/", StaticFiles(directory=str(FRONTEND)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
