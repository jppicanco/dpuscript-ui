"""Rotas de chat interativo e elaboracao de peca."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from services.claude_service import elaborar_peca
from services.chat_service import get_or_create_session, stop_session

router = APIRouter()


# ----- Elaborar Peca (non-interactive, SSE) -----

async def _stream_elaboracao(paj_norm: str):
    async for chunk in elaborar_peca(paj_norm):
        yield {"event": "chunk", "data": chunk}
    yield {"event": "done", "data": "[fim]"}


@router.get("/api/elaborar/{paj_norm}")
async def elaborar(paj_norm: str):
    return EventSourceResponse(_stream_elaboracao(paj_norm))


# ----- Chat Interativo (WebSocket) -----

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
        # Task paralela: ler output do Claude e enviar pro browser
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

        # Loop principal: ler mensagens do browser e enviar pro Claude
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
                if data.get("type") == "message":
                    text = data.get("text", "")
                    if text.strip():
                        session.send_message(text)
                        # Echo da mensagem do usuario pro chat
                        await websocket.send_json({"type": "user", "text": text})
                elif data.get("type") == "stop":
                    stop_session(paj_norm)
                    break
            except asyncio.TimeoutError:
                # Verifica se o output_task terminou
                if output_task.done():
                    break
                continue
            except WebSocketDisconnect:
                break

        output_task.cancel()

    except WebSocketDisconnect:
        pass
    finally:
        # Nao mata a sessao ao desconectar — pode reconectar
        pass
