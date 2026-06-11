#!/usr/bin/env python3
"""Runner único e seguro pra invocar o `claude` CLI — resolve de vez as 2 causas
que matavam a cota / deixavam lixo:

1. CONCORRÊNCIA → rate limit por minuto (TPM). Solução: LOCK GLOBAL entre
   processos (arquivo de lock com PID + detecção de stale). Só UMA chamada
   `claude` roda por vez no sistema inteiro (batch, UI, cron) — igual ao
   Desktop. Os demais esperam na fila. Escalável: o trabalho enfileira, nunca
   estoura em burst.

2. VAZAMENTO DE PROCESSOS. O claude CLI gera filhos (MCP servers, sub-procs)
   que não morrem com o pai no Windows. Solução: REAPER que mata processos
   órfãos (cujo processo-pai já morreu) — seguro, NUNCA mata a sessão Claude
   interativa atual (cujo pai está vivo).

Uso:
    from claude_runner import run_claude, reap_orphans
    rc, out, err = run_claude(cmd_list, input_text, cwd, env, timeout)
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_TEMP = Path(os.getenv("TEMP", r"C:\Users\JP\AppData\Local\Temp"))
_LOCK = _TEMP / "dpu_claude_global.lock"
_LOCK_STALE_S = 1800          # lock com mais de 30 min = stale (chamada travou)
_LOCK_POLL_S = 2
_MIN_SPACING_S = float(os.getenv("CLAUDE_MIN_SPACING", "3"))  # respiro entre chamadas (anti-burst)
_last_release = [0.0]


# ---------------------------------------------------------------------------
# Lock global entre processos (serializa TODAS as chamadas claude)
# ---------------------------------------------------------------------------
def _pid_vivo(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return str(pid) in (out.stdout or "")
    except Exception:
        return True  # na dúvida, considera vivo (não rouba o lock)


def _ler_lock() -> tuple[int, float] | None:
    if not _LOCK.exists():
        return None
    try:
        txt = _LOCK.read_text(encoding="utf-8").strip().split(",")
        return int(txt[0]), float(txt[1])
    except Exception:
        return None


def acquire(timeout: float = 3600) -> bool:
    """Adquire o lock global. Espera enquanto outro detém. Rouba se stale."""
    inicio = time.time()
    while True:
        info = _ler_lock()
        if info is None:
            # livre — tenta criar atomicamente
            try:
                fd = os.open(str(_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()},{time.time()}".encode())
                os.close(fd)
                return True
            except FileExistsError:
                continue  # corrida — outro pegou; reavalia
        else:
            pid, ts = info
            stale = (time.time() - ts > _LOCK_STALE_S) or (not _pid_vivo(pid))
            if stale:
                try:
                    _LOCK.unlink()
                except Exception:
                    pass
                continue
        if time.time() - inicio > timeout:
            return False
        time.sleep(_LOCK_POLL_S)


def release() -> None:
    info = _ler_lock()
    if info and info[0] == os.getpid():
        try:
            _LOCK.unlink()
        except Exception:
            pass
    _last_release[0] = time.time()


# ---------------------------------------------------------------------------
# Reaper de órfãos (processos claude/MCP cujo pai já morreu)
# ---------------------------------------------------------------------------
_PS_LIST = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.Name -in @('node.exe','python.exe','cmd.exe','conhost.exe') } | "
    "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
)


def reap_orphans(verbose: bool = False) -> int:
    """Mata processos claude-code / MCP server cujo PROCESSO-PAI já morreu
    (órfãos). NUNCA mata processo com pai vivo (preserva a sessão interativa).
    Retorna quantos matou."""
    import json as _json
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_LIST],
                            capture_output=True, text=True, timeout=30)
        data = _json.loads(r.stdout) if r.stdout.strip() else []
    except Exception:
        return 0
    if isinstance(data, dict):
        data = [data]

    vivos = {p.get("ProcessId") for p in data}
    alvo_cmd = ("@anthropic-ai\\claude-code", "claude.cmd", "mcp_servers", "claude-code")
    mortos = 0
    for p in data:
        cmd = (p.get("CommandLine") or "")
        ppid = p.get("ParentProcessId")
        pid = p.get("ProcessId")
        if not any(m in cmd for m in alvo_cmd):
            continue
        # órfão = pai não está mais na lista de vivos
        if ppid in vivos:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
            mortos += 1
            if verbose:
                print(f"[reaper] matou órfão PID {pid}: {cmd[:60]}")
        except Exception:
            pass
    return mortos


# ---------------------------------------------------------------------------
# Execução governada
# ---------------------------------------------------------------------------
def run_claude(cmd: list[str], input_text: str, cwd: str, env: dict,
               timeout: int) -> tuple[int, str, str]:
    """Roda o claude CLI sob o lock global (1 por vez no sistema), com respiro
    anti-burst, e reapa órfãos depois. Mata a árvore no timeout.

    Retorna (returncode, stdout, stderr).
    """
    if not acquire(timeout=3600):
        return (-1, "", "não consegui o lock global (timeout)")
    try:
        # respiro mínimo entre chamadas (suaviza TPM)
        desde = time.time() - _last_release[0]
        if desde < _MIN_SPACING_S:
            time.sleep(_MIN_SPACING_S - desde)

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, creationflags=creationflags,
        )
        try:
            out, err = proc.communicate(input=input_text.encode("utf-8"), timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            # mata a ÁRVORE inteira (claude + filhos MCP)
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out, err = b"", b"timeout"
            rc = -9
        # garante que nenhum filho do claude sobrou (mata a árvore pelo PID)
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        return (rc,
                out.decode("utf-8", errors="replace") if isinstance(out, bytes) else (out or ""),
                err.decode("utf-8", errors="replace") if isinstance(err, bytes) else (err or ""))
    finally:
        release()
        try:
            reap_orphans()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "reap":
        n = reap_orphans(verbose=True)
        print(f"órfãos mortos: {n}")
