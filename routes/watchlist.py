"""Rotas da watchlist de transito em julgado."""

import asyncio
import subprocess
import sys

from fastapi import APIRouter, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

from config import DPUSCRIPT_DIR
from services import watchlist_service

router = APIRouter()


@router.get("/watchlist", response_class=HTMLResponse)
async def pagina(request: Request):
    template = request.app.state.jinja.get_template("watchlist.html")
    return HTMLResponse(template.render(request=request))


@router.get("/api/watchlist")
async def api_listar():
    return {
        "itens": watchlist_service.listar(),
        "stats": watchlist_service.stats(),
    }


@router.get("/api/watchlist/stats")
async def api_stats():
    return watchlist_service.stats()


@router.post("/api/watchlist/add")
async def api_adicionar(payload: dict = Body(...)):
    paj = (payload.get("paj") or "").strip()
    cnj = (payload.get("cnj") or "").strip()
    if not paj:
        return JSONResponse({"erro": "paj obrigatorio"}, status_code=400)
    freq = int(payload.get("frequencia_dias", 15))
    motivo = payload.get("motivo", "arquivamento_por_vitoria")
    expectativa = payload.get("expectativa", "")
    item = watchlist_service.adicionar(
        paj=paj, cnj=cnj, motivo=motivo,
        frequencia_dias=freq, expectativa=expectativa,
    )
    return {"paj": paj, "item": item}


@router.post("/api/watchlist/remove/{paj:path}")
async def api_remover(paj: str):
    ok = watchlist_service.remover(paj)
    return {"removido": ok}


@router.post("/api/watchlist/verify-now")
async def api_verificar_agora():
    """Dispara o monitor_transito.py em background (forca verificacao de todos)."""
    python_exe = str(DPUSCRIPT_DIR / ".venv" / "Scripts" / "python.exe")
    script = str(DPUSCRIPT_DIR / "monitor_transito.py")
    try:
        subprocess.Popen(
            [python_exe, script, "--force"],
            cwd=str(DPUSCRIPT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"started": True}
    except Exception as e:
        return JSONResponse({"erro": str(e)}, status_code=500)
