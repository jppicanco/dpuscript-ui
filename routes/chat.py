"""Rota de elaboracao de peca — roda Claude Code CLI com streaming."""

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.claude_service import elaborar_peca

router = APIRouter(prefix="/api/elaborar")


async def _stream_elaboracao(paj_norm: str):
    """Gera eventos SSE com cada chunk do Claude Code."""
    async for chunk in elaborar_peca(paj_norm):
        yield {"event": "chunk", "data": chunk}
    yield {"event": "done", "data": "[fim]"}


@router.get("/{paj_norm}")
async def elaborar(paj_norm: str):
    """Roda Claude Code CLI pra elaborar peca do PAJ com streaming SSE."""
    return EventSourceResponse(_stream_elaboracao(paj_norm))
