#!/usr/bin/env python3
"""Watchdog do batch de atuação — garante que TODOS os PAJs sejam processados,
relançando o batch_atuacao.py se ele morrer (rate limit cascata, crash, etc).

O batch_atuacao.py já pula PAJs concluídos (atuacao.json status=done) e tem
retry+backoff interno. Este watchdog é a camada de cima: enquanto houver
pendentes, mantém um batch vivo. Se o batch parar de dar sinal de vida
(batch_status.json não atualiza há >GRACE s), relança.

Roda em background até zerar pendentes. NÃO usa API paga (batch força OAuth).

Uso:
    python run_batch_until_done.py --workers 2
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("DPU_WORKSPACE", r"E:\DPU\dpu-workspace"))
ENTRADA = WORKSPACE / "Entrada" / "dpuscript"
STATUS_FILE = BASE_DIR / "batch_status.json"
WD_LOG = BASE_DIR / "watchdog.log"

GRACE = 240          # segundos sem atualizar status → considera batch morto
POLL = 60            # intervalo de checagem
MAX_RELAUNCH = 50    # teto de relançamentos (segurança)


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(WD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def contar_pendentes() -> tuple[int, int]:
    """Retorna (pendentes, done) entre PAJs com PROMPT_MAX."""
    done = 0
    pend = 0
    if not ENTRADA.exists():
        return 0, 0
    for d in ENTRADA.iterdir():
        if not d.is_dir() or not (d / "PROMPT_MAX.md").exists():
            continue
        f = d / "atuacao.json"
        ok = False
        if f.exists():
            try:
                ok = json.loads(f.read_text(encoding="utf-8")).get("status") == "done"
            except Exception:
                ok = False
        if ok:
            done += 1
        else:
            pend += 1
    return pend, done


def batch_vivo() -> bool:
    """Heurística: batch_status.json atualizado há < GRACE segundos."""
    if not STATUS_FILE.exists():
        return False
    idade = time.time() - STATUS_FILE.stat().st_mtime
    return idade < GRACE


def lancar_batch(workers: int) -> subprocess.Popen:
    log(f"lançando batch_atuacao.py --workers {workers}")
    # stdout/err pro log de runtime do próprio batch
    out = open(BASE_DIR / "batch_run_stdout.log", "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(BASE_DIR / "batch_atuacao.py"), "--workers", str(workers)],
        stdout=out, stderr=subprocess.STDOUT, cwd=str(BASE_DIR),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    log(f"=== WATCHDOG iniciado (workers={args.workers}) ===")
    relancamentos = 0
    proc: subprocess.Popen | None = None

    while True:
        pend, done = contar_pendentes()
        log(f"pendentes={pend} done={done}")
        if pend == 0:
            log("=== TUDO PROCESSADO — watchdog encerrando ===")
            return 0

        proc_vivo = proc is not None and proc.poll() is None
        if not proc_vivo and not batch_vivo():
            if relancamentos >= MAX_RELAUNCH:
                log(f"teto de relançamentos ({MAX_RELAUNCH}) atingido — parando")
                return 1
            proc = lancar_batch(args.workers)
            relancamentos += 1
            time.sleep(20)  # dá tempo do batch escrever status antes da próxima checagem
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
