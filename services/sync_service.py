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
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

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


_HEALTH_CACHE: dict[str, object] = {"ts": 0.0, "data": None}
_HEALTH_CACHE_TTL = 60.0
_M4_LOG = "/Users/macmini/jarbas/data/logs/preparar-pajs.log"


def _classify_age(seconds: float | None) -> str:
    """Classifica idade em ok/warning/error.

    Cron roda 4x/dia, intervalos máx ~6h (21:00 → 08:15 = 11h15 noturno).
    - ok: <= 12h
    - warning: 12h-26h (perdeu 1 ciclo)
    - error: > 26h ou indisponível
    """
    if seconds is None:
        return "error"
    if seconds <= 12 * 3600:
        return "ok"
    if seconds <= 26 * 3600:
        return "warning"
    return "error"


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "indisponível"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}min"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f}h"
    d = seconds / 86400
    return f"{d:.1f}d"


def _m4_last_cron_run() -> dict[str, object]:
    """Pega timestamp do último 'FIM:' marker no log M4 via SSH (timeout 4s)."""
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=3",
        "-o", "BatchMode=yes",
        M4_HOST,
        f"stat -f '%m' {_M4_LOG} 2>/dev/null && grep -E 'FIM: ' {_M4_LOG} | tail -1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=5)
        out = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        if not out:
            return {"reachable": False, "error": proc.stderr.decode("utf-8", errors="replace")[:200]}
        mtime_epoch = float(out[0])
        last_fim = out[1] if len(out) > 1 else ""
        return {
            "reachable": True,
            "log_mtime_epoch": mtime_epoch,
            "last_fim_line": last_fim,
        }
    except subprocess.TimeoutExpired:
        return {"reachable": False, "error": "ssh timeout"}
    except Exception as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"}


def health() -> dict[str, object]:
    """Estado de sync M4↔PC. Cache 60s pra não SSH a cada page load."""
    now = time.time()
    cached = _HEALTH_CACHE.get("data")
    if cached and now - float(_HEALTH_CACHE["ts"]) < _HEALTH_CACHE_TTL:
        return cached  # type: ignore[return-value]

    pc_state_file = os.path.join(_pc_state_dir(), "pajs_processados.json")
    pc_mtime = os.path.getmtime(pc_state_file) if os.path.exists(pc_state_file) else None
    pc_age = (now - pc_mtime) if pc_mtime else None

    m4 = _m4_last_cron_run()
    m4_mtime = m4.get("log_mtime_epoch") if m4.get("reachable") else None
    m4_age = (now - float(m4_mtime)) if m4_mtime else None

    pc_status = _classify_age(pc_age)
    m4_status = _classify_age(m4_age) if m4.get("reachable") else "error"
    overall = "error" if "error" in (pc_status, m4_status) else (
        "warning" if "warning" in (pc_status, m4_status) else "ok"
    )

    data: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pc": {
            "file": pc_state_file,
            "mtime_epoch": pc_mtime,
            "age_seconds": pc_age,
            "age_human": _fmt_age(pc_age),
            "status": pc_status,
        },
        "m4": {
            "reachable": m4.get("reachable", False),
            "log_mtime_epoch": m4_mtime,
            "age_seconds": m4_age,
            "age_human": _fmt_age(m4_age),
            "last_fim_line": m4.get("last_fim_line", ""),
            "error": m4.get("error", ""),
            "status": m4_status,
        },
        "status": overall,
    }
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["data"] = data
    return data
