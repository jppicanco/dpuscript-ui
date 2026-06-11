"""Geracao DOCX/PDF de pecas via skill formatacao-docx do dpu-workspace.

Chama `formatar_peca.py` (skill _shared/formatacao-docx) como subprocess.
Output vai pra pasta do PAJ — definida via env FORMATAR_PECA_SAIDA_DIR.

Stream linha-a-linha via SSE pra UI ver progresso.
"""

from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import threading
from collections.abc import AsyncGenerator
from pathlib import Path

from config import ARQUIVADOS_DIR, DPUSCRIPT_DIR, ENTRADA_DIR, SKILLS_DIR


SCRIPT_FORMATAR = SKILLS_DIR / "_shared" / "formatacao-docx" / "formatar_peca.py"
import shutil as _shutil
_py_ws = (_shutil.which("python", path=str(DPUSCRIPT_DIR / ".venv" / "Scripts")) or
          _shutil.which("python", path=str(DPUSCRIPT_DIR / ".venv" / "bin")) or
          "python")
PYTHON_WORKSPACE = Path(_py_ws)

TIPOS_PECA_VALIDOS = ("agravo", "embargos", "memoriais", "despacho")


def _localizar_paj(paj_norm: str) -> Path | None:
    for base in (ENTRADA_DIR, ARQUIVADOS_DIR):
        p = base / paj_norm
        if p.exists() and p.is_dir():
            return p
    return None


def listar_txts_paj(paj_norm: str) -> list[dict]:
    """Lista .txt elaborados pelo Claude na raiz do PAJ (excluindo .txt OCR
    de pecas/decisoes_superiores que sao companions).
    """
    pasta = _localizar_paj(paj_norm)
    if not pasta:
        return []
    out: list[dict] = []
    for f in sorted(pasta.iterdir()):
        if f.is_file() and f.suffix.lower() == ".txt":
            out.append({
                "nome": f.name,
                "tamanho": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return out


async def gerar_docx(
    paj_norm: str,
    arquivo_txt: str,
    tipo_peca: str,
    tribunal: str | None = None,
    token: str | None = None,
) -> AsyncGenerator[str, None]:
    """Gera DOCX (+ PDF se LibreOffice instalado) a partir de .txt do PAJ.

    Output salvo na pasta do PAJ (via env FORMATAR_PECA_SAIDA_DIR).
    """
    pasta = _localizar_paj(paj_norm)
    if not pasta:
        yield f"[ERRO] PAJ {paj_norm} nao encontrado\n"
        return

    txt_path = pasta / arquivo_txt
    try:
        # Path traversal guard
        txt_path.resolve().relative_to(pasta.resolve())
    except ValueError:
        yield f"[ERRO] arquivo invalido: {arquivo_txt}\n"
        return
    if not txt_path.exists():
        yield f"[ERRO] {arquivo_txt} nao existe em {pasta}\n"
        return

    if tipo_peca not in TIPOS_PECA_VALIDOS:
        yield f"[ERRO] tipo-peca invalido: {tipo_peca}. Use: {', '.join(TIPOS_PECA_VALIDOS)}\n"
        return

    if not SCRIPT_FORMATAR.exists():
        yield f"[ERRO] script nao encontrado: {SCRIPT_FORMATAR}\n"
        return
    if not PYTHON_WORKSPACE.exists():
        yield f"[ERRO] python venv nao encontrado: {PYTHON_WORKSPACE}\n"
        return

    cmd = [
        str(PYTHON_WORKSPACE),
        "-X", "utf8",
        str(SCRIPT_FORMATAR),
        "--entrada", str(txt_path),
        "--tipo-peca", tipo_peca,
        "--paj", paj_norm,
    ]
    if tribunal:
        cmd.extend(["--tribunal", tribunal])

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["FORMATAR_PECA_SAIDA_DIR"] = str(pasta)

    yield f"[docgen] gerando {tipo_peca} a partir de {arquivo_txt}\n"
    yield f"[docgen] saida: {pasta}\n\n"

    q: queue.Queue[str | None] = queue.Queue()

    def _run():
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
            )
            # Registra no _ACTIVE_PROCS do sync_service pra suportar cancel
            try:
                from services import sync_service
                if token:
                    sync_service._ACTIVE_PROCS[token] = proc
            except Exception:
                pass
            for line in iter(proc.stdout.readline, b""):
                try:
                    txt = line.decode("utf-8")
                except UnicodeDecodeError:
                    txt = line.decode("cp1252", errors="replace")
                q.put(txt)
            proc.wait()
            q.put(f"[docgen] exit code {proc.returncode}\n")
        except Exception as e:
            q.put(f"[docgen] ERRO: {type(e).__name__}: {e}\n")
        finally:
            try:
                from services import sync_service
                if token:
                    sync_service._ACTIVE_PROCS.pop(token, None)
            except Exception:
                pass
            q.put(None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    while True:
        try:
            line = await asyncio.to_thread(q.get, True, 1.0)
        except queue.Empty:
            await asyncio.sleep(0.1)
            continue
        if line is None:
            break
        yield line

    # Lista DOCX/PDF gerados
    artefatos = [
        f for f in pasta.iterdir()
        if f.is_file() and f.suffix.lower() in (".docx", ".pdf") and f.stat().st_mtime > (
            txt_path.stat().st_mtime - 60
        )
    ]
    if artefatos:
        yield "\n[docgen] arquivos gerados:\n"
        for a in sorted(artefatos, key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            yield f"  - {a.name} ({a.stat().st_size // 1024} KB)\n"

    yield "\n=== DOCGEN CONCLUIDO ===\n"
