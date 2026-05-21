"""Rota de feedback livre — JP escreve, Grok M4 extrai estrutura.

POST /api/feedback/<paj_norm>
  Body: mensagem (texto livre JP)
  Retorna: proposta JSON (classif_correta, razao, regex)

JP confirma com 1 clique → frontend chama /api/correcao/<paj_norm> com a proposta.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException

from services.feedback_parser_service import parsear_feedback

router = APIRouter(prefix="/api/feedback")

DPU_WORKSPACE = Path(r"E:\DPU\dpu-workspace")
ENTRADA = DPU_WORKSPACE / "Entrada" / "dpuscript"


def _normalizar_paj_para_pasta(paj_norm: str) -> str:
    """paj_norm UI = '2026-039-07596', já é o nome da pasta."""
    return paj_norm


def _ler_contexto_decisao(paj_norm: str) -> tuple[str, str]:
    """Lê metadata.classificacao + trecho da decisão mais recente."""
    pasta = ENTRADA / _normalizar_paj_para_pasta(paj_norm)
    meta_file = pasta / "metadata.json"
    if not meta_file.exists():
        return "?", ""
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        classif = meta.get("classificacao", "?")
    except Exception:
        return "?", ""

    # Trecho do acórdão/decisão mais recente
    contexto = ""
    for sub in ("peças", "pecas", "decisoes_superiores"):
        d = pasta / sub
        if not d.exists():
            continue
        arqs = sorted(
            [f for f in d.iterdir() if f.suffix == ".txt"],
            key=lambda f: f.name,
            reverse=True,
        )
        if arqs:
            try:
                texto = arqs[0].read_text(encoding="utf-8", errors="replace")
                contexto = texto[:2000]
                break
            except Exception:
                continue
    return classif, contexto


@router.post("/{paj_norm}")
async def feedback(paj_norm: str, mensagem: str = Form(...)):
    """Envia feedback livre pro Grok M4 e retorna proposta estruturada."""
    if not mensagem.strip():
        raise HTTPException(400, "mensagem vazia")
    paj_original = paj_norm.replace("-", "/", 1) if paj_norm.count("-") == 2 else paj_norm
    if paj_norm.count("-") == 2:
        partes = paj_norm.split("-", 2)
        paj_original = f"{partes[0]}/{partes[1]}-{partes[2]}"

    classif_atual, contexto = _ler_contexto_decisao(paj_norm)
    result = await parsear_feedback(
        paj=paj_original,
        classif_atual=classif_atual,
        mensagem_jp=mensagem,
        contexto_decisao=contexto,
    )
    return result
