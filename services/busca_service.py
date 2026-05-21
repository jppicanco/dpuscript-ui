"""Busca textual no acervo de PAJs locais.

Adaptado de tuliorm/DPU-script-SIS pra schema JP:
- Pastas: Entrada/dpuscript/<YYYY-UUU-NNNNN>/
- Sem sisdpu.txt; usa resumo_curto.md + movs do metadata.json
- Peças/ (com acento) + decisoes_superiores/

Cache em memória 60s TTL. Volume típico: ~50 PAJs × ~100KB texto = ~5MB.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

from config import ENTRADA_DIR

_CACHE_TTL_SEG = 60
_cache: dict = {"ts": 0.0, "corpus": []}

_PAJ_FOLDER_RE = re.compile(r"^\d{4}-\d{3}-\d+$")


def _sem_acento(s: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFD", s or "")
        if unicodedata.category(c) != "Mn"
    ).lower()


def _ler_metadata(pasta: Path) -> dict:
    f = pasta / "metadata.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ler_texto(f: Path, limite_bytes: int = 500_000) -> str:
    try:
        if f.stat().st_size > limite_bytes:
            with open(f, encoding="utf-8", errors="replace") as fp:
                return fp.read(limite_bytes)
        return f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _montar_corpus() -> list[dict]:
    corpus: list[dict] = []
    if not ENTRADA_DIR.exists():
        return corpus

    for pasta in sorted(ENTRADA_DIR.iterdir()):
        if not pasta.is_dir():
            continue
        if not _PAJ_FOLDER_RE.match(pasta.name):
            continue
        paj_norm = pasta.name

        meta = _ler_metadata(pasta)
        det = meta.get("detalhes_sisdpu", {}) or {}

        meta_partes = [
            meta.get("paj", ""),
            meta.get("assistido_caixa", ""),
            meta.get("oficio_caixa", ""),
            meta.get("classificacao", ""),
            meta.get("foro_detectado", ""),
            meta.get("processo_judicial", ""),
            meta.get("desc_mov_caixa", ""),
            det.get("assistido", ""),
            det.get("processo_judicial", ""),
        ]
        meta_blob = " | ".join(p for p in meta_partes if p)

        # Movs do metadata viram blob pesquisável
        movs_blob_partes = []
        for m in det.get("movimentacoes", []) or []:
            d = m.get("descricao") or ""
            if d:
                movs_blob_partes.append(d)
        movs_blob = " ".join(movs_blob_partes)

        # Resumo curto (substituto do sisdpu.txt do colega)
        resumo_blob = ""
        rf = pasta / "resumo_curto.md"
        if rf.exists():
            resumo_blob = _ler_texto(rf)

        # OCR peças + decisões
        ocr_docs: list[dict] = []
        for sub in ("peças", "pecas", "decisoes_superiores"):
            d = pasta / sub
            if not d.exists() or not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() == ".txt":
                    ocr_docs.append({
                        "arquivo": f.name,
                        "subpasta": sub,
                        "texto": _ler_texto(f),
                    })

        corpus.append({
            "paj_norm": paj_norm,
            "meta_blob": meta_blob,
            "movs_blob": movs_blob,
            "resumo_blob": resumo_blob,
            "ocr_docs": ocr_docs,
            "assistido": (meta.get("assistido_caixa")
                          or det.get("assistido", "")),
            "classificacao": meta.get("classificacao", ""),
            "foro": meta.get("foro_detectado", ""),
        })

    return corpus


def _get_corpus() -> list[dict]:
    agora = time.time()
    if _cache["corpus"] and (agora - _cache["ts"]) < _CACHE_TTL_SEG:
        return _cache["corpus"]
    _cache["corpus"] = _montar_corpus()
    _cache["ts"] = agora
    return _cache["corpus"]


def invalidar_cache() -> None:
    _cache["corpus"] = []
    _cache["ts"] = 0.0


def _extrair_trecho(
    texto_norm: str, texto_orig: str, termo_norm: str, janela: int = 140
) -> str:
    i = texto_norm.find(termo_norm)
    if i < 0:
        return ""
    inicio = max(0, i - janela // 2)
    fim = min(len(texto_orig), i + len(termo_norm) + janela // 2)
    prefixo = "..." if inicio > 0 else ""
    sufixo = "..." if fim < len(texto_orig) else ""
    trecho = texto_orig[inicio:fim].replace("\n", " ").replace("\r", " ")
    try:
        rx = re.compile(re.escape(termo_norm), re.IGNORECASE)
        trecho = rx.sub(lambda m: f"<mark>{m.group(0)}</mark>", trecho)
    except re.error:
        pass
    return prefixo + trecho + sufixo


def buscar(q: str, limite: int = 50) -> list[dict]:
    """Busca termo `q`. Score:
      - meta_blob: +10 por hit
      - resumo_blob: +5
      - movs_blob: +3
      - OCR (peças/decisões): +1
    """
    termo_norm = _sem_acento(q.strip())
    if len(termo_norm) < 2:
        return []

    resultados: list[dict] = []

    for doc in _get_corpus():
        score = 0
        melhor_trecho = ""
        melhor_fonte = ""
        melhor_arquivo = ""

        for nome_blob, peso, fonte_tag in (
            ("meta_blob", 10, "metadata"),
            ("resumo_blob", 5, "resumo"),
            ("movs_blob", 3, "movs"),
        ):
            blob = doc.get(nome_blob, "")
            blob_norm = _sem_acento(blob)
            hits = blob_norm.count(termo_norm)
            if hits:
                score += peso * hits
                if not melhor_trecho:
                    melhor_trecho = _extrair_trecho(
                        blob_norm, blob, termo_norm
                    )
                    melhor_fonte = fonte_tag

        for ocr in doc["ocr_docs"]:
            ocr_norm = _sem_acento(ocr["texto"])
            hits = ocr_norm.count(termo_norm)
            if hits:
                score += hits
                if not melhor_trecho:
                    melhor_trecho = _extrair_trecho(
                        ocr_norm, ocr["texto"], termo_norm
                    )
                    melhor_fonte = ocr["subpasta"]
                    melhor_arquivo = ocr["arquivo"]

        if score > 0:
            resultados.append({
                "paj_norm": doc["paj_norm"],
                "assistido": doc["assistido"],
                "classificacao": doc["classificacao"],
                "foro": doc["foro"],
                "score": score,
                "fonte": melhor_fonte,
                "arquivo": melhor_arquivo,
                "trecho": melhor_trecho,
            })

    resultados.sort(key=lambda r: r["score"], reverse=True)
    return resultados[:limite]
