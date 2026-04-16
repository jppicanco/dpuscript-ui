"""Servico de leitura de dados de PAJs processados pelo dpuscript."""

from __future__ import annotations

import json
from pathlib import Path

from config import ENTRADA_DIR, ESTADO_FILE
from services.nomes_pecas import parse_nome_peca, CATEGORIAS_ORDEM, CATEGORIAS_LABEL


def _ler_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def listar_pajs() -> list[dict]:
    """Retorna lista de PAJs com metadata resumida pra dashboard."""
    estado = _ler_json(ESTADO_FILE) or {}
    pajs_estado = estado.get("pajs", {})
    ultima_execucao = estado.get("ultima_execucao")

    resultado: list[dict] = []

    # Itera pelas pastas em Entrada/dpuscript/
    if not ENTRADA_DIR.exists():
        return resultado

    for pasta in sorted(ENTRADA_DIR.iterdir()):
        if not pasta.is_dir():
            continue

        paj_norm = pasta.name  # ex: 2018-039-17434
        metadata_path = pasta / "metadata.json"
        metadata = _ler_json(metadata_path)

        if not metadata:
            continue

        paj_id = metadata.get("paj", paj_norm.replace("-", "/", 1))
        estado_paj = pajs_estado.get(paj_id, {})

        # Conta arquivos
        pecas_dir = pasta / "peças"
        decisoes_dir = pasta / "decisoes_superiores"
        n_pecas = sum(1 for f in pecas_dir.glob("*.txt")) if pecas_dir.exists() else 0
        n_decisoes = sum(1 for f in decisoes_dir.glob("*.txt")) if decisoes_dir.exists() else 0

        # Extrai movimentacoes recentes (top 3 por seq desc)
        det = metadata.get("detalhes_sisdpu", {}) or {}
        movs = det.get("movimentacoes", []) or []
        movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0), reverse=True)
        ultima_mov = movs_sorted[0] if movs_sorted else {}

        resultado.append({
            "paj": paj_id,
            "paj_norm": paj_norm,
            "assistido": metadata.get("assistido_caixa", ""),
            "oficio": metadata.get("oficio_caixa", ""),
            "foro": metadata.get("foro_detectado", "?"),
            "classificacao": metadata.get("classificacao", "?"),
            "data_caixa": metadata.get("data_mov_caixa", ""),
            "desc_caixa": metadata.get("desc_mov_caixa", ""),
            "processo_judicial": metadata.get("processo_judicial", ""),
            "n_pecas": n_pecas,
            "n_decisoes": n_decisoes,
            "ultima_preparacao": estado_paj.get("ultima_preparacao", ""),
            "prazos_abertos": metadata.get("prazos_abertos", []),
            "ultima_mov_desc": (ultima_mov.get("descricao") or "")[:120],
            "ultima_mov_data": ultima_mov.get("data", ""),
            "status_sisdpu": (det.get("status_paj") or "").strip(),
        })

    return resultado


def ler_paj(paj_norm: str) -> dict | None:
    """Retorna dados completos de um PAJ especifico."""
    pasta = ENTRADA_DIR / paj_norm
    if not pasta.exists():
        return None

    metadata = _ler_json(pasta / "metadata.json")
    if not metadata:
        return None

    # PROMPT_MAX
    prompt_max_path = pasta / "PROMPT_MAX.md"
    prompt_max = prompt_max_path.read_text(encoding="utf-8") if prompt_max_path.exists() else ""

    # Lista de pecas do processo (PDFs + TXTs baixados)
    # Agrupa PDF+TXT do mesmo doc em um unico item, com nome legivel
    pecas_map: dict[str, dict] = {}
    pecas_dir = pasta / "peças"
    if pecas_dir.exists():
        for f in sorted(pecas_dir.iterdir()):
            if not f.is_file():
                continue
            meta = parse_nome_peca(f.name)
            # Chave = nome sem extensao — agrupa .pdf e .txt do mesmo doc
            chave = f.stem
            if chave not in pecas_map:
                pecas_map[chave] = {
                    **meta,
                    "nome_arquivo": chave,
                    "pdf_caminho": None,
                    "pdf_tamanho": 0,
                    "txt_caminho": None,
                    "txt_tamanho": 0,
                }
            if f.suffix.lower() == ".pdf":
                pecas_map[chave]["pdf_caminho"] = f"peças/{f.name}"
                pecas_map[chave]["pdf_tamanho"] = f.stat().st_size
            elif f.suffix.lower() == ".txt":
                pecas_map[chave]["txt_caminho"] = f"peças/{f.name}"
                pecas_map[chave]["txt_tamanho"] = f.stat().st_size

    # Ordena: primeiro por categoria (decisoes no topo), depois por data desc
    ordem_cat = {c: i for i, c in enumerate(CATEGORIAS_ORDEM)}
    pecas = sorted(
        pecas_map.values(),
        key=lambda p: (ordem_cat.get(p["categoria"], 99), -_ts(p.get("data", ""))),
    )

    # Agrupa por categoria pra renderizacao
    pecas_por_categoria: list[dict] = []
    for cat in CATEGORIAS_ORDEM:
        itens = [p for p in pecas if p["categoria"] == cat]
        if itens:
            pecas_por_categoria.append({
                "categoria": cat,
                "label": CATEGORIAS_LABEL.get(cat, cat),
                "cor": itens[0]["categoria_cor"],
                "itens": itens,
                "count": len(itens),
            })

    # Lista de decisoes STJ/STF (baixadas separadamente)
    decisoes: list[dict] = []
    decisoes_dir = pasta / "decisoes_superiores"
    if decisoes_dir.exists():
        # Agrupa PDF+TXT
        dec_map: dict[str, dict] = {}
        for f in sorted(decisoes_dir.iterdir()):
            if not f.is_file():
                continue
            chave = f.stem
            tribunal = "STJ" if chave.startswith("STJ_") else ("STF" if chave.startswith("STF_") else "DEC")
            if chave not in dec_map:
                dec_map[chave] = {
                    "nome_arquivo": chave,
                    "tribunal": tribunal,
                    "pdf_caminho": None, "pdf_tamanho": 0,
                    "txt_caminho": None, "txt_tamanho": 0,
                }
            if f.suffix.lower() == ".pdf":
                dec_map[chave]["pdf_caminho"] = f"decisoes_superiores/{f.name}"
                dec_map[chave]["pdf_tamanho"] = f.stat().st_size
            elif f.suffix.lower() == ".txt":
                dec_map[chave]["txt_caminho"] = f"decisoes_superiores/{f.name}"
                dec_map[chave]["txt_tamanho"] = f.stat().st_size
        decisoes = list(dec_map.values())

    # Pecas GERADAS pelo Claude (arquivos na RAIZ da pasta do PAJ)
    # Ex: despacho.txt, recurso.docx, peca.pdf (exclui metadata/eventos/PROMPT_MAX)
    IGNORAR = {"metadata.json", "eventos_tnu.json", "datajud.json", "PROMPT_MAX.md", "elaboracao.json"}
    pecas_geradas: list[dict] = []
    for f in sorted(pasta.iterdir()):
        if not f.is_file() or f.name in IGNORAR:
            continue
        pecas_geradas.append({
            "nome": f.name,
            "caminho": f.name,  # caminho relativo a pasta do PAJ
            "tipo": f.suffix.lstrip(".").lower(),
            "tamanho": f.stat().st_size,
            "modificado": f.stat().st_mtime,
        })

    # Movimentacoes
    det = metadata.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes", []) or []
    movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0), reverse=True)

    return {
        "metadata": metadata,
        "prompt_max": prompt_max,
        "pecas": pecas,
        "pecas_por_categoria": pecas_por_categoria,
        "decisoes": decisoes,
        "pecas_geradas": pecas_geradas,
        "movimentacoes": movs_sorted,
        "prazos_abertos": metadata.get("prazos_abertos", []),
        "pasta": str(pasta),
    }


def _ts(data_str: str) -> float:
    """Converte '2018-08-14' em epoch; 0 se invalido. Usado em sort."""
    try:
        from datetime import datetime
        return datetime.strptime(data_str, "%Y-%m-%d").timestamp()
    except Exception:
        return 0.0


def ler_arquivo(paj_norm: str, caminho_relativo: str) -> tuple[Path | None, str]:
    """Retorna (path_absoluto, content_type) de um arquivo do PAJ."""
    pasta = ENTRADA_DIR / paj_norm
    arquivo = pasta / caminho_relativo

    # Previne path traversal
    try:
        arquivo.resolve().relative_to(pasta.resolve())
    except ValueError:
        return None, ""

    if not arquivo.exists() or not arquivo.is_file():
        return None, ""

    ext = arquivo.suffix.lower()
    content_types = {
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
    }
    return arquivo, content_types.get(ext, "application/octet-stream")
