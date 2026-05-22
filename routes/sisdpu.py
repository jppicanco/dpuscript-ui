"""Rotas SISDPU — preview JSON e execução SSE da movimentação."""

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.sisdpu_service import get_preview, executar_movimentacao

router = APIRouter(prefix="/api/paj")


@router.get("/{paj_norm}/sisdpu/preview")
async def preview(paj_norm: str, despacho: str) -> dict:
    """Retorna preview do que seria executado no SISDPU, sem fazer nada."""
    return get_preview(paj_norm, despacho)


@router.get("/{paj_norm}/sisdpu/executar")
async def executar(paj_norm: str, despacho: str, fase: str):
    """Executa a movimentação no SISDPU com streaming SSE de log."""

    async def _stream():
        async for linha in executar_movimentacao(paj_norm, despacho, fase):
            yield {"event": "log", "data": linha.rstrip()}
        yield {"event": "done", "data": "Concluído"}

    return EventSourceResponse(_stream())
