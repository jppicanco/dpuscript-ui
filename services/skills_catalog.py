"""Catalogo dinamico de skills do dpu-workspace.

Le `SKILLS_DIR/<grupo>/<slug?>/SKILL.md`. Estrutura hierarquica do JP:
  - skills/triagem/SKILL.md                    (grupo: triagem)
  - skills/elaboracao/memoriais/SKILL.md       (grupo: elaboracao, slug: memoriais)
  - skills/pesquisa/busca-rapida/SKILL.md
  - skills/validacao/anti-alucinacao/SKILL.md
  - skills/_shared/formatacao-docx/SKILL.md    (utilitario — escondido do dropdown)
  - skills/arquivamento/SKILL.md

Frontmatter YAML nao e usado. Extracao do label vem do `# Skill: X` na primeira
linha; descricao vem dos primeiros paragrafos apos `## Objetivo` ou abaixo do
titulo.

Cache 60s + invalidacao por mtime do diretorio raiz.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from threading import Lock

from config import SKILLS_DIR


GRUPOS_OCULTOS = {"_shared", "_archive"}


_TITULO_RE = re.compile(r"^#\s*SKILL\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_OBJETIVO_RE = re.compile(r"##\s*Objetivo\s*\n+(.+?)(?:\n\n|\n##|\Z)", re.IGNORECASE | re.DOTALL)


def _extrair_meta(texto: str, fallback_label: str) -> tuple[str, str]:
    """Retorna (label, descricao) do conteudo SKILL.md."""
    m_titulo = _TITULO_RE.search(texto)
    label = m_titulo.group(1).strip() if m_titulo else fallback_label

    m_obj = _OBJETIVO_RE.search(texto)
    descricao = ""
    if m_obj:
        descricao = m_obj.group(1).strip()
        # Limita a ~280 char (1ª frase ou primeiros 280 chars)
        if len(descricao) > 280:
            corte = descricao.rfind(".", 0, 280)
            descricao = descricao[:corte + 1] if corte > 100 else descricao[:280] + "..."
    return label, descricao


def _label_padrao(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").capitalize()


def _signature_skills() -> tuple[float, int]:
    """Mtime do dir raiz + contagem grupos. Renomes/criacoes invalidam cache."""
    if not SKILLS_DIR.exists():
        return (0.0, 0)
    try:
        total = 0
        for grupo in SKILLS_DIR.iterdir():
            if grupo.is_dir():
                total += 1
                for _ in grupo.iterdir():
                    total += 1
        return (SKILLS_DIR.stat().st_mtime, total)
    except Exception:
        return (0.0, 0)


_skills_cache: dict = {"key": None, "ts": 0.0, "result": None}
_skills_lock = Lock()
_SKILLS_TTL_SEG = 60.0


def _carregar_skills() -> list[dict]:
    if not SKILLS_DIR.exists() or not SKILLS_DIR.is_dir():
        return []
    skills: list[dict] = []

    for grupo_dir in sorted(SKILLS_DIR.iterdir()):
        if not grupo_dir.is_dir():
            continue
        grupo_nome = grupo_dir.name
        oculto = grupo_nome in GRUPOS_OCULTOS

        # Padrao A: SKILL.md direto no grupo (ex: triagem/SKILL.md, arquivamento/SKILL.md)
        skill_md_direto = grupo_dir / "SKILL.md"
        if skill_md_direto.exists():
            try:
                texto = skill_md_direto.read_text(encoding="utf-8", errors="replace")
            except Exception:
                texto = ""
            label, descricao = _extrair_meta(texto, _label_padrao(grupo_nome))
            skills.append({
                "slug": grupo_nome,
                "label": label,
                "descricao": descricao,
                "grupo": grupo_nome,
                "oculto": oculto,
                "caminho": str(skill_md_direto.relative_to(SKILLS_DIR)).replace("\\", "/"),
            })
            continue

        # Padrao B: grupo/<sub>/SKILL.md
        for sub in sorted(grupo_dir.iterdir()):
            if not sub.is_dir():
                continue
            skill_md = sub / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                texto = skill_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            slug = f"{grupo_nome}/{sub.name}"
            label, descricao = _extrair_meta(texto, _label_padrao(sub.name))
            skills.append({
                "slug": slug,
                "label": label,
                "descricao": descricao,
                "grupo": grupo_nome,
                "oculto": oculto,
                "caminho": str(skill_md.relative_to(SKILLS_DIR)).replace("\\", "/"),
            })

    return skills


def listar_skills(incluir_ocultas: bool = False) -> list[dict]:
    """Lista skills do workspace (cache 60s).

    Args:
        incluir_ocultas: se True, retorna tambem skills de _shared/_archive.
    """
    sig = _signature_skills()
    now = time.monotonic()
    with _skills_lock:
        if (
            _skills_cache["result"] is not None
            and _skills_cache["key"] == sig
            and (now - _skills_cache["ts"]) < _SKILLS_TTL_SEG
        ):
            cached = _skills_cache["result"]
        else:
            cached = _carregar_skills()
            _skills_cache["result"] = cached
            _skills_cache["ts"] = now
            _skills_cache["key"] = sig

    if incluir_ocultas:
        return cached
    return [s for s in cached if not s["oculto"]]


def listar_grupos() -> list[str]:
    """Grupos descobertos no workspace (exclui ocultos)."""
    grupos: list[str] = []
    for s in listar_skills():
        if s["grupo"] not in grupos:
            grupos.append(s["grupo"])
    return grupos


def invalidar_cache_skills() -> None:
    with _skills_lock:
        _skills_cache["result"] = None
        _skills_cache["ts"] = 0.0
        _skills_cache["key"] = None
