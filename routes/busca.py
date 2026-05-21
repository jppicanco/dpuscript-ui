"""Rota de busca textual em PAJs locais."""

from fastapi import APIRouter, Query

from services.busca_service import buscar, invalidar_cache

router = APIRouter(prefix="/api/busca")


@router.get("")
async def buscar_endpoint(q: str = Query("", min_length=0), limite: int = 50):
    """GET /api/busca?q=<termo>&limite=50"""
    if not q.strip():
        return {"q": q, "total": 0, "resultados": []}
    resultados = buscar(q, limite=limite)
    return {"q": q, "total": len(resultados), "resultados": resultados}


@router.post("/invalidar-cache")
async def invalidar():
    """Força re-leitura do disco na próxima busca."""
    invalidar_cache()
    return {"ok": True}
