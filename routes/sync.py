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
    cancel_token,
    health as sync_health,
    reconciliar_apenas,
)

router = APIRouter(prefix="/api/sync")


async def _stream(gen):
    async for line in gen:
        yield {"event": "log", "data": line.rstrip("\n")}
    yield {"event": "done", "data": "Sync finalizado"}


@router.get("/atualizar")
async def atualizar(token: str | None = None):
    """Roda pipeline no M4 + traz dados pro PC."""
    return EventSourceResponse(_stream(atualizar_agora(token=token)))


@router.get("/estado")
async def estado(token: str | None = None):
    """Sync rápido só do estado (sem rodar pipeline M4)."""
    return EventSourceResponse(_stream(atualizar_apenas_estado(token=token)))


@router.get("/baixar-do-m4")
async def baixar(token: str | None = None):
    """Sync rápido: rsync M4→PC sem rodar pipeline. ~30-60s.

    Pega o que o cron M4 (4x/dia) já preparou. Para uso diário.
    """
    return EventSourceResponse(_stream(baixar_do_m4(token=token)))


@router.get("/health")
async def health():
    """Estado de sync M4↔PC: idade do cron M4 + idade do estado local.

    Resposta cacheada por 60s (evita SSH a cada page load).
    """
    return sync_health()


@router.get("/reconciliar")
async def reconciliar(token: str | None = None):
    """Reconciliação rápida — só caixa SISDPU vs estado local (~30-60s).

    NÃO baixa peças, NÃO processa novos PAJs. Apenas:
    - Lê caixa SISDPU real
    - Move PAJs concluídos (que sumiram da caixa) pra arquivados
    - Atualiza estado

    Pro caso comum: JP concluiu PAJ no SISDPU e quer UI refletir já.
    """
    return EventSourceResponse(_stream(reconciliar_apenas(token=token)))


@router.post("/cancel/{token}")
async def cancel(token: str):
    """Mata subprocess rastreado sob `token`. Front-end gera token aleatório
    quando inicia sync e passa via query string ?token=...; aqui mata.
    """
    return cancel_token(token)
