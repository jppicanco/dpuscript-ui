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
import os
import re
import subprocess
from pathlib import Path

from config import ENTRADA_DIR, DPUSCRIPT_DIR
from services.chat_service import CLAUDE_CMD

REGRAS_ATUACAO_FILE = DPUSCRIPT_DIR / "memory" / "regras_atuacao.md"


def _carregar_regras_atuacao() -> str:
    """Lê regras aprendidas (editadas por JP). Carregadas a cada chamada
    — JP atualiza arquivo e próxima chamada já considera."""
    if not REGRAS_ATUACAO_FILE.exists():
        return ""
    try:
        return REGRAS_ATUACAO_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""


def _env_sem_claudecode() -> dict:
    """Clone do env sem CLAUDECODE — evita 'Claude inside another Claude' error."""
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)
    return env


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


def _listar_arquivos_pecas(pasta: Path) -> str:
    """Lista nomes de arquivos .txt em peças/ e decisoes_superiores/ para o Claude poder
    referenciar no campo decisao_recorrida_arquivo / fontes_auxiliares.arquivo."""
    linhas = []
    for sub in ("peças", "pecas", "decisoes_superiores"):
        d = pasta / sub
        if not d.exists():
            continue
        arqs = sorted([f.name for f in d.iterdir() if f.suffix == ".txt"])
        if arqs:
            linhas.append(f"  {sub}/:")
            for a in arqs:
                linhas.append(f"    - {a}")
    return "\n".join(linhas) if linhas else "  (nenhum arquivo de peça/decisão local)"


def _montar_prompt(paj: str, meta: dict, resumo: str, decisao: str, arquivos_locais: str = "") -> str:
    det = meta.get("detalhes_sisdpu", {}) or {}
    regras = _carregar_regras_atuacao()
    bloco_regras = f"\n\nREGRAS APRENDIDAS (correções acumuladas do Defensor — RESPEITE):\n{regras}\n" if regras else ""
    return f"""Você é assistente jurídico da DPU (TNU+STJ). JP é Defensor Cat. Especial.{bloco_regras}

TAREFA: analisar o PAJ e propor um PLANO DE ATUAÇÃO. JP vai revisar antes de executar.

CONTEXTO DO PAJ:
- PAJ: {paj}
- Assistido: {det.get('assistido', '?')}
- Classificação automática (heurística pode estar errada): {meta.get('classificacao', '?')}
- Foro: {meta.get('foro_detectado', '?')}
- Processo judicial: {meta.get('processo_judicial', '?')}

RESUMO DA SITUAÇÃO:
{resumo[:2000]}

TRECHO DA DECISÃO MAIS RECENTE BAIXADA:
{decisao[:3500]}

ARQUIVOS LOCAIS DA PASTA DO PAJ (use estes nomes em decisao_recorrida_arquivo e fontes_auxiliares.arquivo):
{arquivos_locais}

REGRAS:
- JP atua TNU + STJ previdenciário, Cat. Especial.
- TNU/STJ/JEF: SEM dobra DPU. Dias úteis. +10d ciência ficta e-Proc.
- Decisão monocrática do RELATOR → pode caber agravo interno.
- Decisão monocrática do PRESIDENTE TNU → geralmente IRRECORRÍVEL.
- Decisão COLEGIADA TNU → ED só se omissão/contradição/obscuridade; pode caber REsp/RE.
- Decisão de mera ADMISSÃO de PUIL = neutra (processo continua, não é vitória nem derrota).
- Decisão de PROVIMENTO com restituição = VITÓRIA.
- Decisão de DESPROVIMENTO/NÃO-CONHECIMENTO = DERROTA.
- NUNCA chame de "favorável" se for só admissão/conhecimento/distribuição/conversão em diligência. Use "neutra".

ATUAÇÃO POSSÍVEL:
- RECURSO + peça específica (ED, agravo interno, REsp, AREsp, RE, memoriais, embargos divergência)
- DESPACHO_INTERNO + tipo (despacho_acompanhamento, despacho_arquivamento) — só registro no SISDPU, sem peça judicial
- ARQUIVAMENTO + despacho_arquivamento (vitória final ou irrecorribilidade definitiva)
- NAO_ATUAR + nenhuma — só ciência

IMPORTANTE — análise narrativa:
- Em vez de fundamentos em bullets, escreva UMA ANÁLISE NARRATIVA CORRIDA em "analise_completa"
- Explique o que aconteceu, por que escolheu essa atuação, qual é o caminho recomendado
- Português jurídico claro, mas sem floreio
- 500-2000 caracteres
- Se for só admissão de PUIL, diga isso explicitamente — não confunda com vitória

DECISÃO RECORRIDA (campo separado):
- decisao_recorrida_descricao: 1-2 frases identificando a decisão sendo analisada
  (ex: "Despacho do Presidente TNU em DD/MM/AAAA admitindo o PUIL e determinando distribuição.")
- decisao_recorrida_arquivo: nome do arquivo .txt na pasta peças/ ou decisoes_superiores/
  que CONTÉM essa decisão (apenas o nome, ex: "2026-05-04_ev8_DESPADEC1.txt"). Vazio se não achar arquivo claro.

OUTRAS FONTES (auxiliares):
- Lista de outras fontes citadas (jurisprudência, regimento, lei)
- Cada uma com `tipo`, `ref`, e `arquivo` (se puder apontar arquivo local)"""


PLANO_JSON_SCHEMA = {
    "type": "object",
    "required": [
        "tipo_atuacao", "tipo_peca",
        "decisao_recorrida_descricao", "decisao_recorrida_arquivo",
        "analise_completa", "fontes_auxiliares",
        "confianca",
    ],
    "properties": {
        "tipo_atuacao": {
            "type": "string",
            "enum": ["RECURSO", "DESPACHO_INTERNO", "ARQUIVAMENTO", "NAO_ATUAR"],
        },
        "tipo_peca": {
            "type": "string",
            "enum": [
                "embargos_declaracao_tnu", "embargos_declaracao_stj",
                "agravo_interno_tnu", "agravo_interno_stj",
                "resp", "aresp", "re", "memoriais", "embargos_divergencia_stj",
                "despacho_arquivamento", "despacho_acompanhamento", "nenhuma",
            ],
        },
        # Decisão sob análise (destacada, com arquivo clicável)
        "decisao_recorrida_descricao": {
            "type": "string",
            "description": "1-2 frases identificando a decisão sendo analisada",
        },
        "decisao_recorrida_arquivo": {
            "type": "string",
            "description": "Nome do .txt na pasta peças/ ou decisoes_superiores/ contendo a decisão. Vazio se não achar.",
        },
        # Análise narrativa única (substitui bullets de fundamentos + razões)
        "analise_completa": {
            "type": "string",
            "description": "Texto narrativo corrido (500-2000 chars) explicando o caso e a atuação proposta",
        },
        # Fontes auxiliares (não a decisão principal)
        "fontes_auxiliares": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["juris", "regimento", "lei", "decisao_paradigma"]},
                    "ref": {"type": "string"},
                    "arquivo": {
                        "type": "string",
                        "description": "Nome do arquivo local que contém a fonte (se houver). Vazio se externa.",
                    },
                },
                "required": ["tipo", "ref"],
            },
        },
        "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
        "alertas": {"type": "array", "items": {"type": "string"}},
    },
}


async def planejar_elaboracao(
    paj_norm: str,
    timeout: int = 180,
    feedback_jp: str = "",
) -> dict:
    """Chama Claude CLI com prompt de planejamento. Retorna JSON parseado.

    Args:
        paj_norm: PAJ no formato YYYY-UUU-NNNNN
        timeout: máximo de segundos
        feedback_jp: instrução adicional do JP pra refazer com observação
                     (ex: "Decisão é só admissão, não vitória. Refaça.")
    """
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
    arquivos_locais = _listar_arquivos_pecas(pasta)
    prompt = _montar_prompt(paj_original, meta, resumo, decisao, arquivos_locais)
    if feedback_jp.strip():
        prompt += (
            "\n\nOBSERVAÇÃO IMPORTANTE DO DEFENSOR (refaça considerando isto):\n"
            f"\"\"\"{feedback_jp.strip()[:500]}\"\"\""
        )

    # --print: modo não-interativo
    # --output-format json: retorna wrapper {"result": ..., "is_error": ...}
    # --json-schema: força resposta seguir esquema
    # --setting-sources user: ignora CLAUDE.md project (que confunde com tom conversacional)
    # --append-system-prompt: reforça que é tarefa de extração
    cmd = [
        CLAUDE_CMD,
        "--print",
        "--output-format", "json",
        "--json-schema", json.dumps(PLANO_JSON_SCHEMA),
        "--setting-sources", "user",
        "--append-system-prompt",
        "Você é executor de tarefa estruturada. NÃO inicie conversa nem se "
        "apresente. Apenas analise o caso e responda com JSON puro seguindo o "
        "schema. Sem markdown, sem texto antes/depois.",
        prompt,
    ]

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
            env=_env_sem_claudecode(),
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

    # Com --output-format json, Claude retorna wrapper: {"type":"result","result":"<string com JSON>",...}
    plano = _extrair_plano_da_resposta(proc.stdout)
    if not plano:
        return {
            "ok": False,
            "erro": "Claude não retornou JSON parseável",
            "resp_raw": proc.stdout[-2000:],
        }

    return {"ok": True, "plano": plano, "resp_raw": proc.stdout[:300]}


def _extrair_plano_da_resposta(out: str) -> dict | None:
    """Extrai plano (JSON estruturado) da resposta do Claude.

    Com --output-format json + --json-schema, stdout vem como:
      {"type":"result", "result":"", "structured_output": <dict>, ...}
    O plano fica em `structured_output`. `result` pode estar vazio.

    Sem --json-schema, plano fica como string em `result`.
    """
    try:
        wrapper = json.loads(out.strip())
        if isinstance(wrapper, dict):
            # 1. structured_output (preferido — quando --json-schema valida)
            so = wrapper.get("structured_output")
            if isinstance(so, dict) and so:
                return so
            # 2. result como dict
            result = wrapper.get("result", "")
            if isinstance(result, dict):
                return result
            # 3. result como string contendo JSON
            if isinstance(result, str) and result.strip():
                parsed = _extrair_json(result)
                if parsed:
                    return parsed
    except json.JSONDecodeError:
        pass
    # Fallback: tenta extrair JSON em qualquer lugar do output
    return _extrair_json(out)


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
