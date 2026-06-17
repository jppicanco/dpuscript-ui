"""Reautenticação do Claude CLI no M4 pela UI.

Fluxo: POST /api/auth/m4/iniciar -> {url} ; JP autoriza no navegador ;
POST /api/auth/m4/codigo {codigo} -> {ok}. GET /api/auth/m4/status -> testa.
"""
import asyncio

from fastapi import APIRouter, Request

from services.claude_auth_service import (
    disponivel,
    enviar_codigo,
    iniciar_setup_token,
)

api = APIRouter(prefix="/api/auth/m4")


@api.get("/status")
async def status():
    """Testa se o Claude CLI está autenticado (chamada curta)."""
    import shutil

    if not shutil.which("claude"):
        return {"ok": False, "autenticado": False, "msg": "claude CLI não encontrado"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "responda: ok",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        txt = (out or b"").decode(errors="replace")
        autenticado = "401" not in txt and "authenticate" not in txt.lower()
        return {"ok": True, "autenticado": autenticado, "msg": txt.strip()[:200]}
    except Exception as e:
        return {"ok": False, "autenticado": False, "msg": str(e)}


@api.post("/iniciar")
async def iniciar():
    """Abre o setup-token e devolve a URL de OAuth (rodar em thread — usa pty)."""
    if not disponivel():
        return {"ok": False, "erro": "Reautenticação só disponível no M4."}
    return await asyncio.to_thread(iniciar_setup_token)


@api.post("/codigo")
async def codigo(request: Request):
    body = await request.json()
    cod = (body or {}).get("codigo", "")
    return await asyncio.to_thread(enviar_codigo, cod)
