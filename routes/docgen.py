"""Rotas pra gerar DOCX/PDF de uma peca a partir de .txt do PAJ."""

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.docgen_service import TIPOS_PECA_VALIDOS, gerar_docx, listar_txts_paj


router = APIRouter(prefix="/api/docgen")


async def _stream(gen):
    async for line in gen:
        yield {"event": "log", "data": line.rstrip("\n")}
    yield {"event": "done", "data": "Docgen finalizado"}


@router.get("/{paj_norm}/txts")
async def listar_txts(paj_norm: str):
    """Lista .txt na raiz da pasta do PAJ (candidatos pra formatacao DOCX)."""
    return {
        "txts": listar_txts_paj(paj_norm),
        "tipos_peca": list(TIPOS_PECA_VALIDOS),
    }


@router.get("/{paj_norm}/gerar")
async def gerar(
    paj_norm: str,
    arquivo: str,
    tipo_peca: str,
    tribunal: str | None = None,
    token: str | None = None,
):
    """Gera DOCX (+ PDF) a partir de arquivo .txt do PAJ. Stream SSE."""
    return EventSourceResponse(_stream(gerar_docx(
        paj_norm=paj_norm,
        arquivo_txt=arquivo,
        tipo_peca=tipo_peca,
        tribunal=tribunal,
        token=token,
    )))
