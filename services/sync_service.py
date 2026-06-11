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
from pathlib import Path
import queue
import subprocess
import threading
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
import platform
import sys

# True quando rodando direto no M4 (workspace já é local — sem SSH/scp)
IS_M4 = platform.system() == "Darwin" or bool(os.getenv("DPU_IS_M4"))

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


_ACTIVE_PROCS: dict[str, subprocess.Popen] = {}
_CANCELLED_TOKENS: set[str] = set()


def cancel_token(token: str) -> dict:
    """Marca token cancelado + mata subprocesso atual (se houver).

    Generator do sync verifica `_CANCELLED_TOKENS` entre fases pra abortar.
    """
    _CANCELLED_TOKENS.add(token)
    proc = _ACTIVE_PROCS.get(token)
    if not proc:
        return {"ok": True, "killed": False, "reason": "nenhum subprocess ativo (token marcado cancelado)"}
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        return {"ok": True, "killed": True, "pid": proc.pid}
    finally:
        _ACTIVE_PROCS.pop(token, None)


def _is_cancelled(token: str | None) -> bool:
    return bool(token) and token in _CANCELLED_TOKENS


def _release_token(token: str | None) -> None:
    """Limpa token de _CANCELLED_TOKENS no fim de cada generator."""
    if token:
        _CANCELLED_TOKENS.discard(token)


async def _run_subproc_streaming(
    cmd: list[str], label: str = "", token: str | None = None
) -> AsyncGenerator[str, None]:
    """Roda subprocess e faz yield de cada linha do stdout/stderr.

    Se `token` fornecido, registra Popen em `_ACTIVE_PROCS[token]` pra suportar
    cancelamento via `cancel_token(token)`.
    """
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
            if token:
                _ACTIVE_PROCS[token] = proc
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
            if token:
                _ACTIVE_PROCS.pop(token, None)
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


async def atualizar_agora(token: str | None = None) -> AsyncGenerator[str, None]:
    """Roda pipeline + decisão Grok e sinaliza fim.

    No M4: executa preparar_pajs.py + decidir_grok.py --prod localmente.
    No Windows: SSH M4 pipeline + scp downstream (comportamento original).
    """
    if IS_M4:
        yield "=== FASE 1: baixando PAJs do e-Proc/SISDPU ===\n"
        cmd_prep = [sys.executable, "-X", "utf8",
                    str(Path(M4_PIPELINE_CWD) / "preparar_pajs.py")]
        async for linha in _run_subproc_streaming(cmd_prep, label="preparar", token=token):
            yield linha
        if _is_cancelled(token):
            yield "\n=== CANCELADO ===\n"; _release_token(token); return

        yield "\n=== FASE 2: analisando novos PAJs (Grok) ===\n"
        cmd_grok = [sys.executable, "-X", "utf8",
                    str(Path(M4_PIPELINE_CWD) / "decidir_grok.py"), "--prod"]
        async for linha in _run_subproc_streaming(cmd_grok, label="grok", token=token):
            yield linha
        _release_token(token)
        yield "\n=== ATUALIZAÇÃO CONCLUÍDA ===\n"
        return

    # --- Windows: comportamento original ---
    yield "Iniciando atualização — pipeline roda no M4 + sync downstream\n"
    cmd_ssh = ["ssh", M4_HOST, f"cd {M4_PIPELINE_CWD} && {M4_PYTHON} preparar_pajs.py"]
    yield "\n=== FASE 1: pipeline no M4 ===\n"
    async for linha in _run_subproc_streaming(cmd_ssh, label="M4", token=token):
        yield linha
    if _is_cancelled(token):
        yield "\n=== CANCELADO PELO USUÁRIO (fase 1) ===\n"; _release_token(token); return

    yield "\n=== FASE 2: sync estado M4 → PC ===\n"
    os.makedirs(_pc_state_dir(), exist_ok=True)
    cmd_scp_estado = ["scp", f"{M4_HOST}:{M4_STATE_DIR}/pajs_processados.json",
                      os.path.join(_pc_state_dir(), "pajs_processados.json")]
    async for linha in _run_subproc_streaming(cmd_scp_estado, label="scp-estado", token=token):
        yield linha
    if _is_cancelled(token):
        yield "\n=== CANCELADO PELO USUÁRIO (fase 2) ===\n"; _release_token(token); return

    yield "\n=== FASE 3: sync pastas PAJ M4 → PC ===\n"
    os.makedirs(_pc_data_dir(), exist_ok=True)
    tmp_tar = r"E:\DPU\dpu-workspace\Entrada\.sync_m4.tar.gz"
    cmd_tar = ["ssh", M4_HOST, f"cd {M4_DATA_DIR}/.. && tar -czf - dpuscript/"]
    yield "[sync] baixando tarball\n"
    with open(tmp_tar, "wb") as f:
        proc = subprocess.Popen(cmd_tar, stdout=f, stderr=subprocess.PIPE)
        if token:
            _ACTIVE_PROCS[token] = proc
        try:
            _, err = proc.communicate()
        finally:
            if token:
                _ACTIVE_PROCS.pop(token, None)
    if proc.returncode != 0:
        yield f"[sync] ERRO tar ssh: {err.decode('utf-8', errors='replace')}\n"; return
    yield f"[sync] tar baixado ({os.path.getsize(tmp_tar)} bytes), extraindo...\n"
    async for linha in _run_subproc_streaming(
            ["tar", "-xzf", tmp_tar, "-C", r"E:\DPU\dpu-workspace\Entrada"],
            label="extract", token=token):
        yield linha
    try:
        os.remove(tmp_tar)
    except Exception:
        pass
    _release_token(token)
    yield "\n=== ATUALIZAÇÃO CONCLUÍDA ===\n"


async def atualizar_apenas_estado(token: str | None = None) -> AsyncGenerator[str, None]:
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
    async for linha in _run_subproc_streaming(cmd, label="scp", token=token):
        yield linha
    _release_token(token)
    yield "Estado atualizado.\n"


async def baixar_do_m4(token: str | None = None) -> AsyncGenerator[str, None]:
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
    async for linha in _run_subproc_streaming(cmd_estado, label="scp-estado", token=token):
        yield linha
    if _is_cancelled(token):
        yield "\n=== CANCELADO PELO USUÁRIO ===\n"
        _release_token(token)
        return

    # 2. Pastas dos PAJs (tar+ssh pra eficiência)
    yield "\n[2/2] Pastas dos PAJs (Entrada/dpuscript/)\n"
    os.makedirs(_pc_data_dir(), exist_ok=True)
    tmp_tar = r"E:\DPU\dpu-workspace\Entrada\.sync_m4.tar.gz"
    cmd_tar = ["ssh", M4_HOST, f"cd {M4_DATA_DIR}/.. && tar -czf - dpuscript/"]
    yield f"[sync] baixando tarball\n"
    proc = subprocess.Popen(cmd_tar, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if token:
        _ACTIVE_PROCS[token] = proc
    try:
        stdout_bytes, err = proc.communicate()
    finally:
        if token:
            _ACTIVE_PROCS.pop(token, None)
    if proc.returncode != 0:
        yield f"[sync] ERRO tar: {err.decode('utf-8', errors='replace')}\n"
        return
    with open(tmp_tar, "wb") as f:
        f.write(stdout_bytes)
    yield f"[sync] tarball baixado ({os.path.getsize(tmp_tar)/1024:.0f} KB), extraindo...\n"
    cmd_extract = ["tar", "-xzf", tmp_tar, "-C", r"E:\DPU\dpu-workspace\Entrada"]
    async for linha in _run_subproc_streaming(cmd_extract, label="extract", token=token):
        yield linha
    try:
        os.remove(tmp_tar)
    except Exception:
        pass

    _release_token(token)
    yield "\n=== SYNC CONCLUÍDO (sem rodar pipeline) ===\n"


async def reconciliar_apenas(token: str | None = None) -> AsyncGenerator[str, None]:
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
    if IS_M4:
        cmd = [sys.executable, "-X", "utf8",
               str(Path(M4_PIPELINE_CWD) / "preparar_pajs.py"), "--reconciliar-apenas"]
    else:
        python_dpu_workspace = r"E:\DPU\dpu-workspace\dpuscript\.venv\Scripts\python.exe"
        cmd = [python_dpu_workspace, "-X", "utf8",
               r"E:\DPU\dpu-workspace\dpuscript\preparar_pajs.py", "--reconciliar-apenas"]
    async for linha in _run_subproc_streaming(cmd, label="reconcilia", token=token):
        yield linha
    _release_token(token)
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
    """Pega timestamp do último cron run.

    No M4: lê log local diretamente.
    No Windows: SSH para M4.
    """
    if IS_M4:
        log_path = Path(_M4_LOG)
        try:
            if not log_path.exists():
                return {"reachable": True, "error": "log não encontrado"}
            mtime_epoch = log_path.stat().st_mtime
            last_fim = ""
            for line in reversed(log_path.read_text(errors="replace").splitlines()):
                if "FIM:" in line:
                    last_fim = line
                    break
            return {"reachable": True, "log_mtime_epoch": mtime_epoch, "last_fim_line": last_fim}
        except Exception as e:
            return {"reachable": False, "error": str(e)}

    cmd = [
        "ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
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
        return {"reachable": True, "log_mtime_epoch": mtime_epoch, "last_fim_line": last_fim}
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
