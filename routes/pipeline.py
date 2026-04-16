"""Rotas do pipeline — executar preparar_pajs.py com streaming de output."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from services.pipeline_service import rodar_pipeline

router = APIRouter(prefix="/api/pipeline")


async def _stream_pipeline(only: str | None = None):
    """Gera eventos SSE com cada linha do pipeline."""
    async for line in rodar_pipeline(only=only):
        yield {"event": "log", "data": line.rstrip("\n")}
    yield {"event": "done", "data": "Pipeline finalizado"}


@router.get("/run")
async def run_pipeline():
    """Roda o pipeline completo (todos os PAJs) com streaming SSE."""
    return EventSourceResponse(_stream_pipeline())


@router.get("/run/{paj}")
async def run_pipeline_paj(paj: str):
    """Roda o pipeline para um PAJ especifico com streaming SSE.

    paj vem normalizado (2018-039-17434), converte pra formato original (2018/039-17434).
    """
    paj_original = paj.replace("-", "/", 1)
    return EventSourceResponse(_stream_pipeline(only=paj_original))
