"""Rotas de monitoramento do pipeline dpuscript (pagina /pipeline)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.pipeline_monitor_service import (
    ler_state,
    ler_log_atual,
    listar_runs,
    ler_run,
)

router = APIRouter()


@router.get("/pipeline", response_class=HTMLResponse)
async def pagina_pipeline(request: Request):
    template = request.app.state.jinja.get_template("pipeline.html")
    return HTMLResponse(template.render(request=request))


@router.get("/api/pipeline/state")
async def api_state():
    return ler_state()


@router.get("/api/pipeline/log")
async def api_log(max_linhas: int = 500):
    return ler_log_atual(max_linhas=max_linhas)


@router.get("/api/pipeline/runs")
async def api_runs(max_runs: int = 20):
    return {"runs": listar_runs(max_runs=max_runs)}


@router.get("/api/pipeline/runs/{nome}")
async def api_run(nome: str, max_linhas: int = 2000):
    return ler_run(nome, max_linhas=max_linhas)
