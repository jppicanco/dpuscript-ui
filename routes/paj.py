"""Rota de detalhe do PAJ."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.limpeza_service import limpar_anexos_paj
from services.paj_service import ler_paj

router = APIRouter()


@router.get("/paj/{paj_norm}", response_class=HTMLResponse)
async def paj_detail(request: Request, paj_norm: str):
    dados = ler_paj(paj_norm)
    if not dados:
        return HTMLResponse("<h1>PAJ nao encontrado</h1>", status_code=404)
    template = request.app.state.jinja.get_template("paj_detail.html")
    html = template.render(request=request, dados=dados, paj_norm=paj_norm)
    return HTMLResponse(html)


@router.get("/api/paj/{paj_norm}", response_class=JSONResponse)
async def api_paj(paj_norm: str):
    dados = ler_paj(paj_norm)
    if not dados:
        return JSONResponse({"erro": "PAJ nao encontrado"}, status_code=404)
    # Remove prompt_max do JSON (muito grande) — tem endpoint proprio
    dados_resumo = {k: v for k, v in dados.items() if k != "prompt_max"}
    return dados_resumo


@router.get("/api/paj/{paj_norm}/limpar-anexos/preview")
async def limpar_anexos_preview(paj_norm: str):
    """Dry-run: lista arquivos que SERIAM apagados + safeguards. Nada apaga."""
    return limpar_anexos_paj(paj_norm, dry_run=True)


@router.post("/api/paj/{paj_norm}/limpar-anexos/executar")
async def limpar_anexos_executar(paj_norm: str, forcar: bool = False):
    """Executa limpeza. `forcar=True` ignora safeguards (PAJ ativo, sem OCR)."""
    return limpar_anexos_paj(paj_norm, dry_run=False, forcar=forcar)
