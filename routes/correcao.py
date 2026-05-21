"""Rotas de correção/aprendizado.

- POST /api/correcao/<paj>  — corrige classif + opcional regra geral
- GET  /api/correcao/regras — lista regras aprendidas
"""

from fastapi import APIRouter, Form, HTTPException

from services.correcao_service import corrigir_paj, listar_regras

router = APIRouter(prefix="/api/correcao")


@router.post("/{paj_norm}")
async def corrigir(
    paj_norm: str,
    classif_correta: str = Form(...),
    razao: str = Form(...),
    padrao_regex: str = Form(""),
    alvo: str = Form("blob_decisao"),
):
    """Corrige PAJ. paj_norm = '2026-039-07596'; convertido pra '2026/039-07596'."""
    paj_original = paj_norm.replace("-", "/", 1).replace("-", "-", 1)
    # Heurística: PAJ formato YYYY/UUU-NNNNN
    if paj_norm.count("-") == 2:
        partes = paj_norm.split("-", 2)
        paj_original = f"{partes[0]}/{partes[1]}-{partes[2]}"

    result = await corrigir_paj(
        paj=paj_original,
        classif_correta=classif_correta,
        razao=razao,
        padrao_regex=padrao_regex.strip() or None,
        alvo=alvo,
    )
    if not result["ok"]:
        raise HTTPException(500, detail=result.get("stderr") or "Erro desconhecido")
    return result


@router.get("/regras")
async def regras():
    """Lista regras aprendidas."""
    return {"regras": await listar_regras()}
