"""Rotas do catalogo de skills do dpu-workspace."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from services.skills_catalog import (
    invalidar_cache_skills,
    listar_grupos,
    listar_skills,
)

router = APIRouter()
api_router = APIRouter(prefix="/api/skills")


@router.get("/skills", response_class=HTMLResponse)
async def page_skills(request: Request):
    """Pagina HTML listando skills agrupadas."""
    template = request.app.state.jinja.get_template("skills.html")
    return HTMLResponse(template.render(request=request))


@api_router.get("")
async def get_skills(incluir_ocultas: bool = False):
    """Lista skills do workspace. Cache 60s + invalidacao por mtime."""
    return {
        "skills": listar_skills(incluir_ocultas=incluir_ocultas),
        "grupos": listar_grupos(),
    }


@api_router.post("/invalidar-cache")
async def invalidar_cache():
    """Forca releitura na proxima chamada. Util pos criar skill nova."""
    invalidar_cache_skills()
    return {"ok": True}
