"""dpuscript-ui — Interface web para o pipeline dpuscript da DPU.

Auto-gerencia processo: mata qualquer python na porta antes de iniciar,
grava PID file. Evita duplicatas.
"""

import os
import subprocess
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import jinja2

from routes.dashboard import router as dashboard_router
from routes.paj import router as paj_router
from routes.files import router as files_router
from routes.pipeline import router as pipeline_router
from routes.chat import router as chat_router
from routes.pipeline_monitor import router as pipeline_monitor_router

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".server.pid"
PORT = 8001

app = FastAPI(title="dpuscript-ui", version="0.1.0")
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(dashboard_router)
app.include_router(paj_router)
app.include_router(files_router)
app.include_router(pipeline_router)
app.include_router(chat_router)
app.include_router(pipeline_monitor_router)


def _cleanup_port(port: int) -> None:
    """Mata TODOS processos LISTEN na porta informada (Windows-only)."""
    if os.name != "nt":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True
        )
    except Exception:
        return
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4 or pid == os.getpid() or pid in seen:
            continue
        seen.add(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[app] killed stale process on port {port}: PID {pid}")


def _kill_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
            print(f"[app] killed previous server PID {old_pid}")
    except (ValueError, FileNotFoundError):
        pass
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    _kill_pid_file()
    _cleanup_port(PORT)
    import time
    time.sleep(1)  # aguarda SO liberar porta

    PID_FILE.write_text(str(os.getpid()))
    print(f"[app] servidor PID={os.getpid()} — http://127.0.0.1:{PORT}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
