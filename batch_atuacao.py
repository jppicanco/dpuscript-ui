#!/usr/bin/env python3
"""Batch de atuação por PAJ — 2 estágios, leve e com medição de tokens.

PROBLEMA da v1 (corrigido): injetava o PROMPT_MAX inteiro (até 127k tokens),
em modo agêntico (cwd=workspace + bypassPermissions → loop de ferramentas
reenviando contexto), Opus em tudo, 66 processos frios (zero reuso de cache).
Estourava a cota de 5h em <20 PAJs.

v2 — desenho leve (inspirado no planejar_service):

  ESTÁGIO 1 — DECISÃO (default, `--stage decisao`):
    - Opus, 1 chamada só, SEM ferramentas agênticas (--setting-sources user,
      --output-format json --json-schema). cwd = pasta do PAJ.
    - Contexto MÍNIMO: metadata + resumo_curto + últimas 3 movimentações +
      decisão mais recente truncada (head+tail). NÃO o PROMPT_MAX.
    - regras_atuacao.md injetadas (decisão crítica respeita as correções do JP).
    - Decide tipo (DESPACHO/ARQUIVAMENTO/RECURSO/NAO_ATUAR) e, p/ não-recurso,
      já redige a movimentação pronta. Custo ~5-10k tokens/PAJ.
    - RECURSO fica "recurso_pendente" (peça é estágio 2, gated).

  ESTÁGIO 2 — RECURSO (`--stage recurso`):
    - Opus agêntico (skills + anti-alucinação + DOCX) só nos recurso_pendente.
    - Caro; rodar separado e com supervisão de cota.

Mede tokens/custo por PAJ (modelUsage do wrapper) e acumula em batch_status.json
pra dar pra projetar consumo e parar com folga.

NÃO usa API paga (força OAuth). NÃO protocola nem movimenta SISDPU.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil as _shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from claude_runner import run_claude, reap_orphans

WORKSPACE = Path(os.getenv("DPU_WORKSPACE", r"E:\DPU\dpu-workspace"))
ENTRADA = WORKSPACE / "Entrada" / "dpuscript"
REGRAS_FILE = WORKSPACE / "dpuscript" / "memory" / "regras_atuacao.md"
TEMPLATE_DOCX = os.getenv(
    "FORMATAR_PECA_TEMPLATE",
    r"D:\DPU\MODELO ARE 1446634 - agravo interno hanseníase - PAJ 2023.040.06077.docx",
)
CLAUDE_CMD = (os.getenv("CLAUDE_CLI") or _shutil.which("claude")
              or r"C:\Users\JP\AppData\Roaming\npm\claude.CMD")

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "batch_atuacao.log"
STATUS_FILE = BASE_DIR / "batch_status.json"

# Truncagens do contexto mínimo
MAX_MOV_DESC = 800       # chars por descrição de movimentação
N_MOVS = 3               # últimas N movimentações
DECISAO_HEAD = 5000      # chars do início da decisão recente
DECISAO_TAIL = 3000      # chars do fim (dispositivo)
TIMEOUT_DECISAO = 240    # 4 min — decisão é 1 shot
TIMEOUT_RECURSO = 1500   # 25 min — recurso agêntico

_log_lock = threading.Lock()
_acc_lock = threading.Lock()
_status: dict = {}
_acumulado = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
              "custo_usd": 0.0, "pajs": 0}


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _salvar_status(paj: str, **campos) -> None:
    with _acc_lock:
        _status.setdefault(paj, {})
        _status[paj].update(campos)
        _status[paj]["atualizado_em"] = dt.datetime.now().isoformat(timespec="seconds")
        snapshot = {"_acumulado": dict(_acumulado), "pajs": dict(_status)}
        try:
            STATUS_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass


def _ler(p: Path, limite: int | None = None) -> str:
    if not p.exists():
        return ""
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
        return t[:limite] if limite else t
    except Exception:
        return ""


def _ler_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


_DATA_NOME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})_ev(\d+)")
# Tipos que carregam o conteúdo decisório (priorizados)
_TIPOS_DECISORIOS = ("ACOR", "DESPADEC", "DECIS", "SENT", "VOTO", "EXTRATOATA", "QUESTORDEM")
N_DOCS_DECISAO = 3        # nº de documentos recentes a alimentar
MAX_DECISAO_TOTAL = 45000  # teto de chars total (custo controlado)


def _ordem_doc(f: Path) -> tuple:
    """Chave de ordenação: (data, evento) extraídos do NOME (não mtime — todos
    têm o mesmo mtime de sync). Sem data no nome → vai pro fim."""
    m = _DATA_NOME_RE.search(f.name)
    if m:
        ano, mes, dia, ev = m.groups()
        return (1, ano + mes + dia, int(ev))
    return (0, "00000000", 0)


def _decisao_recente(pasta: Path) -> tuple[str, str]:
    """Concatena os N documentos MAIS RECENTES (por data no nome), priorizando
    os decisórios. Retorna (texto, nomes). Teto de chars pra controlar custo;
    documento gigante (OCR) é truncado head+tail."""
    cands = []
    for sub in ("decisoes_superiores", "peças", "pecas"):
        d = pasta / sub
        if d.exists():
            cands += [f for f in d.iterdir() if f.is_file() and f.suffix == ".txt"]
    if not cands:
        return "", ""

    # Separa: nomes COM data (TNU/e-Proc, confiável) vs SEM data (STJ por hash).
    dated = [f for f in cands if _DATA_NOME_RE.search(f.name)]
    undated = [f for f in cands if not _DATA_NOME_RE.search(f.name)]  # STJ_<hash>
    dated.sort(key=_ordem_doc, reverse=True)
    decisorios = [f for f in dated if any(t in f.name.upper() for t in _TIPOS_DECISORIOS)]

    escolhidos: list[Path] = []
    # 1. TODOS os STJ sem data (poucos por PAJ) — não dá pra ordenar por nome,
    #    o modelo lê as datas internas. Sempre incluir pra não perder decisão STJ.
    escolhidos += undated
    # 2. Documento decisório TNU mais recente (acórdão/despacho/sentença)
    if decisorios:
        escolhidos.append(decisorios[0])
    # 3. Documentos TNU mais recentes (qualquer tipo) como contexto
    for f in dated[:N_DOCS_DECISAO]:
        if f not in escolhidos:
            escolhidos.append(f)
    # dedup preservando ordem de inclusão (STJ → decisório → recentes)
    vistos = set()
    escolhidos = [f for f in escolhidos if not (f in vistos or vistos.add(f))]

    partes, nomes, total = [], [], 0
    for f in escolhidos:
        txt = _ler(f)
        if not txt.strip():
            continue
        # trunca documento individual gigante (OCR)
        if len(txt) > 25000:
            txt = txt[:15000] + "\n\n[...TRECHO OMITIDO (documento extenso)...]\n\n" + txt[-8000:]
        if total + len(txt) > MAX_DECISAO_TOTAL:
            txt = txt[: max(0, MAX_DECISAO_TOTAL - total)]
        if not txt:
            break
        partes.append(f"--- {f.name} ---\n{txt}")
        nomes.append(f.name)
        total += len(txt)
    return "\n\n".join(partes), ", ".join(nomes)


def montar_prompt_decisao(paj_norm: str, pasta: Path) -> str:
    meta = _ler_json(pasta / "metadata.json")
    det = meta.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes", []) or []
    movs = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0), reverse=True)[:N_MOVS]
    movs_txt = "\n".join(
        f"  [{m.get('data','?')}] {(m.get('descricao','') or '')[:MAX_MOV_DESC]}"
        for m in movs
    ) or "  (sem movimentações no metadata)"
    resumo = _ler(pasta / "resumo_curto.md", 4000)
    decisao, decisao_arq = _decisao_recente(pasta)
    regras = _ler(REGRAS_FILE)
    bloco_regras = f"\n\n## REGRAS APRENDIDAS (correções do Defensor — RESPEITE)\n{regras}\n" if regras.strip() else ""

    return f"""Você é o assistente jurídico da DPU. JP é Defensor Público Federal Cat. Especial, atua TNU + STJ (previdenciário).

Sua tarefa: DECIDIR a atuação deste PAJ. Esta é a decisão mais importante — ela define todo o resto. Leia de trás pra frente: a movimentação/decisão MAIS RECENTE geralmente já determina o que fazer.{bloco_regras}

## TIPOS DE ATUAÇÃO
- **DESPACHO** — não há peça a fazer: intimação de audiência (registrar ciência), mero expediente, vista ao MPF, aguardando distribuição/julgamento, abertura de PAJ, decurso, OU decisão de mera ADMISSÃO/conhecimento/distribuição (NEUTRA, nem vitória nem derrota), OU a DPU é parte vencedora e o adverso recorreu (só acompanhar).
- **ARQUIVAMENTO** — (a) irrecorribilidade (ex: monocrática do Presidente da TNU), (b) inviabilidade de mérito (juris consolidada contra sem distinguishing), (c) VITÓRIA já obtida e cumprida.
- **RECURSO** — decisão DESFAVORÁVEL ao assistido, com recurso cabível e viável (ED, agravo interno, REsp, AREsp, RE, memoriais, embargos divergência). NÃO marque recurso se a DPU já interpôs o recurso cabível (verifique nas movimentações) ou se a DPU é a vencedora.
- **NAO_ATUAR** — nada a fazer.

## REGRAS PROCESSUAIS
- TNU/STJ/JEF: SEM dobra DPU. Dias úteis. +10d ciência ficta e-Proc.
- Monocrática do RELATOR desfavorável → agravo interno. Monocrática do PRESIDENTE TNU → em regra IRRECORRÍVEL (no máx ED). Colegiada → ED só se vício; pode caber REsp/RE.
- NUNCA invente citação/jurisprudência/número.

## CONTEXTO DO PAJ {paj_norm}
- Assistido: {meta.get('assistido_caixa','?')}
- Ofício: {meta.get('oficio_caixa','?')}
- Foro detectado: {meta.get('foro_detectado','?')}
- Classificação automática (heurística — pode estar errada): {meta.get('classificacao','?')}
- Processo judicial: {meta.get('processo_judicial','?')}

### RESUMO (gerado pelo pipeline)
{resumo}

### ÚLTIMAS {N_MOVS} MOVIMENTAÇÕES (ORDEM CRONOLÓGICA REAL — mais recente primeiro)
ESTA é a verdade cronológica do processo. Use-a pra saber qual é o evento/decisão MAIS RECENTE.
{movs_txt}

### DOCUMENTOS RELEVANTES NA PASTA ({decisao_arq or 'nenhum'})
ATENÇÃO: os documentos abaixo PODEM NÃO estar em ordem cronológica (alguns vêm sem data no nome, ex: STJ). Use as DATAS INTERNAS de cada um + as movimentações acima pra identificar qual é a decisão mais recente e relevante.
{decisao or '(sem documento de decisão baixado)'}

## SAÍDA
Responda SOMENTE o JSON do schema. Para DESPACHO/ARQUIVAMENTO/NAO_ATUAR, o campo `movimentacao` deve trazer o TEXTO pronto pra colar no SISDPU. Para RECURSO, `movimentacao` traz a movimentação de juntada da peça (a peça em si será redigida depois) e `precisa_aprofundar`=true.
"""


DECISAO_SCHEMA = {
    "type": "object",
    "required": ["tipo", "peca_tipo", "fundamento_decisao", "resumo",
                 "o_que_fazer", "movimentacao", "confianca"],
    "properties": {
        "tipo": {"type": "string", "enum": ["DESPACHO", "ARQUIVAMENTO", "RECURSO", "NAO_ATUAR"]},
        "peca_tipo": {"type": "string"},
        "prazo": {"type": "string"},
        "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
        "fundamento_decisao": {"type": "string",
            "description": "1-3 frases: POR QUE este tipo (a decisão crítica que JP vai auditar)"},
        "resumo": {"type": "string", "description": "2-4 frases: o que é o PAJ e o que aconteceu"},
        "o_que_fazer": {"type": "string", "description": "1-3 frases diretas pro Defensor"},
        "alertas": {"type": "string"},
        "movimentacao": {"type": "string", "description": "texto pronto pro SISDPU"},
        "precisa_aprofundar": {"type": "boolean",
            "description": "true se for RECURSO (peça precisa estágio 2)"},
    },
}


CONFIG_LIMPO = Path(os.getenv("TEMP", r"C:\Users\JP\AppData\Local\Temp")) / "dpu_clean_cfg"
_HOME_CRED = Path.home() / ".claude" / ".credentials.json"


def _setup_config_limpo() -> None:
    """Cria um CLAUDE_CONFIG_DIR limpo com SÓ a credencial (auth), sem plugins,
    hooks (claude-mem/caveman) nem CLAUDE.md global. Isso elimina ~30-40k tokens
    de contexto por chamada — o real culpado do estouro de cota (rate limit conta
    tokens, e os hooks de SessionStart injetavam memória em toda chamada fria)."""
    CONFIG_LIMPO.mkdir(parents=True, exist_ok=True)
    if _HOME_CRED.exists():
        try:
            _shutil.copy2(_HOME_CRED, CONFIG_LIMPO / ".credentials.json")
        except Exception:
            pass


def _env(pasta: Path) -> dict:
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
              "CLAUDE_PROJECT_DIR", "CLAUDE_AGENT_RUN_ID",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["CLAUDE_CONFIG_DIR"] = str(CONFIG_LIMPO)  # config limpa: sem plugins/hooks
    env["FORMATAR_PECA_SAIDA_DIR"] = str(pasta)
    env["FORMATAR_PECA_TEMPLATE"] = TEMPLATE_DOCX
    return env


_RATE = ("limitando temporariamente", "rate limit", "rate_limit", "429",
         "too many requests", "overloaded", "quota", "try again later")


def _contabilizar(wrapper: dict, paj: str) -> dict:
    """Extrai modelUsage do wrapper e acumula. Retorna dict de uso do PAJ."""
    uso = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "custo_usd": 0.0}
    mu = wrapper.get("modelUsage", {}) if isinstance(wrapper, dict) else {}
    for _modelo, v in (mu or {}).items():
        uso["input"] += v.get("inputTokens", 0) or 0
        uso["output"] += v.get("outputTokens", 0) or 0
        uso["cache_read"] += v.get("cacheReadInputTokens", 0) or 0
        uso["cache_creation"] += v.get("cacheCreationInputTokens", 0) or 0
        uso["custo_usd"] += v.get("costUSD", 0.0) or 0.0
    with _acc_lock:
        for k in ("input", "output", "cache_read", "cache_creation", "custo_usd"):
            _acumulado[k] += uso[k]
        _acumulado["pajs"] += 1
    return uso


CWD_LIMPO = Path(os.getenv("TEMP", r"C:\Users\JP\AppData\Local\Temp")) / "dpu_decisao_cwd"


def _claude_json(prompt: str, pasta: Path, schema: dict, timeout: int) -> tuple[dict, dict, str]:
    """Chamada leve (sem ferramentas agênticas). Retorna (structured, wrapper, erro).

    Roda de um cwd LIMPO (fora do workspace) + --strict-mcp-config pra NÃO
    carregar .mcp.json/CLAUDE.md/hooks do projeto. Reduz o contexto ao mínimo:
    só o system base do Claude Code + nosso prompt. Sequencial → PAJ 2+
    reaproveita o cache do system base (barato).
    """
    CWD_LIMPO.mkdir(parents=True, exist_ok=True)
    _setup_config_limpo()
    cmd = [
        CLAUDE_CMD, "--print", "--model", "opus",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--strict-mcp-config",          # sem MCP servers
        "--setting-sources", "project",  # não carrega settings/plugins do usuário
        "--tools", "",                  # decisão não usa ferramenta (corta defs built-in)
        "--append-system-prompt",
        "Você é executor de tarefa estruturada. Responda só o JSON do schema, sem markdown.",
    ]
    backoffs = [45, 90, 180]
    for tent in range(len(backoffs) + 1):
        # run_claude: lock global (1 chamada/vez no sistema) + mata árvore + reapa órfãos
        rc, stdout, stderr = run_claude(cmd, prompt, str(CWD_LIMPO), _env(pasta), timeout)
        if rc == -9:
            return {}, {}, "timeout"
        saida = (stdout or "") + " " + (stderr or "")
        low = saida.lower()
        if any(p in low for p in _RATE) and (rc != 0 or "is_error" in low):
            if tent < len(backoffs):
                time.sleep(backoffs[tent])
                continue
            return {}, {}, "rate_limit"
        try:
            wrapper = json.loads(stdout.strip())
        except Exception:
            return {}, {}, f"json inválido (exit {rc}): {(stderr or stdout)[-200:]}"
        structured = wrapper.get("structured_output") if isinstance(wrapper, dict) else None
        if not structured:
            # fallback: result como string com JSON
            r = wrapper.get("result", "") if isinstance(wrapper, dict) else ""
            if isinstance(r, str):
                m = re.search(r"\{.*\}", r, re.DOTALL)
                if m:
                    try:
                        structured = json.loads(m.group(0))
                    except Exception:
                        structured = None
        return (structured or {}), (wrapper if isinstance(wrapper, dict) else {}), ""
    return {}, {}, "rate_limit"


MAX_TOKENS_RUN = int(os.getenv("BATCH_MAX_TOKENS", "1500000"))  # teto de tokens/run (folga p/ correções)


def decidir(paj: str) -> dict:
    # Trava de orçamento: se já consumimos o teto de tokens nesta execução,
    # para de processar (deixa folga de cota pras correções do JP + recursos).
    with _acc_lock:
        consumido = (_acumulado["input"] + _acumulado["output"]
                     + _acumulado["cache_read"] + _acumulado["cache_creation"])
    if consumido >= MAX_TOKENS_RUN:
        return {"paj": paj, "status": "pulado_budget"}

    pasta = ENTRADA / paj
    t0 = dt.datetime.now()
    _salvar_status(paj, status="decidindo", inicio=t0.isoformat(timespec="seconds"))
    prompt = montar_prompt_decisao(paj, pasta)
    structured, wrapper, erro = _claude_json(prompt, pasta, DECISAO_SCHEMA, TIMEOUT_DECISAO)
    uso = _contabilizar(wrapper, paj)
    dur = (dt.datetime.now() - t0).total_seconds()

    if erro:
        log(f"[{paj}] DECISÃO falhou: {erro} ({dur:.0f}s)")
        _salvar_status(paj, status="erro_decisao", erro=erro, **uso)
        return {"paj": paj, "status": "erro", "erro": erro, "uso": uso}

    tipo = structured.get("tipo", "?")
    # status: recurso vira recurso_pendente (estágio 2); resto é done
    novo_status = "recurso_pendente" if tipo == "RECURSO" else "done"
    _escrever_atuacao(paj, pasta, structured, status=novo_status, etapa="decisao")
    # despacho.txt pros casos simples
    if tipo != "RECURSO" and structured.get("movimentacao"):
        try:
            (pasta / "despacho.txt").write_text(structured["movimentacao"], encoding="utf-8")
        except Exception:
            pass

    with _acc_lock:
        custo_tot = _acumulado["custo_usd"]
        pajs_tot = _acumulado["pajs"]
    log(f"[{paj}] DECISÃO={tipo} conf={structured.get('confianca','?')} "
        f"({dur:.0f}s, ${uso['custo_usd']:.3f}, cache_cr={uso['cache_creation']}) "
        f"| acumulado: {pajs_tot} PAJs ${custo_tot:.2f}")
    _salvar_status(paj, status=novo_status, tipo=tipo, duracao_s=round(dur), **uso)
    return {"paj": paj, "status": novo_status, "tipo": tipo, "uso": uso}


def _escrever_atuacao(paj: str, pasta: Path, d: dict, status: str, etapa: str) -> None:
    agora = dt.datetime.now().isoformat(timespec="seconds")
    atuacao = {
        "status": "done" if status == "done" else status,
        "tipo": d.get("tipo", ""),
        "peca_tipo": d.get("peca_tipo", ""),
        "prazo": d.get("prazo", ""),
        "confianca": d.get("confianca", ""),
        "fundamento_decisao": d.get("fundamento_decisao", ""),
        "resumo": d.get("resumo", ""),
        "o_que_fazer": d.get("o_que_fazer", ""),
        "alertas": d.get("alertas", ""),
        "movimentacao": d.get("movimentacao", ""),
        "etapa": etapa,
        "concluido_em": agora,
    }
    try:
        (pasta / "atuacao.json").write_text(json.dumps(atuacao, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    except Exception:
        pass
    # compat UI antiga
    try:
        (pasta / "elaboracao.json").write_text(json.dumps({
            "status": "done" if status in ("done", "recurso_pendente") else status,
            "summary": (f"[{d.get('tipo','')}] {d.get('fundamento_decisao','')}\n\n"
                        f"{d.get('resumo','')}\n\nO que fazer: {d.get('o_que_fazer','')}"),
            "last_action": f"batch_{etapa}", "concluido_em": agora,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
def _env_recurso(pasta: Path) -> dict:
    """Env pro estágio recurso: config COMPLETA (precisa das skills do workspace
    + MCPs de pesquisa bnp/cjf). NÃO usa config limpa nem --tools (é agêntico)."""
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
              "CLAUDE_PROJECT_DIR", "CLAUDE_AGENT_RUN_ID",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["FORMATAR_PECA_SAIDA_DIR"] = str(pasta)
    env["FORMATAR_PECA_TEMPLATE"] = TEMPLATE_DOCX
    return env


def montar_prompt_recurso(paj_norm: str, pasta: Path, atuacao: dict) -> str:
    peca_tipo = atuacao.get("peca_tipo", "") or "(definir)"
    fundamento = atuacao.get("fundamento_decisao", "")
    recorrida, rec_nomes = _decisao_recente(pasta)   # TEXTO da decisão recorrida (não o PROMPT_MAX magro)
    prompt_max = _ler(pasta / "PROMPT_MAX.md", 8000)
    return f"""Você é o assistente jurídico da DPU (TNU + STJ, previdenciário). JP é Defensor Cat. Especial.

A triagem JÁ decidiu: este PAJ é **RECURSO** — {peca_tipo}.
Fundamento: {fundamento}

SUA TAREFA: redigir a peça COMPLETA e PRONTA. Você TEM o texto da decisão recorrida abaixo. NÃO pare na análise — produza os arquivos no disco.

PASSOS OBRIGATÓRIOS (execute de verdade, com as ferramentas — não só descreva):
1. **PESQUISA JURISPRUDENCIAL OBRIGATÓRIA**: use os MCPs `bnp-api` (buscar_precedentes) e `cjf-jurisprudencia` (buscar_jurisprudencia_cjf) pra achar precedentes TNU/STJ/STF que sustentem a tese. Monte o Banco de Fontes Verificadas. SÓ cite o que vier dessas fontes (origem rastreável).
2. Redija a peça ({peca_tipo}) com a skill de elaboração adequada, rebatendo os fundamentos da recorrida.
3. **OBRIGATÓRIO**: rode `validacao/anti-alucinacao` — remova citação sem origem.
4. **ESCREVA os arquivos no disco** (use a ferramenta de escrita): salve o `.txt` da peça em `{pasta}` e gere o `.docx` via `python skills/_shared/formatacao-docx/formatar_peca.py --entrada <txt> --tipo-peca <agravo|embargos|memoriais> --paj {paj_norm}` (env FORMATAR_PECA_SAIDA_DIR já aponta pra pasta do PAJ).

NÃO protocole. NÃO movimente o SISDPU.

Ao FINAL, emita EXATAMENTE (e só depois de ter ESCRITO os arquivos):
@@@RECURSO_INICIO@@@
PECA_TIPO: <tipo>
ARQUIVOS: <nomes .txt/.docx gerados, separados por ; — se vazio, explique em ALERTAS por que não gerou>
RESUMO: <2-4 frases>
PRECEDENTES_USADOS: <quantos precedentes do MCP citou>
ALERTAS: <pontos de atenção ou n/a>
@@@RECURSO_FIM@@@

==== DECISÃO RECORRIDA E DOCS ({rec_nomes or 'nenhum'}) ====
{recorrida or '(sem documento — verifique a pasta peças/ com a ferramenta Read)'}

==== CONTEXTO RESUMIDO (PROMPT_MAX, trecho) ====
{prompt_max}
"""


_REC_RE = re.compile(r"@@@RECURSO_INICIO@@@(.*?)@@@RECURSO_FIM@@@", re.DOTALL)


def recurso(paj: str) -> dict:
    # trava de orçamento (recurso é pesado — respeitar teto)
    with _acc_lock:
        consumido = (_acumulado["input"] + _acumulado["output"]
                     + _acumulado["cache_read"] + _acumulado["cache_creation"])
    if consumido >= MAX_TOKENS_RUN:
        return {"paj": paj, "status": "pulado_budget"}

    pasta = ENTRADA / paj
    atuacao = _ler_json(pasta / "atuacao.json")
    t0 = dt.datetime.now()
    _salvar_status(paj, status="redigindo_recurso", inicio=t0.isoformat(timespec="seconds"))
    log(f"[{paj}] redigindo recurso ({atuacao.get('peca_tipo','?')})…")

    prompt = montar_prompt_recurso(paj, pasta, atuacao)
    cmd = [
        CLAUDE_CMD, "--print", "--model", "opus",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]
    backoffs = [60, 120, 240]
    stdout = ""
    for tent in range(len(backoffs) + 1):
        # run_claude: lock global serializa (sem burst TPM) + mata árvore + reapa MCP órfãos
        rc, stdout, stderr = run_claude(cmd, prompt, str(WORKSPACE), _env_recurso(pasta), TIMEOUT_RECURSO)
        if rc == -9:
            log(f"[{paj}] TIMEOUT recurso")
            _salvar_status(paj, status="erro_recurso", erro="timeout")
            return {"paj": paj, "status": "timeout"}
        saida = (stdout or "") + " " + (stderr or "")
        low = saida.lower()
        if any(p in low for p in _RATE) and (rc != 0 or "is_error" in low):
            if tent < len(backoffs):
                log(f"[{paj}] rate limit — aguardando {backoffs[tent]}s")
                time.sleep(backoffs[tent]); continue
            _salvar_status(paj, status="rate_limit")
            return {"paj": paj, "status": "rate_limit"}
        break

    try:
        wrapper = json.loads(stdout.strip())
    except Exception:
        wrapper = {}
    _contabilizar(wrapper, paj)
    result_text = wrapper.get("result", "") if isinstance(wrapper, dict) else stdout

    m = _REC_RE.search(result_text or "")
    bloco = {}
    if m:
        for linha in m.group(1).splitlines():
            mm = re.match(r"^([A-Z_]+):\s*(.*)$", linha)
            if mm:
                bloco[mm.group(1).lower()] = mm.group(2).strip()

    # atualiza atuacao.json: status done + arquivos gerados
    atuacao["status"] = "done"
    atuacao["etapa"] = "recurso"
    if bloco.get("resumo"):
        atuacao["resumo_recurso"] = bloco["resumo"]
    if bloco.get("alertas"):
        atuacao["alertas"] = (atuacao.get("alertas", "") + " | " + bloco["alertas"]).strip(" |")
    try:
        (pasta / "atuacao.json").write_text(json.dumps(atuacao, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    dur = (dt.datetime.now() - t0).total_seconds()
    with _acc_lock:
        custo = _acumulado["custo_usd"]
        toks = _acumulado["input"]+_acumulado["output"]+_acumulado["cache_read"]+_acumulado["cache_creation"]
    log(f"[{paj}] RECURSO ok ({dur:.0f}s) | run acumulado: {toks:,} tok ${custo:.2f}")
    _salvar_status(paj, status="done", etapa="recurso", duracao_s=round(dur))
    return {"paj": paj, "status": "done", "arquivos": bloco.get("arquivos", "")}


def listar_pajs() -> list[str]:
    if not ENTRADA.exists():
        return []
    return [d.name for d in sorted(ENTRADA.iterdir())
            if d.is_dir() and (d / "PROMPT_MAX.md").exists()]


def _atuacao_status(paj: str) -> str:
    return _ler_json(ENTRADA / paj / "atuacao.json").get("status", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["decisao", "recurso"], default="decisao")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=int(os.getenv("BATCH_WORKERS", "1")))
    args = ap.parse_args()

    pajs = listar_pajs()
    if args.only:
        pajs = [p for p in pajs if p == args.only]

    if args.stage == "decisao":
        if not args.force:
            pajs = [p for p in pajs if _atuacao_status(p) not in ("done", "recurso_pendente")]
        fn = decidir
    else:  # recurso
        pajs = [p for p in pajs if _atuacao_status(p) == "recurso_pendente"]
        fn = recurso

    if args.limit:
        pajs = pajs[: args.limit]
    if not pajs:
        log("nada a processar")
        return 0

    # Hygiene: mata órfãos de runs anteriores antes de começar
    n = reap_orphans()
    if n:
        log(f"reaper: {n} processo(s) órfão(s) limpo(s) no início")

    log(f"=== ESTÁGIO {args.stage.upper()} — {len(pajs)} PAJs, {args.workers} workers ===")
    resultados = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fn, p): p for p in pajs}
        feito = 0
        for fut in as_completed(futs):
            feito += 1
            try:
                resultados.append(fut.result())
            except Exception as e:
                resultados.append({"paj": futs[fut], "status": "erro", "erro": str(e)})
            log(f"--- {feito}/{len(pajs)} ---")

    tipos: dict[str, int] = {}
    for r in resultados:
        t = r.get("tipo") or r.get("status", "?")
        tipos[t] = tipos.get(t, 0) + 1
    with _acc_lock:
        a = dict(_acumulado)
    log(f"=== FIM {args.stage} === tipos={tipos}")
    log(f"=== CONSUMO: {a['pajs']} PAJs | input={a['input']} output={a['output']} "
        f"cache_read={a['cache_read']} cache_creation={a['cache_creation']} | TOTAL ${a['custo_usd']:.2f} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
