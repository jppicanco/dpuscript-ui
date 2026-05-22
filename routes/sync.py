"""Rotas de sincronização M4 → PC.

- /api/sync/atualizar — pipeline M4 + downstream sync (botão "Atualizar agora")
- /api/sync/estado — sync rápido só do estado
"""

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.sync_service import (
    atualizar_agora,
    atualizar_apenas_estado,
    baixar_do_m4,
    reconciliar_apenas,
)

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


@router.get("/baixar-do-m4")
async def baixar():
    """Sync rápido: rsync M4→PC sem rodar pipeline. ~30-60s.

    Pega o que o cron M4 (4x/dia) já preparou. Para uso diário.
    """
    return EventSourceResponse(_stream(baixar_do_m4()))


@router.get("/reconciliar")
async def reconciliar():
    """Reconciliação rápida — só caixa SISDPU vs estado local (~30-60s).

    NÃO baixa peças, NÃO processa novos PAJs. Apenas:
    - Lê caixa SISDPU real
    - Move PAJs concluídos (que sumiram da caixa) pra arquivados
    - Atualiza estado

    Pro caso comum: JP concluiu PAJ no SISDPU e quer UI refletir já.
    """
    return EventSourceResponse(_stream(reconciliar_apenas()))
