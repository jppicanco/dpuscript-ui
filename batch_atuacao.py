#!/usr/bin/env python3
"""Batch autônomo de atuação por PAJ.

Para cada PAJ com PROMPT_MAX.md, roda o Claude CLI (Opus) com cwd no
dpu-workspace — assim ele tem acesso a CLAUDE.md, skills (triagem,
elaboracao, pesquisa, validacao/anti-alucinacao, arquivamento), MEMORY.md
e bases. O Claude DECIDE autonomamente entre:

  - DESPACHO/ciência  → mero expediente, intimação de audiência, etc. Só texto
  - ARQUIVAMENTO      → irrecorribilidade / inviabilidade / vitória já obtida
  - RECURSO           → decisão recorrível → peça completa + anti-alucinação + DOCX

Escreve por PAJ:
  - elaboracao.json  (compat com a UI: {status, summary, last_action, concluido_em})
  - atuacao.json     (estruturado: tipo, peca_tipo, prazo, resumo, o_que_fazer,
                      movimentacao, arquivos, confianca, alertas)

Resiliente: pool de N workers, pula PAJs já concluídos (salvo --force),
loga progresso em batch_atuacao.log + batch_status.json continuamente.

NÃO protocola nem movimenta o SISDPU — só prepara. JP revisa e protocola.

Uso:
    python batch_atuacao.py                  # todos os PAJs pendentes
    python batch_atuacao.py --force          # reprocessa todos
    python batch_atuacao.py --only 2026-039-07596
    python batch_atuacao.py --limit 5        # só os 5 primeiros (teste)
    python batch_atuacao.py --workers 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WORKSPACE = Path(os.getenv("DPU_WORKSPACE", r"E:\DPU\dpu-workspace"))
ENTRADA = WORKSPACE / "Entrada" / "dpuscript"
REGRAS_FILE = WORKSPACE / "dpuscript" / "memory" / "regras_atuacao.md"
TEMPLATE_DOCX = os.getenv(
    "FORMATAR_PECA_TEMPLATE",
    r"D:\DPU\MODELO ARE 1446634 - agravo interno hanseníase - PAJ 2023.040.06077.docx",
)

import shutil as _shutil
CLAUDE_CMD = (
    os.getenv("CLAUDE_CLI")
    or _shutil.which("claude")
    or r"C:\Users\JP\AppData\Roaming\npm\claude.CMD"
)

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "batch_atuacao.log"
STATUS_FILE = BASE_DIR / "batch_status.json"

TIMEOUT_SEG = int(os.getenv("BATCH_TIMEOUT", "1500"))  # 25 min por PAJ

_log_lock = threading.Lock()
_status_lock = threading.Lock()
_status: dict = {}


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def atualizar_status(paj: str, **campos) -> None:
    with _status_lock:
        _status.setdefault(paj, {})
        _status[paj].update(campos)
        _status[paj]["atualizado_em"] = dt.datetime.now().isoformat(timespec="seconds")
        try:
            STATUS_FILE.write_text(
                json.dumps(_status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def _carregar_regras() -> str:
    if REGRAS_FILE.exists():
        try:
            return REGRAS_FILE.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def montar_prompt(paj_norm: str, pasta: Path) -> str:
    prompt_max = (pasta / "PROMPT_MAX.md").read_text(encoding="utf-8", errors="replace")
    regras = _carregar_regras()
    bloco_regras = (
        f"\n\n## REGRAS APRENDIDAS (correções do Defensor — RESPEITE À RISCA)\n{regras}\n"
        if regras.strip()
        else ""
    )
    pasta_abs = str(pasta)
    return f"""Você é o assistente jurídico autônomo da DPU. JP é Defensor Público Federal Categoria Especial, atua na **TNU e no STJ** (matéria previdenciária majoritariamente).

Sua tarefa: analisar ESTE PAJ e DEIXAR A ATUAÇÃO PRONTA para o Defensor só revisar e protocolar. Você decide TUDO autonomamente — JP não está disponível agora; ele revisa de manhã.

NÃO protocole nada. NÃO faça movimentação no SISDPU. Apenas PREPARE (texto + arquivos).{bloco_regras}

## DECISÃO — escolha UMA atuação para este PAJ

1. **DESPACHO / CIÊNCIA** — quando NÃO há peça a fazer:
   - Intimação de audiência (só registrar ciência: "Ciente. Audiência designada para DD/MM às HH:MM.")
   - Mero expediente, vista ao MPF, aguardando distribuição/julgamento, abertura de PAJ, decurso de prazo
   - Decisão de mera ADMISSÃO/conhecimento/distribuição/conversão em diligência (NEUTRA — não é vitória nem derrota)
   → Produza só o TEXTO da movimentação (curto), salve em `{pasta_abs}\\despacho.txt`. SEM DOCX.

2. **ARQUIVAMENTO** — use a skill `arquivamento`:
   - Tipo 1: irrecorribilidade (ex: decisão monocrática do Presidente da TNU — vide regras)
   - Tipo 2: inviabilidade de mérito (jurisprudência consolidada contra, sem distinguishing viável)
   - Tipo 3: VITÓRIA já obtida e cumprida (acórdão favorável transitado, acordo cumprido)
   → Redija o despacho de arquivamento, salve em `{pasta_abs}\\`. Texto pronto pro SISDPU.

3. **RECURSO** — decisão DESFAVORÁVEL com recurso cabível e viável:
   - Identifique o recurso correto (ED, agravo interno, REsp, AREsp, RE, memoriais, embargos de divergência)
   - Use as skills de pesquisa + elaboração do workspace
   - **OBRIGATÓRIO**: rode a skill `validacao/anti-alucinacao` ANTES de finalizar (toda citação tem que ter origem rastreável)
   - Gere a peça final em DOCX via `skills/_shared/formatacao-docx/formatar_peca.py` (o DOCX deve ir para `{pasta_abs}\\`)
   - Salve também o .txt da peça em `{pasta_abs}\\`

## REGRAS PROCESSUAIS
- TNU/STJ/JEF: SEM dobra de prazo da DPU. Dias úteis. +10 dias de ciência ficta no e-Proc.
- Decisão monocrática do RELATOR desfavorável → cabe agravo interno.
- Decisão monocrática do PRESIDENTE da TNU → em regra IRRECORRÍVEL (no máximo ED por omissão/contradição/obscuridade).
- Decisão COLEGIADA → ED só se vício; pode caber REsp/RE.
- NUNCA invente jurisprudência, número de processo, tese ou citação. Se não tem na base/PAJ, não cite.

## ESFORÇO PROPORCIONAL
- Despacho/ciência: texto curto, só .txt. Rápido.
- Arquivamento: despacho fundamentado, skill arquivamento.
- Recurso: peça completa + anti-alucinação + DOCX. Capriche — é peça judicial real.

## SAÍDA OBRIGATÓRIA
Faça todo o trabalho (use ferramentas, escreva os arquivos na pasta do PAJ) e, ao FINAL da sua resposta, emita EXATAMENTE este bloco estruturado (sem markdown, sem nada depois dele):

@@@ATUACAO_INICIO@@@
TIPO: <DESPACHO|ARQUIVAMENTO|RECURSO|NAO_ATUAR>
PECA_TIPO: <ex: agravo_interno_stj|embargos_declaracao_tnu|resp|despacho_ciencia|despacho_arquivamento|nenhuma>
PRAZO: <DD/MM/AAAA ou n/a>
CONFIANCA: <alta|media|baixa>
ARQUIVOS: <nomes dos arquivos gerados separados por ; ou "nenhum">
RESUMO: <2 a 4 frases: o que é este PAJ, qual a decisão/movimentação atual, e o que ela significa>
O_QUE_FAZER: <1 a 3 frases diretas dizendo o que o Defensor tem que fazer agora>
ALERTAS: <riscos, dúvidas ou pontos de atenção; ou n/a>
---MOVIMENTACAO---
<texto PRONTO pra colar no SISDPU — a movimentação/despacho. Se for recurso, escreva a movimentação de juntada da peça, ex: "Junta-se agravo interno em face da decisão de fls. ...">
@@@ATUACAO_FIM@@@

--- INÍCIO DO PROMPT_MAX DO PAJ {paj_norm} ---

{prompt_max}
"""


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def _env_para_claude(pasta: Path) -> dict:
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
              "CLAUDE_PROJECT_DIR", "CLAUDE_AGENT_RUN_ID",
              # NUNCA usar API paga — força OAuth (plano enterprise via claude login)
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # formatar_peca.py salva o DOCX/PDF na pasta do próprio PAJ
    env["FORMATAR_PECA_SAIDA_DIR"] = str(pasta)
    env["FORMATAR_PECA_TEMPLATE"] = TEMPLATE_DOCX
    return env


_BLOCO_RE = re.compile(
    r"@@@ATUACAO_INICIO@@@(.*?)@@@ATUACAO_FIM@@@", re.DOTALL
)


def parse_bloco(texto: str) -> dict:
    """Extrai o bloco estruturado @@@ATUACAO@@@ do output do Claude."""
    m = _BLOCO_RE.search(texto or "")
    if not m:
        return {}
    corpo = m.group(1)
    # separa cabeçalho (key: value) da movimentação
    if "---MOVIMENTACAO---" in corpo:
        cab, mov = corpo.split("---MOVIMENTACAO---", 1)
    else:
        cab, mov = corpo, ""
    campos: dict = {"movimentacao": mov.strip()}
    chave_map = {
        "TIPO": "tipo",
        "PECA_TIPO": "peca_tipo",
        "PRAZO": "prazo",
        "CONFIANCA": "confianca",
        "ARQUIVOS": "arquivos",
        "RESUMO": "resumo",
        "O_QUE_FAZER": "o_que_fazer",
        "ALERTAS": "alertas",
    }
    cur_key = None
    buf: dict[str, list] = {}
    for linha in cab.splitlines():
        m2 = re.match(r"^([A-Z_]+):\s*(.*)$", linha)
        if m2 and m2.group(1) in chave_map:
            cur_key = chave_map[m2.group(1)]
            buf[cur_key] = [m2.group(2)]
        elif cur_key:
            buf[cur_key].append(linha)
    for k, v in buf.items():
        campos[k] = "\n".join(v).strip()
    return campos


_RATE_PATTERNS = (
    "limitando temporariamente",
    "rate limit", "rate_limit", "rate-limit",
    "429", "too many requests", "overloaded", "quota",
    "tente novamente", "try again later",
)


def _e_rate_limit(saida: str, returncode: int) -> bool:
    """Detecta rate limit da Anthropic na saída do claude CLI."""
    if returncode == 0:
        # Mesmo com exit 0, claude pode retornar is_error com a msg no result
        low = (saida or "").lower()
        return any(p in low for p in _RATE_PATTERNS) and "is_error" in low
    low = (saida or "").lower()
    return any(p in low for p in _RATE_PATTERNS)


def listar_pajs() -> list[str]:
    if not ENTRADA.exists():
        return []
    out = []
    for d in sorted(ENTRADA.iterdir()):
        if d.is_dir() and (d / "PROMPT_MAX.md").exists():
            out.append(d.name)
    return out


def ja_concluido(paj: str) -> bool:
    f = ENTRADA / paj / "atuacao.json"
    if not f.exists():
        return False
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d.get("status") == "done"
    except Exception:
        return False


def processar(paj: str) -> dict:
    pasta = ENTRADA / paj
    inicio = dt.datetime.now()
    atualizar_status(paj, status="rodando", inicio=inicio.isoformat(timespec="seconds"))
    log(f"[{paj}] iniciando…")

    try:
        prompt = montar_prompt(paj, pasta)
    except Exception as e:
        log(f"[{paj}] ERRO montando prompt: {e}")
        atualizar_status(paj, status="erro", erro=f"prompt: {e}")
        return {"paj": paj, "status": "erro", "erro": str(e)}

    cmd = [
        CLAUDE_CMD,
        "--print",
        "--model", "opus",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
    ]

    # Retry com backoff exponencial pra rate limit da Anthropic.
    # Backoffs em segundos; len = nº de tentativas extras.
    backoffs = [45, 90, 180, 300]
    proc = None
    result_text = ""
    for tentativa in range(len(backoffs) + 1):
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_SEG,
                cwd=str(WORKSPACE),
                env=_env_para_claude(pasta),
            )
        except subprocess.TimeoutExpired:
            log(f"[{paj}] TIMEOUT ({TIMEOUT_SEG}s)")
            atualizar_status(paj, status="timeout")
            _escrever_resultado(paj, pasta, status="timeout", summary="(timeout)", bloco={})
            return {"paj": paj, "status": "timeout"}
        except Exception as e:
            log(f"[{paj}] ERRO subprocess: {type(e).__name__}: {e}")
            atualizar_status(paj, status="erro", erro=str(e))
            return {"paj": paj, "status": "erro", "erro": str(e)}

        saida = (proc.stdout or "") + " " + (proc.stderr or "")
        if _e_rate_limit(saida, proc.returncode):
            if tentativa < len(backoffs):
                espera = backoffs[tentativa]
                log(f"[{paj}] rate limit — aguardando {espera}s (tentativa {tentativa + 1}/{len(backoffs)})")
                atualizar_status(paj, status="rate_limit_retry", tentativa=tentativa + 1)
                import time as _t
                _t.sleep(espera)
                continue
            else:
                log(f"[{paj}] rate limit persistente após {len(backoffs)} tentativas — desisto")
                atualizar_status(paj, status="rate_limit")
                _escrever_resultado(paj, pasta, status="erro",
                                    summary="rate limit persistente (Anthropic)", bloco={})
                return {"paj": paj, "status": "rate_limit"}
        break  # não é rate limit — sai do loop de retry

    if proc.returncode != 0:
        log(f"[{paj}] exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
        atualizar_status(paj, status="erro", erro=f"exit {proc.returncode}")
        _escrever_resultado(paj, pasta, status="erro",
                            summary=f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}", bloco={})
        return {"paj": paj, "status": "erro"}

    # output-format json → wrapper {"result": "<texto final>", ...}
    try:
        wrapper = json.loads(proc.stdout.strip())
        result_text = wrapper.get("result", "") if isinstance(wrapper, dict) else ""
    except Exception:
        result_text = proc.stdout

    bloco = parse_bloco(result_text)
    tipo = bloco.get("tipo", "?")
    dur = (dt.datetime.now() - inicio).total_seconds()
    _escrever_resultado(paj, pasta, status="done", summary=result_text, bloco=bloco)
    log(f"[{paj}] OK tipo={tipo} ({dur:.0f}s)")
    atualizar_status(paj, status="done", tipo=tipo, duracao_s=round(dur))
    return {"paj": paj, "status": "done", "tipo": tipo}


def _escrever_resultado(paj: str, pasta: Path, status: str, summary: str, bloco: dict) -> None:
    agora = dt.datetime.now().isoformat(timespec="seconds")
    # elaboracao.json — compat com a UI existente
    try:
        (pasta / "elaboracao.json").write_text(
            json.dumps({
                "status": status,
                "summary": summary,
                "last_action": "batch_atuacao",
                "concluido_em": agora,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    # atuacao.json — estruturado pra Central de Atuação
    try:
        (pasta / "atuacao.json").write_text(
            json.dumps({
                "status": status,
                "tipo": bloco.get("tipo", ""),
                "peca_tipo": bloco.get("peca_tipo", ""),
                "prazo": bloco.get("prazo", ""),
                "confianca": bloco.get("confianca", ""),
                "arquivos": bloco.get("arquivos", ""),
                "resumo": bloco.get("resumo", ""),
                "o_que_fazer": bloco.get("o_que_fazer", ""),
                "alertas": bloco.get("alertas", ""),
                "movimentacao": bloco.get("movimentacao", ""),
                "concluido_em": agora,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="reprocessa PAJs já concluídos")
    ap.add_argument("--only", help="processa só este PAJ (norm: 2026-039-07596)")
    ap.add_argument("--limit", type=int, default=0, help="limita N PAJs (teste)")
    ap.add_argument("--workers", type=int, default=int(os.getenv("BATCH_WORKERS", "3")))
    args = ap.parse_args()

    pajs = listar_pajs()
    if args.only:
        pajs = [p for p in pajs if p == args.only]
    if not args.force:
        pajs = [p for p in pajs if not ja_concluido(p)]
    if args.limit:
        pajs = pajs[: args.limit]

    if not pajs:
        log("nada a processar (todos concluídos? use --force)")
        return 0

    log(f"=== BATCH ATUAÇÃO — {len(pajs)} PAJs, {args.workers} workers, modelo opus ===")
    log(f"CLAUDE_CMD={CLAUDE_CMD}")
    resultados = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(processar, p): p for p in pajs}
        feito = 0
        for fut in as_completed(futs):
            feito += 1
            try:
                r = fut.result()
            except Exception as e:
                r = {"paj": futs[fut], "status": "erro", "erro": str(e)}
            resultados.append(r)
            log(f"--- progresso: {feito}/{len(pajs)} ---")

    # Resumo final
    por_status: dict[str, int] = {}
    por_tipo: dict[str, int] = {}
    for r in resultados:
        por_status[r.get("status", "?")] = por_status.get(r.get("status", "?"), 0) + 1
        if r.get("tipo"):
            por_tipo[r["tipo"]] = por_tipo.get(r["tipo"], 0) + 1
    log(f"=== FIM === status={por_status} tipos={por_tipo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
