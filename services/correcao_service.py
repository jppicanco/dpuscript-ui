"""Serviço de correção/aprendizado de classificações.

JP corrige classificação errada de um PAJ. Sistema:
1. Atualiza metadata.json do PAJ
2. Se JP forneceu regex, cria regra geral no memory de aprendizado
3. Próximos PAJs similares passam a classificar corretamente

Backend wrapper sobre dpuscript/memory/corrigir.py (no dpu-workspace).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

DPU_WORKSPACE = Path(r"E:\DPU\dpu-workspace")
CORRIGIR_SCRIPT = DPU_WORKSPACE / "dpuscript" / "memory" / "corrigir.py"
PYTHON_EXE = sys.executable  # mesmo Python do UI


async def corrigir_paj(
    paj: str,
    classif_correta: str,
    razao: str,
    padrao_regex: str | None = None,
    alvo: str = "blob_decisao",
) -> dict:
    """Corrige classif do PAJ + opcionalmente cria regra geral.

    Returns: {"ok": bool, "stdout": str, "stderr": str, "regra_criada": bool}
    """
    cmd = [PYTHON_EXE, "-X", "utf8", str(CORRIGIR_SCRIPT), paj, classif_correta, razao]
    if padrao_regex:
        cmd.extend(["--padrao", padrao_regex, "--alvo", alvo])

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(DPU_WORKSPACE),
            timeout=30,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "regra_criada": bool(padrao_regex) and proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "Timeout", "regra_criada": False}
    except Exception as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"{type(e).__name__}: {e}",
            "regra_criada": False,
        }


async def listar_regras() -> list[dict]:
    """Lista regras aprendidas atuais."""
    cmd = [PYTHON_EXE, "-X", "utf8", str(CORRIGIR_SCRIPT), "--listar"]
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(DPU_WORKSPACE),
            timeout=15,
        )
        # Parser simples — formato do --listar é texto plano
        return _parse_listar_output(proc.stdout)
    except Exception:
        return []


def _parse_listar_output(s: str) -> list[dict]:
    """Parse formato saída do --listar pra lista de dicts."""
    regras = []
    cur = None
    for linha in s.splitlines():
        ln = linha.strip()
        if not ln:
            continue
        if ln.startswith("[") and "]" in ln:
            if cur:
                regras.append(cur)
            partes = ln.split("]", 1)
            id_ = partes[0][1:]
            resto = partes[1].strip()
            cur = {"id": id_, "info": resto, "ativa": "ativa" in resto, "lines": []}
        elif cur is not None:
            cur["lines"].append(ln)
    if cur:
        regras.append(cur)
    return regras
