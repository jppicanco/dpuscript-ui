#!/usr/bin/env python3
"""
Inicia o servidor dpuscript-ui de forma limpa.

- Mata QUALQUER processo anterior na porta 8001 (incluindo orfaos)
- Grava PID em .server.pid
- Inicia uvicorn em foreground (sem reload, evita processos duplicados)

Uso:
    .venv\\Scripts\\python.exe start.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
PORT = 8001


def _killp_windows(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _kill_by_port(port: int) -> list[int]:
    """Mata TODOS os processos LISTEN na porta informada. Retorna PIDs mortos."""
    killed: list[int] = []
    if os.name == "nt":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True,
        )
        seen: set[int] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local, state = parts[1], parts[3] if len(parts) >= 4 else ""
            if state != "LISTENING":
                continue
            if not local.endswith(f":{port}"):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid in seen or pid <= 4:
                continue
            seen.add(pid)
            _killp_windows(pid)
            killed.append(pid)
    else:
        result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ValueError, ProcessLookupError):
                pass
    return killed


def _kill_pid_file() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        if os.name == "nt":
            _killp_windows(pid)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return pid
    except (ValueError, FileNotFoundError):
        return None
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    # 1. Mata servidor anterior via PID file
    old_pid = _kill_pid_file()
    if old_pid:
        print(f"[start] matou servidor anterior (PID file): {old_pid}")

    # 2. Mata qualquer um que ainda esteja na porta
    killed = _kill_by_port(PORT)
    if killed:
        print(f"[start] matou processos na porta {PORT}: {killed}")

    # 3. Espera o SO liberar a porta (TIME_WAIT)
    time.sleep(2)

    # 4. Inicia servidor em foreground e grava PID
    python_exe = sys.executable
    app_path = ROOT / "app.py"
    print(f"[start] iniciando servidor na porta {PORT}...")

    proc = subprocess.Popen(
        [python_exe, str(app_path)],
        cwd=str(ROOT),
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"[start] servidor rodando PID={proc.pid} — http://127.0.0.1:{PORT}")
    print(f"[start] para parar: python stop.py  ou  Ctrl+C")

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[start] recebido Ctrl+C, parando servidor...")
        if os.name == "nt":
            _killp_windows(proc.pid)
        else:
            proc.terminate()
        try:
            PID_FILE.unlink()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
