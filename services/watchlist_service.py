"""Servico de leitura/escrita da watchlist de transito em julgado.

A watchlist vive em E:\\DPU\\dpu-workspace\\dpuscript\\watchlist.json e eh
escrita tanto pelo script monitor_transito.py quanto pelas acoes da UI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from config import DPUSCRIPT_DIR

WATCHLIST_FILE = DPUSCRIPT_DIR / "watchlist.json"


def _carregar() -> dict:
    if not WATCHLIST_FILE.exists():
        return {"itens": {}, "atualizada_em": None}
    try:
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"itens": {}, "atualizada_em": None}


def _salvar(wl: dict) -> None:
    wl["atualizada_em"] = datetime.now().isoformat()
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        json.dumps(wl, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def listar() -> list[dict]:
    wl = _carregar()
    itens = []
    for paj, item in wl.get("itens", {}).items():
        itens.append({"paj": paj, **item})
    # Ordem: transitou primeiro, depois ativos, depois removidos
    def _ord(x):
        st = x.get("status", "")
        return (0 if st == "transitou" else 1 if st == "ativo" else 2, x.get("adicionado_em", ""))
    itens.sort(key=_ord)
    return itens


def stats() -> dict:
    wl = _carregar()
    itens = wl.get("itens", {})
    return {
        "total": len(itens),
        "ativo": sum(1 for v in itens.values() if v.get("status") == "ativo"),
        "transitou": sum(1 for v in itens.values() if v.get("status") == "transitou"),
        "atualizada_em": wl.get("atualizada_em"),
    }


def adicionar(paj: str, cnj: str, motivo: str = "arquivamento_por_vitoria",
              frequencia_dias: int = 15, expectativa: str = "") -> dict:
    wl = _carregar()
    itens = wl.setdefault("itens", {})
    proxima = (datetime.now() + timedelta(days=frequencia_dias)).date().isoformat()
    itens[paj] = {
        "cnj": cnj,
        "adicionado_em": datetime.now().isoformat(),
        "motivo": motivo,
        "expectativa": expectativa or "transito em julgado + baixa a origem",
        "frequencia_dias": frequencia_dias,
        "ultima_verificacao": None,
        "proxima_verificacao": proxima,
        "status": "ativo",
        "ultimo_erro": None,
        "historico": [],
    }
    _salvar(wl)
    return itens[paj]


def remover(paj: str) -> bool:
    wl = _carregar()
    itens = wl.get("itens", {})
    if paj in itens:
        del itens[paj]
        _salvar(wl)
        return True
    return False


def get(paj: str) -> dict | None:
    wl = _carregar()
    return wl.get("itens", {}).get(paj)
