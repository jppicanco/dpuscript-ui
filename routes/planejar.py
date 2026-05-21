"""Rotas de planejamento pré-elaboração.

POST /api/elaborar/planejar/<paj_norm>      — gera plano via Claude CLI
POST /api/elaborar/aprovar-plano/<paj_norm> — salva plano aprovado
GET  /api/elaborar/plano/<paj_norm>         — lê plano salvo (se houver)
"""

from fastapi import APIRouter, HTTPException, Request

from services.planejar_service import (
    carregar_plano,
    planejar_elaboracao,
    salvar_plano,
)

router = APIRouter(prefix="/api/elaborar")


@router.post("/planejar/{paj_norm}")
async def planejar(paj_norm: str, request: Request):
    """Gera plano via Claude CLI. Pode demorar ~30-60s.

    Body opcional: {"feedback": "instrução adicional pra refazer com observação"}
    """
    feedback = ""
    try:
        body = await request.json()
        feedback = (body or {}).get("feedback", "") or ""
    except Exception:
        pass
    return await planejar_elaboracao(paj_norm, feedback_jp=feedback)


@router.post("/aprovar-plano/{paj_norm}")
async def aprovar(paj_norm: str, request: Request):
    """Recebe plano (possivelmente editado pelo JP) e salva como aprovado."""
    body = await request.json()
    plano = body.get("plano")
    if not plano or not isinstance(plano, dict):
        raise HTTPException(400, "plano inválido")
    f = salvar_plano(paj_norm, plano, fonte=body.get("fonte", "jp"))
    return {"ok": True, "arquivo": str(f)}


@router.get("/plano/{paj_norm}")
async def ler_plano(paj_norm: str):
    payload = carregar_plano(paj_norm)
    if not payload:
        return {"ok": False, "plano": None}
    return {"ok": True, **payload}
