"""Serviço de sincronização M4 → PC.

Pipeline canônico roda 24/7 no M4 (cron 08h15). Esta UI lê dados locais
do PC, espelhados periodicamente ou sob demanda via "Atualizar agora".

Fluxo do botão "Atualizar agora":
1. SSH M4: chama preparar_pajs.py (gera/atualiza pastas + estado no M4)
2. scp/rsync downstream: M4:/Users/macmini/dpu-workspace/ → PC:/E:/DPU/dpu-workspace/
3. UI relê pastas locais

Stream eventos via SSE pra UI mostrar progresso.
"""

from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import threading
from collections.abc import AsyncGenerator

M4_HOST = "macmini@192.168.0.102"
M4_PIPELINE_CWD = "/Users/macmini/dpu-workspace/dpuscript"
M4_PYTHON = "/Users/macmini/jarbas/venv-dpu/bin/python"
M4_DATA_DIR = "/Users/macmini/dpu-workspace/Entrada/dpuscript"
M4_STATE_DIR = "/Users/macmini/dpu-workspace/dpuscript/estado"


def _pc_data_dir() -> str:
    """Pasta local PC pra onde fazer sync."""
    # Usa config.DPUSCRIPT_DIR? Pra começar, hardcode.
    return r"E:\DPU\dpu-workspace\Entrada\dpuscript"


def _pc_state_dir() -> str:
    return r"E:\DPU\dpu-workspace\dpuscript\estado"


async def _run_subproc_streaming(
    cmd: list[str], label: str = ""
) -> AsyncGenerator[str, None]:
    """Roda subprocess e faz yield de cada linha do stdout/stderr."""
    if label:
        yield f"[{label}] iniciando: {' '.join(cmd[:3])}...\n"

    q: queue.Queue[str | None] = queue.Queue()

    def _run():
        try:
            # PYTHONIOENCODING força subprocess Python a emitir UTF-8
            # mesmo em Windows (que por default usa cp1252)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            for line in iter(proc.stdout.readline, b""):
                # Tenta UTF-8 primeiro, fallback CP1252 (Windows default)
                try:
                    txt = line.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        txt = line.decode("cp1252")
                    except UnicodeDecodeError:
                        txt = line.decode("utf-8", errors="replace")
                q.put(txt)
            proc.wait()
            q.put(f"[{label}] exit code {proc.returncode}\n")
        except Exception as e:
            q.put(f"[{label}] ERRO: {type(e).__name__}: {e}\n")
        finally:
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


async def atualizar_agora() -> AsyncGenerator[str, None]:
    """Roda pipeline no M4 + scp downstream + sinaliza fim."""
    yield "Iniciando atualização — pipeline roda no M4 + sync downstream\n"

    # 1. SSH M4 chama pipeline (sem --full-prompt-max — modo lazy)
    cmd_ssh = [
        "ssh",
        M4_HOST,
        f"cd {M4_PIPELINE_CWD} && {M4_PYTHON} preparar_pajs.py",
    ]
    yield "\n=== FASE 1: pipeline no M4 ===\n"
    async for linha in _run_subproc_streaming(cmd_ssh, label="M4"):
        yield linha

    # 2. Sync estado (pequeno, rápido)
    yield "\n=== FASE 2: sync estado M4 → PC ===\n"
    os.makedirs(_pc_state_dir(), exist_ok=True)
    cmd_scp_estado = [
        "scp",
        f"{M4_HOST}:{M4_STATE_DIR}/pajs_processados.json",
        os.path.join(_pc_state_dir(), "pajs_processados.json"),
    ]
    async for linha in _run_subproc_streaming(cmd_scp_estado, label="scp-estado"):
        yield linha

    # 3. Sync pastas dos PAJs (pode ser grande — usar tar+ssh pra eficiência)
    yield "\n=== FASE 3: sync pastas PAJ M4 → PC ===\n"
    os.makedirs(_pc_data_dir(), exist_ok=True)
    # tar via ssh pra um arquivo temp, depois extrai no PC
    tmp_tar = r"E:\DPU\dpu-workspace\Entrada\.sync_m4.tar.gz"
    cmd_tar = [
        "ssh",
        M4_HOST,
        f"cd {M4_DATA_DIR}/.. && tar -czf - dpuscript/",
    ]
    yield f"[sync] baixando arquivo {tmp_tar}\n"
    with open(tmp_tar, "wb") as f:
        proc = subprocess.run(cmd_tar, stdout=f, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        yield f"[sync] ERRO tar ssh: {proc.stderr.decode('utf-8', errors='replace')}\n"
        return
    yield f"[sync] tar baixado ({os.path.getsize(tmp_tar)} bytes), extraindo...\n"
    cmd_extract = [
        "tar",
        "-xzf",
        tmp_tar,
        "-C",
        r"E:\DPU\dpu-workspace\Entrada",
    ]
    async for linha in _run_subproc_streaming(cmd_extract, label="extract"):
        yield linha
    try:
        os.remove(tmp_tar)
    except Exception:
        pass

    yield "\n=== ATUALIZAÇÃO CONCLUÍDA ===\n"


async def atualizar_apenas_estado() -> AsyncGenerator[str, None]:
    """Sync rápido — só estado/pajs_processados.json (sem rodar pipeline M4).

    Pro caso de JP já sabe que o cron rodou e só quer ver o estado novo.
    """
    yield "Sync rápido do estado (sem rodar pipeline)\n"
    os.makedirs(_pc_state_dir(), exist_ok=True)
    cmd = [
        "scp",
        f"{M4_HOST}:{M4_STATE_DIR}/pajs_processados.json",
        os.path.join(_pc_state_dir(), "pajs_processados.json"),
    ]
    async for linha in _run_subproc_streaming(cmd, label="scp"):
        yield linha
    yield "Estado atualizado.\n"


async def baixar_do_m4() -> AsyncGenerator[str, None]:
    """Sync rápido: rsync M4→PC SEM rodar pipeline.

    Pega o estado + pastas que M4 já tem (mantido fresco pelo cron 4x/dia).
    USE QUANDO: rotina diária. Cron M4 já fez trabalho pesado, JP só quer
    ver dados frescos.

    Diferente de "atualizar_agora" (que faz SSH + roda pipeline + sync).
    Esse só faz sync.
    """
    yield "Sync M4 → PC (sem rodar pipeline — pega o que M4 já tem)\n\n"

    # 1. Estado
    yield "[1/2] Estado pajs_processados.json\n"
    os.makedirs(_pc_state_dir(), exist_ok=True)
    cmd_estado = [
        "scp",
        f"{M4_HOST}:{M4_STATE_DIR}/pajs_processados.json",
        os.path.join(_pc_state_dir(), "pajs_processados.json"),
    ]
    async for linha in _run_subproc_streaming(cmd_estado, label="scp-estado"):
        yield linha

    # 2. Pastas dos PAJs (tar+ssh pra eficiência)
    yield "\n[2/2] Pastas dos PAJs (Entrada/dpuscript/)\n"
    os.makedirs(_pc_data_dir(), exist_ok=True)
    tmp_tar = r"E:\DPU\dpu-workspace\Entrada\.sync_m4.tar.gz"
    cmd_tar = ["ssh", M4_HOST, f"cd {M4_DATA_DIR}/.. && tar -czf - dpuscript/"]
    yield f"[sync] baixando tarball\n"
    proc = subprocess.run(cmd_tar, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        yield f"[sync] ERRO tar: {proc.stderr.decode('utf-8', errors='replace')}\n"
        return
    with open(tmp_tar, "wb") as f:
        f.write(proc.stdout)
    yield f"[sync] tarball baixado ({os.path.getsize(tmp_tar)/1024:.0f} KB), extraindo...\n"
    cmd_extract = ["tar", "-xzf", tmp_tar, "-C", r"E:\DPU\dpu-workspace\Entrada"]
    async for linha in _run_subproc_streaming(cmd_extract, label="extract"):
        yield linha
    try:
        os.remove(tmp_tar)
    except Exception:
        pass

    yield "\n=== SYNC CONCLUÍDO (sem rodar pipeline) ===\n"


async def reconciliar_apenas() -> AsyncGenerator[str, None]:
    """Reconciliação rápida — só compara caixa SISDPU real vs estado local
    e move PAJs concluídos pra dpuscript_arquivados/. NÃO baixa peças nem
    processa novos PAJs. Rápido: ~30-60s.

    Roda localmente (pipeline preparar_pajs.py --reconciliar-apenas) porque
    é só leitura + mover pastas locais. Não precisa SSH M4.
    """
    yield "Reconciliação rápida — comparando caixa SISDPU vs estado local\n"
    yield "(NÃO baixa peças. Só move PAJs concluídos pra arquivados.)\n\n"
    # IMPORTANTE: usar venv do dpu-workspace (tem fitz/playwright/etc),
    # não o do dpuscript-ui (sem essas deps).
    python_dpu_workspace = r"E:\DPU\dpu-workspace\dpuscript\.venv\Scripts\python.exe"
    cmd = [
        python_dpu_workspace,
        "-X", "utf8",
        r"E:\DPU\dpu-workspace\dpuscript\preparar_pajs.py",
        "--reconciliar-apenas",
    ]
    async for linha in _run_subproc_streaming(cmd, label="reconcilia"):
        yield linha
    yield "\n=== RECONCILIAÇÃO CONCLUÍDA ===\n"
