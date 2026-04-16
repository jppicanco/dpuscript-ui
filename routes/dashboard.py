"""Rota do dashboard — lista PAJs da caixa de entrada."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import ESTADO_FILE
from services.paj_service import listar_pajs

import json

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = request.app.state.jinja.get_template("dashboard.html")
    html = template.render(request=request)
    return HTMLResponse(html)


@router.get("/api/pajs", response_class=JSONResponse)
async def api_pajs():
    pajs = listar_pajs()
    ultima_execucao = ""
    if ESTADO_FILE.exists():
        try:
            estado = json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
            ultima_execucao = estado.get("ultima_execucao", "")
        except Exception:
            pass
    return {"pajs": pajs, "ultima_execucao": ultima_execucao}
