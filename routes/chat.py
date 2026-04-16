"""Rotas de elaboracao de peca (background + polling) e chat interativo."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from services.claude_service import elaborar_peca
from services.chat_service import get_or_create_session, stop_session, _sessions

router = APIRouter()


# ----- Elaborar Peca (legado single-shot SSE) -----

async def _stream_elaboracao(paj_norm: str):
    async for chunk in elaborar_peca(paj_norm):
        yield {"event": "chunk", "data": chunk}
    yield {"event": "done", "data": "[fim]"}


@router.get("/api/elaborar/{paj_norm}")
async def elaborar_sse(paj_norm: str):
    return EventSourceResponse(_stream_elaboracao(paj_norm))


# ----- Fluxo novo: background + polling + correcao multi-turn -----

@router.post("/api/elaborar/start/{paj_norm}")
async def elaborar_start(paj_norm: str):
    """Inicia (ou reinicia) a elaboracao em background."""
    # Se ja existe uma sessao viva, nao reinicia — retorna status atual
    existing = _sessions.get(paj_norm)
    if existing and existing.is_alive() and existing.status == "running":
        return {"status": existing.status, "last_action": existing.last_action}

    # Recria sessao e inicia
    if existing:
        existing.stop()
    session = get_or_create_session(paj_norm)
    session.start()
    return {"status": session.status, "last_action": session.last_action}


@router.get("/api/elaborar/status/{paj_norm}")
async def elaborar_status(paj_norm: str):
    """Retorna status atual: idle/running/done/error + resumo se pronto."""
    session = _sessions.get(paj_norm)
    if not session:
        return {"status": "idle", "last_action": "", "summary": "", "error": ""}
    return {
        "status": session.status,
        "last_action": session.last_action,
        "summary": session.summary,
        "error": session.error,
        "alive": session.is_alive(),
    }


@router.post("/api/elaborar/correcao/{paj_norm}")
async def elaborar_correcao(paj_norm: str, payload: dict = Body(...)):
    """Envia correcao/discordancia pro Claude refazer."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"erro": "texto vazio"}, status_code=400)

    session = _sessions.get(paj_norm)
    if not session or not session.is_alive():
        return JSONResponse({"erro": "sessao inativa — reinicie com /start"}, status_code=400)

    session.send_message(text)
    return {"status": session.status, "last_action": session.last_action}


@router.post("/api/elaborar/stop/{paj_norm}")
async def elaborar_stop(paj_norm: str):
    stop_session(paj_norm)
    return {"status": "stopped"}


# ----- Chat Interativo (WebSocket — uso direto se quiser log completo) -----

@router.get("/chat/{paj_norm}", response_class=HTMLResponse)
async def chat_page(request: Request, paj_norm: str):
    template = request.app.state.jinja.get_template("chat.html")
    html = template.render(request=request, paj_norm=paj_norm)
    return HTMLResponse(html)


@router.websocket("/ws/chat/{paj_norm}")
async def chat_websocket(websocket: WebSocket, paj_norm: str):
    await websocket.accept()

    session = get_or_create_session(paj_norm)
    if not session.is_alive():
        session.start()

    try:
        async def send_output():
            while True:
                try:
                    event = session.output_queue.get_nowait()
                    await websocket.send_json(event)
                    if event.get("type") == "done":
                        break
                except Exception:
                    await asyncio.sleep(0.05)

        output_task = asyncio.create_task(send_output())

        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
                if data.get("type") == "message":
                    text = data.get("text", "")
                    if text.strip():
                        session.send_message(text)
                        await websocket.send_json({"type": "user", "text": text})
                elif data.get("type") == "stop":
                    stop_session(paj_norm)
                    break
            except asyncio.TimeoutError:
                if output_task.done():
                    break
                continue
            except WebSocketDisconnect:
                break

        output_task.cancel()

    except WebSocketDisconnect:
        pass
