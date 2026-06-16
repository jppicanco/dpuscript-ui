"""Central de Atuação — lê os artefatos atuacao.json produzidos pelo
batch_atuacao.py e os combina com a metadata do PAJ, pra UI mostrar de pronto
o que é cada PAJ + o que o Defensor tem que fazer.

atuacao.json (por PAJ, gerado pelo batch):
{
  "status": "done|erro|timeout",
  "tipo": "DESPACHO|ARQUIVAMENTO|RECURSO|NAO_ATUAR",
  "peca_tipo": "...", "prazo": "...", "confianca": "...",
  "arquivos": "...", "resumo": "...", "o_que_fazer": "...",
  "alertas": "...", "movimentacao": "...", "concluido_em": "..."
}
"""

from __future__ import annotations

import json
from pathlib import Path

from config import ENTRADA_DIR


def _ler_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _listar_arquivos_gerados(pasta: Path) -> list[dict]:
    """Lista DOCX/PDF/TXT de peça/despacho gerados na raiz da pasta do PAJ."""
    IGNORAR = {
        "metadata.json", "eventos_tnu.json", "datajud.json", "PROMPT_MAX.md",
        "elaboracao.json", "atuacao.json", "resumo_curto.md",
        "prazos_detectados.json", "resumo.md", "plano_elaboracao.json",
    }
    out = []
    for f in sorted(pasta.iterdir()):
        if not f.is_file() or f.name in IGNORAR:
            continue
        if f.suffix.lower() in (".docx", ".pdf", ".txt"):
            out.append({
                "nome": f.name,
                "ext": f.suffix.lower().lstrip("."),
                "tamanho": f.stat().st_size,
            })
    return out


def _norm_to_paj(paj_norm: str) -> str:
    # 2026-039-07596 -> 2026/039-07596
    return paj_norm.replace("-", "/", 1)


def listar_atuacoes() -> list[dict]:
    """Lista todos os PAJs com seu artefato de atuação (se houver)."""
    if not ENTRADA_DIR.exists():
        return []

    out: list[dict] = []
    for pasta in sorted(ENTRADA_DIR.iterdir()):
        if not pasta.is_dir():
            continue
        meta = _ler_json(pasta / "metadata.json")
        if not meta:
            continue
        paj_norm = pasta.name
        atuacao = _ler_json(pasta / "atuacao.json") or {}
        cm = _ler_json(pasta / "concluido_manual.json") or {}
        arquivos = _listar_arquivos_gerados(pasta)

        det = meta.get("detalhes_sisdpu", {}) or {}
        out.append({
            "paj_norm": paj_norm,
            "paj": meta.get("paj", _norm_to_paj(paj_norm)),
            "assistido": meta.get("assistido_caixa", ""),
            "oficio": meta.get("oficio_caixa", ""),
            "foro": meta.get("foro_detectado", "?"),
            "classificacao": meta.get("classificacao", "?"),
            "data_caixa": meta.get("data_mov_caixa", ""),
            "desc_caixa": (meta.get("desc_mov_caixa", "") or "")[:200],
            "processo_judicial": meta.get("processo_judicial", ""),
            # artefato de atuação
            "atuacao_status": atuacao.get("status", "pendente"),
            "tipo": atuacao.get("tipo", ""),
            "peca_tipo": atuacao.get("peca_tipo", ""),
            "prazo": atuacao.get("prazo", ""),
            "confianca": atuacao.get("confianca", ""),
            "resumo": atuacao.get("resumo", ""),
            "o_que_fazer": atuacao.get("o_que_fazer", ""),
            "alertas": atuacao.get("alertas", ""),
            "movimentacao": atuacao.get("movimentacao", ""),
            "concluido_em": atuacao.get("concluido_em", ""),
            # marcador manual "já concluí no SIS" (independe da reconciliação)
            "concluido_manual": bool(cm.get("em")),
            "concluido_manual_em": cm.get("em", ""),
            # kit de recurso (só RECURSO): dossiê pronto pro Claude
            "recurso_tipo": atuacao.get("recurso_tipo", ""),
            "pecas_chave": atuacao.get("pecas_chave", []),
            "preparo_recurso": (_ler_texto(pasta / "preparo_recurso.md")
                                if atuacao.get("tipo") == "RECURSO" else ""),
            "arquivos": arquivos,
        })
    return out


def _ler_texto(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def _marker_path(paj_norm: str) -> Path:
    return ENTRADA_DIR / paj_norm / "concluido_manual.json"


def concluir_manual(paj_norm: str) -> dict | None:
    """Marca o PAJ como concluído manualmente pelo JP (já despachado no SIS).
    Não mexe na peça nem no atuacao.json; só grava um marcador."""
    pasta = ENTRADA_DIR / paj_norm
    if not pasta.is_dir():
        return None
    from datetime import datetime, timezone
    em = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    _marker_path(paj_norm).write_text(
        json.dumps({"em": em}, ensure_ascii=False), encoding="utf-8"
    )
    return {"paj_norm": paj_norm, "concluido_manual": True, "concluido_manual_em": em}


def reabrir_manual(paj_norm: str) -> dict | None:
    """Desfaz a conclusão manual (remove o marcador)."""
    pasta = ENTRADA_DIR / paj_norm
    if not pasta.is_dir():
        return None
    mp = _marker_path(paj_norm)
    if mp.exists():
        mp.unlink()
    return {"paj_norm": paj_norm, "concluido_manual": False}


def atuacao_paj(paj_norm: str) -> dict | None:
    pasta = ENTRADA_DIR / paj_norm
    if not pasta.exists():
        return None
    meta = _ler_json(pasta / "metadata.json") or {}
    atuacao = _ler_json(pasta / "atuacao.json") or {}
    cm = _ler_json(pasta / "concluido_manual.json") or {}
    return {
        "paj_norm": paj_norm,
        "paj": meta.get("paj", _norm_to_paj(paj_norm)),
        "assistido": meta.get("assistido_caixa", ""),
        **atuacao,
        "concluido_manual": bool(cm.get("em")),
        "concluido_manual_em": cm.get("em", ""),
        "arquivos": _listar_arquivos_gerados(pasta),
    }


def resumo_batch() -> dict:
    """Contagens pra cabeçalho da Central de Atuação."""
    ats = listar_atuacoes()
    por_status: dict[str, int] = {}
    por_tipo: dict[str, int] = {}
    concluidos_manual = 0
    for a in ats:
        s = a["atuacao_status"]
        por_status[s] = por_status.get(s, 0) + 1
        t = a.get("tipo") or "—"
        if a["atuacao_status"] == "done":
            por_tipo[t] = por_tipo.get(t, 0) + 1
        if a.get("concluido_manual"):
            concluidos_manual += 1
    return {
        "total": len(ats),
        "por_status": por_status,
        "por_tipo": por_tipo,
        "concluidos_manual": concluidos_manual,
    }
