"""Servico de leitura do state.json e logs do pipeline dpuscript.

Usado pela pagina /pipeline pra mostrar status em tempo real + historico.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

from config import DPUSCRIPT_DIR

LOG_DIR = DPUSCRIPT_DIR / "logs"
STATE_FILE = LOG_DIR / "state.json"


def ler_state() -> dict:
    """Retorna state atual do pipeline, ou {} se nao existe."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ler_log_atual(max_linhas: int = 500) -> dict:
    """Le as ultimas N linhas do log da execucao ATUAL (state_file.log_file)."""
    state = ler_state()
    log_path_str = state.get("log_file")
    if not log_path_str:
        return {"linhas": [], "caminho": None, "tamanho": 0}

    log_path = Path(log_path_str)
    if not log_path.exists():
        return {"linhas": [], "caminho": str(log_path), "tamanho": 0}

    try:
        linhas = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {"linhas": [], "caminho": str(log_path), "tamanho": 0}

    return {
        "linhas": linhas[-max_linhas:],
        "caminho": str(log_path),
        "tamanho": len(linhas),
    }


def listar_runs(max_runs: int = 20) -> list[dict]:
    """Lista execucoes passadas (arquivos run_*.log), ordenadas por data desc."""
    if not LOG_DIR.exists():
        return []

    runs: list[dict] = []
    for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True)[:max_runs]:
        try:
            stat = f.stat()
            # Extrai timestamp do nome: run_2026-04-16_083243.log
            name = f.stem  # run_2026-04-16_083243
            try:
                ts_str = name.removeprefix("run_")
                inicio = datetime.strptime(ts_str, "%Y-%m-%d_%H%M%S")
            except Exception:
                inicio = datetime.fromtimestamp(stat.st_ctime)

            # Tenta extrair resumo (ultimas linhas: FIM: X processados, Y falhas)
            resumo = ""
            try:
                conteudo = f.read_text(encoding="utf-8", errors="replace")
                for linha in reversed(conteudo.splitlines()):
                    if "FIM:" in linha or "processados" in linha.lower():
                        resumo = linha[:200]
                        break
            except Exception:
                pass

            runs.append({
                "nome": f.name,
                "caminho": str(f),
                "inicio": inicio.isoformat(),
                "tamanho_bytes": stat.st_size,
                "tamanho_linhas": conteudo.count("\n") if 'conteudo' in locals() else 0,
                "resumo": resumo,
                "modificado": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except Exception:
            continue

    return runs


def ler_run(nome_arquivo: str, max_linhas: int = 2000) -> dict:
    """Le um log de execucao historica."""
    # Sanitiza: so permite run_*.log
    if not nome_arquivo.startswith("run_") or not nome_arquivo.endswith(".log"):
        return {"linhas": [], "erro": "arquivo invalido"}

    f = LOG_DIR / nome_arquivo
    try:
        f.resolve().relative_to(LOG_DIR.resolve())  # previne path traversal
    except ValueError:
        return {"linhas": [], "erro": "caminho invalido"}

    if not f.exists():
        return {"linhas": [], "erro": "arquivo nao encontrado"}

    try:
        linhas = f.read_text(encoding="utf-8", errors="replace").splitlines()
        return {
            "nome": nome_arquivo,
            "linhas": linhas[-max_linhas:],
            "total_linhas": len(linhas),
            "caminho": str(f),
        }
    except Exception as e:
        return {"linhas": [], "erro": str(e)}
