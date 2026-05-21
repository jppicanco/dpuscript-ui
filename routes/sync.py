"""Rotas de sincronização M4 → PC.

- /api/sync/atualizar — pipeline M4 + downstream sync (botão "Atualizar agora")
- /api/sync/estado — sync rápido só do estado
"""

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.sync_service import atualizar_agora, atualizar_apenas_estado

router = APIRouter(prefix="/api/sync")


async def _stream(gen):
    async for line in gen:
        yield {"event": "log", "data": line.rstrip("\n")}
    yield {"event": "done", "data": "Sync finalizado"}


@router.get("/atualizar")
async def atualizar():
    """Roda pipeline no M4 + traz dados pro PC."""
    return EventSourceResponse(_stream(atualizar_agora()))


@router.get("/estado")
async def estado():
    """Sync rápido só do estado (sem rodar pipeline M4)."""
    return EventSourceResponse(_stream(atualizar_apenas_estado()))
