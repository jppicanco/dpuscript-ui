"""Servico para executar o pipeline preparar_pajs.py como subprocess com streaming."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import queue
from typing import AsyncGenerator

from config import DPUSCRIPT_DIR

PYTHON_EXE = str(DPUSCRIPT_DIR / ".venv" / "Scripts" / "python.exe")
PREPARAR_SCRIPT = str(DPUSCRIPT_DIR / "preparar_pajs.py")


async def rodar_pipeline(
    only: str | None = None,
    dry_run: bool = False,
) -> AsyncGenerator[str, None]:
    """Roda preparar_pajs.py e faz yield de cada linha de output."""
    cmd = [PYTHON_EXE, PREPARAR_SCRIPT]
    if only:
        cmd.extend(["--only", only])
    if dry_run:
        cmd.append("--dry-run")

    yield f"[dpuscript-ui] Executando: {' '.join(cmd)}\n"

    q: queue.Queue[str | None] = queue.Queue()

    def _run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(DPUSCRIPT_DIR),
            )
            for line in iter(proc.stdout.readline, b""):
                q.put(line.decode("utf-8", errors="replace"))
            proc.wait()
            q.put(f"\n[dpuscript-ui] Pipeline finalizado (exit code {proc.returncode})\n")
        except FileNotFoundError:
            q.put(f"[ERRO] Python ou script nao encontrado: {PYTHON_EXE}\n")
        except Exception as e:
            q.put(f"[ERRO] {type(e).__name__}: {e}\n")
        finally:
            q.put(None)  # Sentinel

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        # Poll queue sem bloquear o event loop
        try:
            line = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.1)
            continue

        if line is None:
            break
        yield line
