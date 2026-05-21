"""Planejamento pré-elaboração — Claude analisa o PAJ e propõe estrutura da peça.

Antes do Claude executar a elaboração completa (que gera DOCX/PDF), ele
primeiro gera um PLANO em JSON estruturado. JP revisa, corrige se preciso,
e SÓ depois aprova → elaboração real.

Plano:
{
  "tipo_atuacao": "RECURSO" | "DESPACHO_INTERNO" | "ARQUIVAMENTO" | "NAO_ATUAR",
  "tipo_peca": "embargos_declaracao_tnu" | "agravo_interno_tnu" | "resp" | "re" |
               "despacho_arquivamento" | "despacho_acompanhamento" | "nenhuma",
  "fundamentos_principais": ["..."],
  "fontes_citadas": [{"tipo": "decisao|juris|regimento|lei", "ref": "..."}],
  "razoes_resumo": "100-300 chars",
  "raciocinio_completo": "texto livre",
  "confianca": "alta" | "media" | "baixa",
  "alertas": ["..."]
}
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

from config import ENTRADA_DIR
from services.chat_service import CLAUDE_CMD


def _ler_resumo_curto(pasta: Path) -> str:
    f = pasta / "resumo_curto.md"
    if not f.exists():
        return ""
    try:
        return f.read_text(encoding="utf-8")
    except Exception:
        return ""


def _ler_decisao_recente(pasta: Path, limite: int = 4000) -> str:
    """Pega trecho da decisão/acórdão mais recente."""
    for sub in ("peças", "pecas", "decisoes_superiores"):
        d = pasta / sub
        if not d.exists():
            continue
        arqs = sorted(
            [f for f in d.iterdir() if f.suffix == ".txt"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if arqs:
            try:
                return arqs[0].read_text(encoding="utf-8", errors="replace")[:limite]
            except Exception:
                continue
    return ""


def _montar_prompt(paj: str, meta: dict, resumo: str, decisao: str) -> str:
    det = meta.get("detalhes_sisdpu", {}) or {}
    return f"""Você é assistente jurídico DPU (TNU+STJ). JP é Defensor Categoria Especial.

TAREFA: produza um PLANO ESTRUTURADO em JSON para o PAJ abaixo. NÃO escreva a peça ainda. Só analise e proponha estrutura. JP vai revisar antes de você executar.

PAJ: {paj}
Assistido: {det.get('assistido', '?')}
Classificação automática: {meta.get('classificacao', '?')}
Foro: {meta.get('foro_detectado', '?')}
Processo judicial: {meta.get('processo_judicial', '?')}

RESUMO:
{resumo[:2000]}

TRECHO DA DECISÃO MAIS RECENTE:
{decisao[:3000]}

REGRAS:
- JP atua em TNU + STJ previdenciário, Cat. Especial
- TNU/STJ/JEF: sem dobra DPU, dias úteis, +10d ciência ficta e-Proc
- Decisão monocrática Relator → cabe agravo interno
- Decisão monocrática Presidente TNU → IRRECORRÍVEL na maioria
- Decisão colegiada TNU → ED só se omissão/contradição/obscuridade; analisar REsp/RE
- Não generalizar — estudar o caso

CLASSES VÁLIDAS:
- tipo_atuacao: RECURSO | DESPACHO_INTERNO | ARQUIVAMENTO | NAO_ATUAR
- tipo_peca: embargos_declaracao_tnu | embargos_declaracao_stj | agravo_interno_tnu | agravo_interno_stj | resp | aresp | re | memoriais | embargos_divergencia_stj | despacho_arquivamento | despacho_acompanhamento | nenhuma

Responda APENAS com JSON puro (sem markdown, sem texto antes/depois):
{{
  "tipo_atuacao": "...",
  "tipo_peca": "...",
  "fundamentos_principais": ["fundamento 1 curto", "fundamento 2", "..."],
  "fontes_citadas": [
    {{"tipo": "decisao", "ref": "Decisão monocrática do Rel. X em DD/MM/YYYY"}},
    {{"tipo": "juris", "ref": "Tema 359/TNU - Neian Milhomem Cruz - 25/06/2025"}},
    {{"tipo": "regimento", "ref": "RITNU art. 16"}},
    {{"tipo": "lei", "ref": "Lei 10.259/2001 art. 14"}}
  ],
  "razoes_resumo": "explicação curta da escolha (100-300 chars)",
  "raciocinio_completo": "texto livre até 1000 chars",
  "confianca": "alta|media|baixa",
  "alertas": ["alerta opcional", "ex: Prazo apertado", "ex: Tese ainda não pacificada"]
}}"""


async def planejar_elaboracao(paj_norm: str, timeout: int = 120) -> dict:
    """Chama Claude CLI com prompt de planejamento. Retorna JSON parseado."""
    pasta = ENTRADA_DIR / paj_norm
    meta_f = pasta / "metadata.json"
    if not meta_f.exists():
        return {"ok": False, "erro": f"metadata.json não encontrado em {pasta}"}

    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "erro": f"erro lendo metadata: {e}"}

    paj_original = meta.get("paj", paj_norm)
    resumo = _ler_resumo_curto(pasta)
    decisao = _ler_decisao_recente(pasta)
    prompt = _montar_prompt(paj_original, meta, resumo, decisao)

    cmd = [CLAUDE_CMD, "-p", prompt, "--output-format", "text"]

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(pasta),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "erro": "Timeout chamando Claude CLI"}
    except FileNotFoundError:
        return {"ok": False, "erro": f"Claude CLI não encontrado: {CLAUDE_CMD}"}
    except Exception as e:
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "erro": f"Claude exit {proc.returncode}: {proc.stderr[-500:]}",
            "stdout": proc.stdout[-500:],
        }

    plano = _extrair_json(proc.stdout)
    if not plano:
        return {
            "ok": False,
            "erro": "Claude não retornou JSON parseável",
            "resp_raw": proc.stdout[-1000:],
        }

    return {"ok": True, "plano": plano, "resp_raw": proc.stdout[:300]}


def _extrair_json(s: str) -> dict | None:
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # Tenta limpar
        clean = re.sub(r",\s*([}\]])", r"\1", m.group(0))  # trailing commas
        try:
            return json.loads(clean)
        except Exception:
            return None


def salvar_plano(paj_norm: str, plano: dict, fonte: str = "claude") -> Path:
    """Persiste plano aprovado em disco pra uso futuro pelo executor."""
    pasta = ENTRADA_DIR / paj_norm
    pasta.mkdir(parents=True, exist_ok=True)
    f = pasta / "plano_elaboracao.json"
    payload = {
        "plano": plano,
        "fonte": fonte,
        "salvo_em": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def carregar_plano(paj_norm: str) -> dict | None:
    pasta = ENTRADA_DIR / paj_norm
    f = pasta / "plano_elaboracao.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
