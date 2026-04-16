"""Rotas de elaboracao de peca (background + polling) e chat interativo."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

from services.chat_service import (
    get_or_create_session,
    stop_session,
    start_or_queue,
    get_stats,
    ler_elaboracao_disco,
    _sessions,
    _queue,
)
from config import ENTRADA_DIR

router = APIRouter()


# ----- Elaborar Peca (background + polling + correcao multi-turn) -----

@router.post("/api/elaborar/start/{paj_norm}")
async def elaborar_start(paj_norm: str):
    """Inicia elaboracao (ou enfileira se limite de paralelos atingido)."""
    return start_or_queue(paj_norm)


@router.get("/api/elaborar/stats")
async def elaborar_stats():
    """Retorna {running, queued, max_parallel} — pro dashboard."""
    return get_stats()


@router.get("/api/elaborar/status")
async def elaborar_status_all():
    """Retorna status de todas as sessoes — RAM + persistencia em disco.

    Busca status em memoria (sessoes ativas) e tambem PAJs que ja foram
    elaborados antes (tem elaboracao.json no disco). Prefere RAM sobre disco
    quando os dois existem.
    """
    result: dict[str, dict] = {}

    # 1. PAJs com elaboracao persistida em disco (resultado de runs anteriores)
    if ENTRADA_DIR.exists():
        for pasta in ENTRADA_DIR.iterdir():
            if not pasta.is_dir():
                continue
            persist = ler_elaboracao_disco(pasta.name)
            if persist:
                result[pasta.name] = {
                    "status": persist.get("status", "done"),
                    "last_action": persist.get("last_action", ""),
                    "alive": False,
                    "persisted": True,
                }

    # 2. Sessoes em memoria sobrescrevem (dados mais frescos)
    for paj_norm, session in _sessions.items():
        result[paj_norm] = {
            "status": session.status,
            "last_action": session.last_action,
            "alive": session.is_alive(),
            "persisted": False,
        }
    return result


@router.get("/api/elaborar/status/{paj_norm}")
async def elaborar_status(paj_norm: str):
    """Retorna status atual: le sessao em memoria OU elaboracao.json do disco."""
    session = _sessions.get(paj_norm)
    if session:
        return {
            "status": session.status,
            "last_action": session.last_action,
            "summary": session.summary,
            "error": session.error,
            "alive": session.is_alive(),
            "persisted": False,
        }
    # Fallback: le do disco (persistido de run anterior)
    persist = ler_elaboracao_disco(paj_norm)
    if persist:
        return {
            "status": persist.get("status", "done"),
            "last_action": persist.get("last_action", ""),
            "summary": persist.get("summary", ""),
            "error": "",
            "alive": False,
            "persisted": True,
            "concluido_em": persist.get("concluido_em", ""),
        }
    return {"status": "idle", "last_action": "", "summary": "", "error": ""}


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
