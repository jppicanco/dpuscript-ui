"""Central de Atuação — rotas.

GET  /atuacao            — página (revisão noturna)
GET  /api/atuacao        — lista todos PAJs + artefato de atuação
GET  /api/atuacao/{paj}  — um PAJ
GET  /api/atuacao/resumo — contagens
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.atuacao_service import atuacao_paj, listar_atuacoes, resumo_batch

router = APIRouter()
api = APIRouter(prefix="/api/atuacao")


@router.get("/atuacao", response_class=HTMLResponse)
async def page_atuacao(request: Request):
    template = request.app.state.jinja.get_template("atuacao.html")
    return HTMLResponse(template.render(request=request))


@api.get("")
async def get_atuacoes():
    return {"atuacoes": listar_atuacoes(), "resumo": resumo_batch()}


@api.get("/resumo")
async def get_resumo():
    return resumo_batch()


@api.get("/{paj_norm}")
async def get_atuacao(paj_norm: str):
    a = atuacao_paj(paj_norm)
    if not a:
        return JSONResponse({"erro": "PAJ não encontrado"}, status_code=404)
    return a
