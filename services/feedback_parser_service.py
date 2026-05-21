"""Parser de feedback livre via LLM (Grok M4).

JP escreve crítica em texto natural sobre um PAJ. Grok extrai estrutura:
{classif_correta, razao_curta, padrao_regex (opcional)}.

UI mostra proposta. JP confirma com 1 clique → cria regra de fato via /api/correcao.

Backend usa Grok 4.3 fast no M4 via Hermes (não Claude — não precisa profundidade,
só extração estruturada).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess

M4_HOST = "macmini@192.168.0.102"
HERMES_PROFILE = "jarbas-dpu"

CLASSIFS_VALIDAS = [
    "DECISAO_MONOCRATICA_RELATOR_TNU",
    "DECISAO_MONOCRATICA_RELATOR_STJ",
    "DECISAO_MONOCRATICA_PRESIDENTE_TNU",
    "DECISAO_MONOCRATICA_PRESIDENTE_STJ",
    "DECISAO_COLEGIADA_TNU",
    "DECISAO_COLEGIADA_STJ",
    "DECISAO_COLEGIADA_TNU_PROVIMENTO",
    "DECISAO_COLEGIADA_STJ_PROVIMENTO",
    "DECISAO_MERITO_TNU_PENDENTE",
    "DECISAO_MERITO_STJ_PENDENTE",
    "AGUARDA_JULGAMENTO_TNU",
    "AGUARDA_JULGAMENTO_STJ",
    "INCLUSAO_EM_PAUTA",
    "ARQUIVADO_VITORIA_PROVIMENTO",
    "ARQUIVADO_TRAMITE_INTERNO",
    "RETORNO_AUTOMATICO_1ANO",
    "VISTA_MP",
    "RETORNO_ASSISTIDO",
    "INTIMACAO_SIMPLES_CIENCIA",
    "OUTRO",
]


def _montar_prompt(paj: str, classif_atual: str, msg_jp: str, contexto: str = "") -> str:
    return f"""Você é assistente de extração estruturada. JP (Defensor Público Federal, atua em TNU+STJ previdenciário) escreveu crítica sobre a classificação automática de um PAJ. Sua tarefa: extrair em JSON.

PAJ: {paj}
Classificação automática atual: {classif_atual}
Contexto da decisão (trecho): {contexto[:1000]}

Mensagem do JP:
\"\"\"{msg_jp}\"\"\"

Classes válidas:
{chr(10).join("- " + c for c in CLASSIFS_VALIDAS)}

Responda APENAS em JSON puro (sem markdown, sem texto antes/depois):
{{
  "classif_correta": "<uma das classes válidas>",
  "razao_curta": "<max 150 chars>",
  "padrao_regex_sugerido": "<regex opcional que identifica padrão no texto da decisão; deixe vazio se inferir só caso específico>",
  "confianca": "alta" | "media" | "baixa"
}}

Critério da regex: só sugerir se JP descreveu padrão GERAL (ex: "Presidente da TNU não cabe agravo"). Se descreveu só caso específico, deixar vazia."""


async def parsear_feedback(
    paj: str,
    classif_atual: str,
    mensagem_jp: str,
    contexto_decisao: str = "",
    timeout: int = 60,
) -> dict:
    """Envia mensagem JP pro Grok M4 e retorna estrutura parseada.

    Returns: {
        "ok": bool,
        "classif_correta": str,
        "razao_curta": str,
        "padrao_regex_sugerido": str,
        "confianca": str,
        "resp_raw": str,
        "erro": str | None,
    }
    """
    prompt = _montar_prompt(paj, classif_atual, mensagem_jp, contexto_decisao)
    cmd = [
        "ssh",
        M4_HOST,
        f"hermes chat --profile {HERMES_PROFILE} -q {json.dumps(prompt)} --max-turns 1 2>&1",
    ]
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        out = proc.stdout or ""
        parsed = _extrair_json(out)
        if not parsed:
            return {
                "ok": False,
                "erro": "Grok não retornou JSON parseável",
                "resp_raw": out[-1000:],
            }
        # Valida
        classif = parsed.get("classif_correta", "")
        if classif not in CLASSIFS_VALIDAS:
            return {
                "ok": False,
                "erro": f"Classif inválida: {classif}",
                "resp_raw": out[-500:],
                **parsed,
            }
        return {
            "ok": True,
            "classif_correta": classif,
            "razao_curta": parsed.get("razao_curta", "")[:200],
            "padrao_regex_sugerido": parsed.get("padrao_regex_sugerido", ""),
            "confianca": parsed.get("confianca", "media"),
            "resp_raw": out[-500:],
            "erro": None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "erro": "Timeout chamando Grok M4"}
    except Exception as e:
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"}


def _extrair_json(s: str) -> dict | None:
    """Tenta extrair primeiro objeto JSON do texto."""
    # Procura padrão {...}
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # Tenta limpar quebras de linha que podem corromper o JSON
        clean = re.sub(r"\s+", " ", m.group(0))
        try:
            return json.loads(clean)
        except Exception:
            return None
