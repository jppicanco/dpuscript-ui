#!/usr/bin/env python3
"""Estágio DECISÃO via Grok/Hermes no M4 — produção + piloto comparativo.

Modos:
  --prod    : produção — escreve atuacao.json, despacho.txt, elaboracao.json
              só processa PAJs SEM decisão ainda (sem atuacao.json ou status vazio)
  (padrão)  : piloto — escreve atuacao_grok.json, não toca atuacao.json

Uso:
  python decidir_grok.py --prod              # cron M4 normal
  python decidir_grok.py --prod --force      # reprocessa todos
  python decidir_grok.py --limit 5           # piloto nos 5 primeiros sem decisão
  python decidir_grok.py --compare-only      # relatório Grok vs Opus (piloto)
  python decidir_grok.py --only 2026-039-XXXXX --prod

Pré-requisitos (M4):
  - ~/.hermes/hermes-agent/venv/bin/hermes instalado e autenticado (xai-oauth)
  - /Users/macmini/dpu-workspace/ com as pastas dos PAJs
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Caminhos M4
# ---------------------------------------------------------------------------
WORKSPACE   = Path("/Users/macmini/dpu-workspace")
ENTRADA     = WORKSPACE / "Entrada" / "dpuscript"
REGRAS_FILE = WORKSPACE / "dpuscript" / "memory" / "regras_atuacao.md"
LOG_FILE    = WORKSPACE / "dpuscript" / "decidir_grok.log"
ESTADO_FILE = WORKSPACE / "dpuscript" / "estado" / "pajs_processados.json"
MODELO_ARQ_FILE = WORKSPACE / "dpuscript" / "memory" / "modelo_arquivamento.md"

HERMES        = Path.home() / ".hermes/hermes-agent/venv/bin/hermes"
GROK_MODEL    = "grok-4.20-0309-reasoning"
GROK_PROVIDER = "xai-oauth"
TIMEOUT_DECISAO = 300  # 5 min

# Truncagens — idênticas ao batch_atuacao.py do Windows
MAX_MOV_DESC      = 800
N_MOVS            = 3
N_DOCS_DECISAO    = 3
MAX_DECISAO_TOTAL = 45000
_TIPOS_DECISORIOS = ("ACOR", "DESPADEC", "DECIS", "SENT", "VOTO", "EXTRATOATA", "QUESTORDEM")
_DATA_NOME_RE     = re.compile(r"(\d{4})-(\d{2})-(\d{2})_ev(\d+)")

DECISAO_SCHEMA = {
    "type": "object",
    "required": ["tipo", "peca_tipo", "fundamento_decisao", "resumo",
                 "o_que_fazer", "movimentacao", "confianca"],
    "properties": {
        "tipo":               {"type": "string", "enum": ["DESPACHO", "ARQUIVAMENTO", "RECURSO", "NAO_ATUAR"]},
        "peca_tipo":          {"type": "string"},
        "prazo":              {"type": "string"},
        "confianca":          {"type": "string", "enum": ["alta", "media", "baixa"]},
        "fundamento_decisao": {"type": "string"},
        "resumo":             {"type": "string"},
        "o_que_fazer":        {"type": "string"},
        "alertas":            {"type": "string"},
        "movimentacao":       {"type": "string"},
        "precisa_aprofundar": {"type": "boolean"},
        # preenchidos só quando tipo=RECURSO (kit de recurso pro Claude):
        "recurso_tipo":       {"type": "string"},
        "teses_sugeridas":    {"type": "array", "items": {"type": "string"}},
        "pecas_chave":        {"type": "array", "items": {
            "type": "object",
            "properties": {
                "arquivo":   {"type": "string"},
                "descricao": {"type": "string"},
            },
            "required": ["arquivo", "descricao"],
        }},
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
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


def _listar_pecas(pasta: Path) -> list[str]:
    """Nomes das peças .txt em peças/ (pro Grok apontar as peças-chave do recurso)."""
    sub = pasta / "peças"
    if not sub.is_dir():
        sub = pasta / "pecas"
    if not sub.is_dir():
        return []
    return sorted(f.name for f in sub.iterdir() if f.suffix.lower() == ".txt")


def _ler_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _ordem_doc(f: Path) -> tuple:
    m = _DATA_NOME_RE.search(f.name)
    if m:
        ano, mes, dia, ev = m.groups()
        return (1, ano + mes + dia, int(ev))
    return (0, "00000000", 0)


def _decisao_recente(pasta: Path) -> tuple[str, str]:
    cands = []
    for sub in ("decisoes_superiores", "peças", "pecas"):
        d = pasta / sub
        if d.exists():
            cands += [f for f in d.iterdir() if f.is_file() and f.suffix == ".txt"]
    if not cands:
        return "", ""

    dated   = [f for f in cands if _DATA_NOME_RE.search(f.name)]
    undated = [f for f in cands if not _DATA_NOME_RE.search(f.name)]
    dated.sort(key=_ordem_doc, reverse=True)
    decisorios = [f for f in dated if any(t in f.name.upper() for t in _TIPOS_DECISORIOS)]

    escolhidos: list[Path] = []
    escolhidos += undated
    if decisorios:
        escolhidos.append(decisorios[0])
    for f in dated[:N_DOCS_DECISAO]:
        if f not in escolhidos:
            escolhidos.append(f)
    vistos: set[Path] = set()
    escolhidos = [f for f in escolhidos if not (f in vistos or vistos.add(f))]

    partes, nomes, total = [], [], 0
    for f in escolhidos:
        txt = _ler(f)
        if not txt.strip():
            continue
        if len(txt) > 25000:
            txt = txt[:15000] + "\n\n[...TRECHO OMITIDO...]\n\n" + txt[-8000:]
        if total + len(txt) > MAX_DECISAO_TOTAL:
            txt = txt[: max(0, MAX_DECISAO_TOTAL - total)]
        if not txt:
            break
        partes.append(f"--- {f.name} ---\n{txt}")
        nomes.append(f.name)
        total += len(txt)
    return "\n\n".join(partes), ", ".join(nomes)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def montar_prompt_decisao(paj_norm: str, pasta: Path) -> str:
    meta      = _ler_json(pasta / "metadata.json")
    det       = meta.get("detalhes_sisdpu", {}) or {}
    movs      = det.get("movimentacoes", []) or []
    movs      = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0), reverse=True)[:N_MOVS]
    movs_txt  = "\n".join(
        f"  [{m.get('data','?')}] {(m.get('descricao','') or '')[:MAX_MOV_DESC]}"
        for m in movs
    ) or "  (sem movimentações)"
    resumo       = _ler(pasta / "resumo_curto.md", 4000)
    decisao, arq = _decisao_recente(pasta)
    regras       = _ler(REGRAS_FILE)
    bloco_regras = f"\n\n## REGRAS APRENDIDAS (correções do Defensor — RESPEITE)\n{regras}\n" if regras.strip() else ""
    modelo_arq   = _ler(MODELO_ARQ_FILE)
    bloco_modelo = f"\n\n## MODELO DE ARQUIVAMENTO (siga à risca quando tipo=ARQUIVAMENTO)\n{modelo_arq}\n" if modelo_arq.strip() else ""
    pecas_lista  = _listar_pecas(pasta)
    pecas_txt    = "\n".join(f"  - {n}" for n in pecas_lista) or "  (nenhuma peça localizada)"
    schema_str   = json.dumps(DECISAO_SCHEMA, ensure_ascii=False, indent=2)

    return f"""Você é o assistente jurídico da DPU. JP é Defensor Público Federal Cat. Especial, atua TNU + STJ (previdenciário).

Sua tarefa: DECIDIR a atuação deste PAJ. Leia de trás pra frente: a movimentação/decisão MAIS RECENTE geralmente já determina o que fazer.{bloco_regras}

## TIPOS DE ATUAÇÃO
- **DESPACHO** — não há peça a fazer: intimação de audiência, mero expediente, vista ao MPF, aguardando distribuição/julgamento, abertura de PAJ, decurso, OU decisão de mera ADMISSÃO/conhecimento/distribuição (NEUTRA), OU a DPU é vencedora e o adverso recorreu (só acompanhar).
- **ARQUIVAMENTO** — (a) irrecorribilidade (ex: monocrática do Presidente TNU), (b) inviabilidade de mérito (juris consolidada contra sem distinguishing), (c) VITÓRIA já obtida e cumprida.
- **RECURSO** — decisão DESFAVORÁVEL ao assistido, com recurso cabível e viável (ED, agravo interno, REsp, AREsp, RE, memoriais, embargos divergência). NÃO marque recurso se a DPU já interpôs o recurso cabível.
- **NAO_ATUAR** — nada a fazer.

## REGRAS PROCESSUAIS
- TNU/STJ/JEF: SEM dobra DPU. Prazo em DIAS ÚTEIS.
- **CÁLCULO DE PRAZO (e-Proc — SEMPRE aplicar):** a intimação eletrônica só é considerada ABERTA após **10 DIAS CORRIDOS** da disponibilização (ciência ficta no 10º dia corrido, se não consultada antes). SÓ DEPOIS disso começam a correr os DIAS ÚTEIS do prazo recursal. Portanto: data da disponibilização + 10 dias corridos = início; a partir do 1º dia útil seguinte, conte os dias úteis do prazo (agravo interno/ED = 15 dias úteis na TNU/STJ, sem dobra). Informe a DATA-LIMITE resultante no campo `prazo`. NUNCA conte os dias úteis a partir da disponibilização sem somar os 10 corridos.
- Monocrática do RELATOR desfavorável → agravo interno. Monocrática do PRESIDENTE TNU → em regra IRRECORRÍVEL (no máx ED). Colegiada → ED só se vício; pode caber REsp/RE.
- NUNCA invente citação/jurisprudência/número.

## CONTEXTO DO PAJ {paj_norm}
- Assistido: {meta.get('assistido_caixa','?')}
- Ofício: {meta.get('oficio_caixa','?')}
- Foro detectado: {meta.get('foro_detectado','?')}
- Classificação automática (heurística): {meta.get('classificacao','?')}
- Processo judicial: {meta.get('processo_judicial','?')}

### RESUMO
{resumo}

### ÚLTIMAS {N_MOVS} MOVIMENTAÇÕES (mais recente primeiro)
{movs_txt}

### DOCUMENTOS RELEVANTES ({arq or 'nenhum'})
{decisao or '(sem documento de decisão)'}

### PEÇAS DISPONÍVEIS NA PASTA (use estes nomes em pecas_chave)
{pecas_txt}

## KIT DE RECURSO (preencher SOMENTE se tipo=RECURSO)
Quando o caso for RECURSO, além dos campos normais, preencha:
- `recurso_tipo`: qual recurso cabe (agravo interno, embargos de declaração, REsp, AREsp, RE, memoriais, embargos de divergência).
- `pecas_chave`: as peças PRINCIPAIS pro recurso (seja generoso — inclua todas as relevantes: decisão recorrida, acórdão da TR, sentença, PUIL/recurso anterior, contrarrazões, laudo pericial etc.; deixe de fora só o irrelevante). Para CADA uma, dois campos: `arquivo` (nome EXATO da lista acima, sem inventar) e `descricao` (o que é a peça, pra o Claude saber se precisa ler — ex.: "Acórdão da Turma Recursal que negou provimento ao recurso inominado", "Decisão monocrática do Presidente da TNU inadmitindo o PUIL", "Sentença de 1º grau de improcedência").
- `teses_sugeridas`: hipóteses de tese recursal, como SUGESTÕES. IMPORTANTE: são apenas pistas pra adiantar o raciocínio — NÃO são vinculantes. Quem decide a estratégia é o Claude (mais preciso), que poderá escolher uma delas, combiná-las, criar uma tese nova não listada ou recusar todas. Levante 2 a 4 hipóteses plausíveis com base na decisão recorrida, sem se alongar.

## TAMANHO DO DESPACHO — SEJA INTELIGENTE
O campo `movimentacao` é o texto pronto pra colar no SISDPU: texto corrido, sem markdown, sem saudação, sem "Excelentíssimo", sem data/nome/cargo ao final. O TAMANHO depende do caso:

- TRÂMITE SIMPLES (DESPACHO) → texto CURTO, 1 a 3 frases. Ex.: intimação da data de audiência/sessão de julgamento (informe a data/hora marcada e que se aguarda), vista ao MPF, mero expediente, abertura de PAJ, mera ciência, decurso. NÃO encha de fundamentação o que é simples.
- DESPACHO DE MÉRITO / sobrestamento / situação que exija justificar a conduta → fundamentado: relatório breve + razão + conclusão.
- ARQUIVAMENTO → SEMPRE completo e bem motivado. Tem que explicar POR QUE se arquiva e POR QUE não cabe mais recurso. Siga o "MODELO DE ARQUIVAMENTO" abaixo conforme o tipo (1 = monocrática do Presidente da TNU; 2 = inviabilidade caso a caso; 3 = vitória).

REGRAS DE PRECISÃO (valem sempre):
- NUNCA invente número de precedente, súmula, artigo ou processo. Use só o que consta dos autos/decisão ou do modelo/regras deste prompt.
- NÃO cite o NOME do Ministro/Presidente — escreva apenas "o Presidente da TNU" / "a Presidência da TNU".
- Monocrática do Presidente da TNU é irrecorrível por força do art. 15, §1º, do RI-TNU (NÃO "art. 15, V"; NÃO cabe agravo interno — no máximo ED).
{bloco_modelo}
## SAÍDA OBRIGATÓRIA
Responda SOMENTE um objeto JSON válido, sem texto antes ou depois, sem markdown (sem ```). Siga exatamente este schema:
{schema_str}

Para DESPACHO: `movimentacao` segue a regra de TAMANHO acima (curto p/ trâmite simples; fundamentado p/ mérito). Campo `resumo` sempre curto (uma linha do que é o PAJ).
Para ARQUIVAMENTO: `movimentacao` traz o despacho COMPLETO conforme o MODELO DE ARQUIVAMENTO. `resumo` curto.
Para NAO_ATUAR: `movimentacao` pode ser breve.
Para RECURSO: `movimentacao` traz a movimentação de juntada e `precisa_aprofundar`=true.
"""


# ---------------------------------------------------------------------------
# Chamada Grok via Hermes
# ---------------------------------------------------------------------------

def chamar_grok(prompt: str) -> tuple[dict, str]:
    if not HERMES.exists():
        return {}, f"hermes não encontrado em {HERMES}"
    cmd = [str(HERMES), "chat", "-Q", "-q", prompt, "-m", GROK_MODEL, "--provider", GROK_PROVIDER]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=TIMEOUT_DECISAO, check=False,
        )
    except subprocess.TimeoutExpired:
        return {}, "timeout"
    except Exception as e:
        return {}, f"subprocess erro: {e!r}"

    if proc.returncode != 0:
        return {}, f"hermes rc={proc.returncode}: {(proc.stderr or '')[-300:]}"

    raw = (proc.stdout or "").strip()
    m = re.search(r"(\{[\s\S]*\})\s*$", raw) or re.search(r"\{[\s\S]+\}", raw)
    if not m:
        return {}, f"sem JSON na resposta: {raw[:300]}"
    try:
        return json.loads(m.group(0)), ""
    except json.JSONDecodeError as e:
        return {}, f"JSON inválido: {e} | trecho: {m.group(0)[:200]}"


# ---------------------------------------------------------------------------
# Persistência — modo produção
# ---------------------------------------------------------------------------

def _salvar_prod(paj: str, pasta: Path, d: dict) -> None:
    """Escreve atuacao.json, elaboracao.json e despacho.txt (espelha Windows)."""
    agora  = dt.datetime.now().isoformat(timespec="seconds")
    tipo   = d.get("tipo", "")
    status = "recurso_pendente" if tipo == "RECURSO" else "done"

    atuacao = {
        "status":             status,
        "tipo":               tipo,
        "peca_tipo":          d.get("peca_tipo", ""),
        "prazo":              d.get("prazo", ""),
        "confianca":          d.get("confianca", ""),
        "fundamento_decisao": d.get("fundamento_decisao", ""),
        "resumo":             d.get("resumo", ""),
        "o_que_fazer":        d.get("o_que_fazer", ""),
        "alertas":            d.get("alertas", ""),
        "movimentacao":       d.get("movimentacao", ""),
        "etapa":              "decisao",
        "modelo":             GROK_MODEL,
        "concluido_em":       agora,
        # movimento da caixa que esta decisão analisou — se chegar movimento
        # novo depois, _precisa_decisao detecta e re-analisa.
        "decidido_mov_hash":  _mov_hash_atual(pasta),
        # kit de recurso (vazio quando não é RECURSO):
        "recurso_tipo":       d.get("recurso_tipo", ""),
        "teses_sugeridas":    d.get("teses_sugeridas", []),
        "pecas_chave":        d.get("pecas_chave", []),
    }
    (pasta / "atuacao.json").write_text(
        json.dumps(atuacao, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # compat UI
    (pasta / "elaboracao.json").write_text(json.dumps({
        "status":      "done" if status in ("done", "recurso_pendente") else status,
        "summary":     f"[{tipo}] {d.get('fundamento_decisao','')}\n\n{d.get('resumo','')}\n\nO que fazer: {d.get('o_que_fazer','')}",
        "last_action": "batch_decisao_grok",
        "concluido_em": agora,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    # despacho pronto pra colar
    if tipo != "RECURSO" and d.get("movimentacao"):
        (pasta / "despacho.txt").write_text(d["movimentacao"], encoding="utf-8")

    # kit de recurso: dossiê pro Claude redigir (M4 adianta o trabalho factual)
    if tipo == "RECURSO":
        (pasta / "preparo_recurso.md").write_text(
            _montar_preparo_recurso(d, paj), encoding="utf-8"
        )


# Raiz das pastas de PAJ NO WINDOWS — o Claude que redige o recurso roda lá e
# precisa abrir os arquivos por caminho absoluto. M4 e Windows ficam em sync.
WIN_PAJ_ROOT = r"E:\DPU\dpu-workspace\Entrada\dpuscript"


def _montar_preparo_recurso(d: dict, paj: str) -> str:
    """Monta o dossiê de recurso (kit pro Claude). Teses são NÃO-VINCULANTES.
    Lista as peças PRINCIPAIS (com caminho Windows completo + descrição do que
    é cada uma) pro Claude decidir o que precisa ler."""
    def _lista(itens):
        itens = [str(x).strip() for x in (itens or []) if str(x).strip()]
        return "\n".join(f"- {x}" for x in itens) or "- (nenhuma)"

    base   = f"{WIN_PAJ_ROOT}\\{paj}"
    linhas = []
    for p in (d.get("pecas_chave") or []):
        if isinstance(p, dict):
            arq, desc = str(p.get("arquivo", "")).strip(), str(p.get("descricao", "")).strip()
        else:
            arq, desc = str(p).strip(), ""
        if arq:
            linhas.append(f"- {base}\\peças\\{arq}" + (f"  —  {desc}" if desc else ""))
    pecas_paths = "\n".join(linhas) or f"- (ver peças em {base}\\peças\\)"
    teses       = _lista(d.get("teses_sugeridas"))
    return f"""# Kit de recurso — preparado pelo Grok (M4) para o Claude redigir

> Dossiê factual montado automaticamente. As teses são SUGESTÕES não-vinculantes.

## Resumo do caso
{d.get('resumo', '').strip() or '(sem resumo)'}

## Decisão recorrida (fundamento)
{d.get('fundamento_decisao', '').strip() or '(não informado)'}

## Recurso cabível
{d.get('recurso_tipo', '').strip() or '(avaliar)'} — Prazo: {d.get('prazo', '').strip() or '(verificar)'}

## O que fazer
{d.get('o_que_fazer', '').strip() or '(não informado)'}

## ARQUIVOS DO PROCESSO (caminhos completos — abrir no Claude)
Pasta do PAJ: {base}
Contexto consolidado: {base}\\PROMPT_MAX.md

Peças principais (cada uma com o que é — abra conforme a tese que escolher):
{pecas_paths}

Demais peças do processo, se precisar de algo além: {base}\\peças\\

## Teses sugeridas pelo Grok — NÃO-VINCULANTES
> ATENÇÃO, Claude: as teses abaixo são apenas pistas levantadas pelo Grok pra adiantar
> o raciocínio. Você NÃO está preso a elas. Avalie criticamente e decida a melhor
> estratégia — pode escolher uma, combiná-las, desenvolver uma tese NOVA não listada
> aqui, ou recusar todas. A estratégia recursal é SUA decisão, com sua maior precisão.
{teses}

## Movimentação de juntada (p/ SISDPU após protocolar)
{d.get('movimentacao', '').strip() or '(gerar na juntada)'}
"""


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------

def _status_atual(pasta: Path) -> str:
    return _ler_json(pasta / "atuacao.json").get("status", "")


def _mov_hash_atual(pasta: Path) -> str:
    """mov_hash do movimento que está HOJE na caixa (fonte: estado central)."""
    paj = pasta.name.replace("-", "/", 1)  # 2026-039-06814 -> 2026/039-06814
    try:
        est = json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
        return est.get("pajs", {}).get(paj, {}).get("mov_hash", "")
    except Exception:
        return ""


def _data_br(s: str):
    """Parse DD/MM/YYYY ou ISO (YYYY-MM-DD...) -> date, ou None."""
    s = (s or "")[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _movimento_apos_decisao(pasta: Path, at: dict) -> bool:
    """Fallback p/ atuacao.json antigo (sem decidido_mov_hash): True se a data
    do movimento na caixa é POSTERIOR à data da decisão — i.e., chegou algo
    novo depois que decidimos."""
    meta = _ler_json(pasta / "metadata.json")
    d_mov = _data_br(meta.get("data_mov_caixa", ""))
    d_dec = _data_br(at.get("concluido_em", ""))
    return bool(d_mov and d_dec and d_mov > d_dec)


def _precisa_decisao(pasta: Path) -> bool:
    """True se o PAJ precisa (re)decisão.

    Decide quando: (a) ainda não há decisão; ou (b) já há decisão mas chegou
    movimento NOVO na caixa depois dela (ex.: despacho 'aguardar julgamento'
    e depois a TNU efetivamente julga). Nunca interrompe trabalho em curso.
    """
    at = _ler_json(pasta / "atuacao.json")
    status = at.get("status", "")
    if status in ("decidindo", "redigindo_recurso"):
        return False
    if status not in ("done", "recurso_pendente"):
        return True
    # já decidido — re-decide se o movimento mudou desde a decisão
    cur = _mov_hash_atual(pasta)
    dec = at.get("decidido_mov_hash", "")
    if cur and dec:
        return cur != dec
    # atuacao.json antigo (sem o campo): usa a data do movimento como fallback
    return _movimento_apos_decisao(pasta, at)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod",         action="store_true", help="modo produção: escreve atuacao.json")
    ap.add_argument("--only",         help="processar só este PAJ")
    ap.add_argument("--limit",        type=int, default=0)
    ap.add_argument("--force",        action="store_true", help="reprocessa mesmo quem já tem decisão")
    ap.add_argument("--so-tipos",     help="filtra PAJs cujo tipo atual está na lista (ex: DESPACHO,ARQUIVAMENTO)")
    ap.add_argument("--compare-only", action="store_true", help="relatório Grok vs Opus (piloto)")
    args = ap.parse_args()

    pajs = sorted(
        p.name for p in ENTRADA.iterdir()
        if p.is_dir() and re.match(r"\d{4}-\d{3}-\d+", p.name)
    ) if ENTRADA.exists() else []

    if args.only:
        pajs = [p for p in pajs if p == args.only]

    if args.so_tipos:
        alvos_t = {t.strip().upper() for t in args.so_tipos.split(",") if t.strip()}
        pajs = [p for p in pajs
                if _ler_json(ENTRADA / p / "atuacao.json").get("tipo", "").upper() in alvos_t]

    # --- modo comparação (piloto) ---
    if args.compare_only:
        pajs_com = [p for p in pajs
                    if (ENTRADA / p / "atuacao_grok.json").exists()
                    and (ENTRADA / p / "atuacao.json").exists()]
        if not pajs_com:
            log("Nenhum PAJ com atuacao_grok.json para comparar.")
            return 0
        resultados = [_comparar(p, ENTRADA / p) for p in pajs_com]
        _relatorio(resultados)
        return 0

    # --- filtro principal ---
    if args.prod:
        if not args.force:
            pajs = [p for p in pajs if _precisa_decisao(ENTRADA / p)]
    else:
        # piloto: processa PAJs com atuacao.json done (para comparar)
        pajs = [p for p in pajs if _status_atual(ENTRADA / p) in ("done", "recurso_pendente")]
        if not args.force:
            pajs = [p for p in pajs if not (ENTRADA / p / "atuacao_grok.json").exists()]

    if args.limit:
        pajs = pajs[:args.limit]
    if not pajs:
        log("nada a processar")
        return 0

    modo = "PRODUÇÃO" if args.prod else "PILOTO"
    log(f"=== DECISÃO GROK [{modo}] — {len(pajs)} PAJs ===")
    resultados = []
    for i, paj in enumerate(pajs, 1):
        pasta = ENTRADA / paj
        t0    = dt.datetime.now()
        log(f"[{paj}] ({i}/{len(pajs)}) chamando Grok…")
        prompt     = montar_prompt_decisao(paj, pasta)
        structured, erro = chamar_grok(prompt)
        dur = round((dt.datetime.now() - t0).total_seconds())

        if erro:
            log(f"[{paj}] ERRO ({dur}s): {erro}")
            resultados.append({"paj": paj, "status": "erro", "erro": erro})
            continue

        tipo = structured.get("tipo", "?")
        try:
            if args.prod:
                _salvar_prod(paj, pasta, structured)
                log(f"[{paj}] {tipo} conf={structured.get('confianca','?')} ({dur}s)")
                resultados.append({"paj": paj, "status": "done", "tipo": tipo})
            else:
                structured["_modelo"]    = GROK_MODEL
                structured["_gerado_em"] = dt.datetime.now().isoformat(timespec="seconds")
                (pasta / "atuacao_grok.json").write_text(
                    json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log(f"[{paj}] GROK={tipo} conf={structured.get('confianca','?')} ({dur}s)")
                if (pasta / "atuacao.json").exists():
                    resultados.append(_comparar(paj, pasta))
        except Exception as e:
            log(f"[{paj}] ERRO ao salvar: {e!r}")
            resultados.append({"paj": paj, "status": "erro", "erro": str(e)})

    tipos: dict[str, int] = {}
    for r in resultados:
        k = r.get("tipo") or r.get("status", "?")
        tipos[k] = tipos.get(k, 0) + 1
    log(f"=== FIM [{modo}] tipos={tipos} ===")

    if not args.prod:
        _relatorio([r for r in resultados if "opus_tipo" in r])
    return 0


# ---------------------------------------------------------------------------
# Comparação / relatório (piloto)
# ---------------------------------------------------------------------------

def _comparar(paj: str, pasta: Path) -> dict:
    opus = _ler_json(pasta / "atuacao.json")
    grok = _ler_json(pasta / "atuacao_grok.json")
    return {
        "paj":        paj,
        "opus_tipo":  opus.get("tipo", "?"),
        "grok_tipo":  grok.get("tipo", "?"),
        "concordam":  opus.get("tipo") == grok.get("tipo"),
        "opus_conf":  opus.get("confianca", "?"),
        "grok_conf":  grok.get("confianca", "?"),
        "opus_mov":   opus.get("movimentacao", "")[:200],
        "grok_mov":   grok.get("movimentacao", "")[:200],
    }


def _relatorio(resultados: list[dict]) -> None:
    if not resultados:
        return
    total     = len(resultados)
    concordam = sum(1 for r in resultados if r.get("concordam"))
    log(f"\n{'='*60}")
    log(f"RELATÓRIO PILOTO — {total} PAJs | concordância: {concordam}/{total} ({100*concordam//total}%)")
    divergentes = [r for r in resultados if not r.get("concordam")]
    if divergentes:
        log("DIVERGÊNCIAS:")
        for r in divergentes:
            log(f"  {r['paj']}: Opus={r['opus_tipo']} vs Grok={r['grok_tipo']}")
    for r in resultados:
        ok = "✓" if r.get("concordam") else "✗"
        log(f"  {ok} {r['paj']} Opus={r['opus_tipo']}({r.get('opus_conf','?')}) "
            f"Grok={r['grok_tipo']}({r.get('grok_conf','?')})")
    out = WORKSPACE / "dpuscript" / "piloto_grok_resultado.json"
    try:
        out.write_text(json.dumps({
            "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
            "total": total, "concordam": concordam, "resultados": resultados,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Relatório: {out}")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
