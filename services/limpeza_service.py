"""Limpeza de anexos PDF/binarios pos-OCR.

Politica: apaga binarios originais (PDF, docx, imagens) das subpastas `peças/`
e `decisoes_superiores/`, PRESERVANDO os .txt OCR ao lado. Tambem preserva
metadata.json, PROMPT_MAX.md, resumo_curto.md, eventos_tnu.json, prazos_detectados.json.

Detecta se PAJ esta ATIVO (em ENTRADA_DIR) ou ARQUIVADO (em ARQUIVADOS_DIR).
PAJ ativo requer `forcar=True` (safeguard).
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

from config import ARQUIVADOS_DIR, ENTRADA_DIR


SUBPASTAS_LIMPAVEIS = ("peças", "decisoes_superiores")


def _ler_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _localizar_paj(paj_norm: str) -> tuple[Path | None, bool]:
    """Retorna (pasta, em_caixa_atual). em_caixa_atual=False se arquivado."""
    pasta_ativo = ENTRADA_DIR / paj_norm
    if pasta_ativo.exists() and pasta_ativo.is_dir():
        return pasta_ativo, True
    pasta_arq = ARQUIVADOS_DIR / paj_norm
    if pasta_arq.exists() and pasta_arq.is_dir():
        return pasta_arq, False
    return None, False


def _coletar_arquivos(pasta: Path) -> tuple[list[dict], list[dict]]:
    """Varre subpastas limpaveis. Retorna (a_remover, preservados)."""
    a_remover: list[dict] = []
    preservados: list[dict] = []

    for nome_sub in SUBPASTAS_LIMPAVEIS:
        sub = pasta / nome_sub
        if not sub.exists() or not sub.is_dir():
            continue

        txts_ocr: dict[str, Path] = {}
        for f in sub.iterdir():
            if f.is_file() and f.suffix.lower() == ".txt":
                txts_ocr[f.stem] = f

        for f in sorted(sub.iterdir()):
            if not f.is_file():
                continue
            rel = f"{nome_sub}/{f.name}"
            if f.suffix.lower() == ".txt":
                preservados.append({
                    "nome": rel,
                    "tamanho": f.stat().st_size,
                    "motivo": "OCR companion",
                })
                continue
            ocr = txts_ocr.get(f.stem)
            tem_ocr = ocr is not None and ocr.stat().st_size > 0
            a_remover.append({
                "nome": rel,
                "tamanho": f.stat().st_size,
                "tem_ocr": tem_ocr,
            })

    return a_remover, preservados


def limpar_anexos_paj(
    paj_norm: str,
    dry_run: bool = True,
    forcar: bool = False,
) -> dict:
    """Prepara (dry_run) ou executa limpeza dos anexos de um PAJ.

    Safeguards (bloqueiam execucao automatica; passe `forcar=True` pra override):
      1. Todos os binarios precisam ter .txt OCR companion nao-vazio
      2. PAJ precisa estar arquivado (fora da caixa). PAJ ativo => bloqueio.

    Returns:
        {
            "ok": bool, "pode_limpar": bool, "motivos_bloqueio": [...],
            "em_caixa_atual": bool, "arquivos_a_remover": [...],
            "arquivos_preservados": [...], "removidos": N, "bytes_liberados": N,
            "bytes_total_disponivel": N,
        }
    """
    pasta, em_caixa = _localizar_paj(paj_norm)
    if pasta is None:
        return {
            "ok": False,
            "erro": f"PAJ {paj_norm} nao encontrado (nem em Entrada/dpuscript, nem em dpuscript_arquivados)",
            "arquivos_a_remover": [],
            "arquivos_preservados": [],
        }

    a_remover, preservados = _coletar_arquivos(pasta)

    motivos: list[str] = []
    sem_ocr = [a for a in a_remover if not a["tem_ocr"]]
    if sem_ocr:
        motivos.append(
            f"{len(sem_ocr)} arquivo(s) sem OCR — perder o original removeria o conteudo "
            "(verifique Tesseract instalado e se o arquivo realmente foi processado)"
        )
    if em_caixa:
        motivos.append(
            "PAJ esta ATIVO na caixa SISDPU — limpar agora pode dificultar elaboracao "
            "de pecas que ainda precisam consultar o original"
        )

    pode_limpar = len(motivos) == 0
    bytes_total = sum(a["tamanho"] for a in a_remover)

    resultado: dict = {
        "ok": True,
        "pode_limpar": pode_limpar,
        "motivos_bloqueio": motivos,
        "em_caixa_atual": em_caixa,
        "arquivos_a_remover": a_remover,
        "arquivos_preservados": preservados,
        "removidos": 0,
        "bytes_liberados": 0,
        "bytes_total_disponivel": bytes_total,
    }

    if dry_run:
        return resultado

    if not pode_limpar and not forcar:
        resultado["ok"] = False
        resultado["erro"] = "bloqueado por safeguards — use forcar=True para override"
        return resultado

    removidos = 0
    bytes_liberados = 0
    for a in a_remover:
        arq = pasta / a["nome"]  # nome ja contem "subpasta/arquivo"
        try:
            tam = arq.stat().st_size
            arq.unlink()
            removidos += 1
            bytes_liberados += tam
        except Exception:
            pass

    resultado["removidos"] = removidos
    resultado["bytes_liberados"] = bytes_liberados

    if removidos > 0:
        metadata = _ler_json(pasta / "metadata.json") or {}
        metadata["n_anexos_removidos"] = metadata.get("n_anexos_removidos", 0) + removidos
        metadata.setdefault("anexos_removidos_em", []).append(
            datetime.now().isoformat(timespec="seconds")
        )
        with contextlib.suppress(Exception):
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    return resultado
