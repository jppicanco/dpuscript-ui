# dpuscript-ui — Interface web do pipeline DPU

Painel FastAPI para o Dr. João Paulo Picanço (Defensor Público Federal, Categoria
Especial, DPU/AM, atua em TNU + STJ previdenciário).

## O que é

UI web local rodando em `http://127.0.0.1:8001` que mostra os PAJs (Processos
de Assistência Jurídica) processados pelo pipeline `dpuscript` e permite que JP
revise, atue e mande o Claude elaborar peças.

## Arquitetura (refatorada em 2026-05-21)

```
M4 (24/7, cron 4x/dia)              PC Windows (visual + elaboração)
─ preparar_pajs.py (08h15/12h30/    ─ dpuscript-ui (este projeto)
  17h30/21h00)                        ─ Flask/FastAPI server :8001
─ Lê caixa SISDPU + reconcilia       ─ Lê pastas locais (rsync M4→PC)
─ Baixa peças TNU/STJ + OCR          ─ JP escolhe PAJ → "Atuar no PAJ"
─ Detecta prazos                      ─ Claude Opus elabora
─ Classifica (regras + memory)        ─ Anti-alucinação + DOCX
─ Source of truth                     ─ Source of decisão jurídica
       ↓ rsync sob demanda ↑               ↑
       Entrada/dpuscript/<PAJ>/
```

## Como rodar

```
duplo-clique em dpuscript-ui.bat OU atalho no Desktop
→ inicia server em :8001 + abre navegador
```

Dependências em `requirements.txt`. Python 3.13 esperado. Venv em `.venv/`.

## Estrutura

```
dpuscript-ui/
├─ app.py                    # FastAPI app
├─ config.py                 # Paths (DPUSCRIPT_DIR, ENTRADA_DIR, ESTADO_FILE)
├─ dpuscript-ui.bat          # Launcher Windows
├─ stop.py                   # Mata server por PID
├─ routes/                   # Endpoints HTTP
│  ├─ dashboard.py             # /, /api/pajs
│  ├─ paj.py                   # /paj/<paj_norm>, /api/pajs/<paj>
│  ├─ files.py                 # /files/<paj>/<arquivo> (TXT/PDF/JSON)
│  ├─ chat.py                  # /api/elaborar/* (Claude CLI subprocess)
│  ├─ pipeline.py              # /api/pipeline/run (local fallback)
│  ├─ pipeline_monitor.py      # logs do pipeline
│  ├─ watchlist.py             # transito em julgado
│  ├─ sisdpu.py                # /api/paj/{paj}/sis-* (movimentação)
│  ├─ sync.py                  # /api/sync/{reconciliar,baixar-do-m4,atualizar,estado}
│  ├─ correcao.py              # /api/correcao/<paj> (atualizar classif)
│  ├─ feedback.py              # /api/feedback/<paj> (Grok M4 LLM-parsed)
│  ├─ busca.py                 # /api/busca?q= (textual em PAJs locais)
│  └─ planejar.py              # /api/elaborar/{planejar,aprovar-plano,plano}
├─ services/                 # Lógica de negócio
│  ├─ paj_service.py           # Lê metadata + peças + classifica
│  ├─ chat_service.py          # Sessões Claude Code CLI (subprocess)
│  ├─ pipeline_service.py      # Roda preparar_pajs.py local (fallback)
│  ├─ pipeline_monitor_service.py
│  ├─ watchlist_service.py     # transito
│  ├─ sisdpu_service.py        # Playwright headless — automação SISDPU
│  ├─ sync_service.py          # SSH M4 + rsync downstream
│  ├─ nomes_pecas.py           # heurísticas nomeação
│  ├─ claude_service.py        # wrapper auxiliar Claude
│  ├─ correcao_service.py      # wrapper memory.corrigir do dpu-workspace
│  ├─ feedback_parser_service.py  # Grok M4 via Hermes (LLM-parsed feedback)
│  ├─ busca_service.py         # Busca textual c/ cache (60s TTL)
│  └─ planejar_service.py      # Plano R7: Claude CLI → JSON estruturado
├─ templates/                # Jinja2 + Alpine.js + DaisyUI
│  ├─ base.html
│  ├─ dashboard.html           # Lista de PAJs ativos
│  ├─ paj_detail.html          # Detalhe + tabs + modal "Atuar no PAJ"
│  ├─ chat.html                # Chat Claude por PAJ
│  ├─ pipeline.html
│  └─ partials/
└─ static/
   ├─ css/
   └─ js/app.js               # elaborarApp Alpine.js
```

## Fluxos principais

### 1. JP concluiu PAJ no SISDPU → atualizar UI

```
[Reconciliar caixa] (botão verde, ~10s)
  → POST /api/sync/reconciliar
  → SSH não usado — roda preparar_pajs.py --reconciliar-apenas LOCAL
  → Lê caixa SISDPU real via Playwright
  → Move PAJs que sumiram da caixa pra Entrada/dpuscript_arquivados/
  → Atualiza estado/pajs_processados.json
```

### 2. JP quer ver dados frescos (peças, OCR, classificação atualizada)

```
[Baixar do M4] (botão azul, ~2-30s)
  → POST /api/sync/baixar-do-m4
  → scp estado + tar+ssh das pastas M4 → extract local
  → NÃO roda pipeline; usa o que M4 já preparou (cron 4x/dia)
```

### 3. JP quer atualizar JÁ (não esperar próximo cron)

```
Menu ⋯ → [Forçar pipeline M4 agora] (5-20min)
  → POST /api/sync/atualizar
  → SSH M4 + roda preparar_pajs.py completo + rsync downstream

OU (M4 down)

Menu ⋯ → [Local (fallback)]
  → POST /api/pipeline/run
  → Roda preparar_pajs.py NO PC
```

### 4. JP escolhe PAJ pra atuar (peça ou despacho)

```
Abre PAJ → [Atuar no PAJ] (botão)
  → Modal abre + carrega ou gera PLANO via Claude CLI (~60s)
    - POST /api/elaborar/planejar/<paj>
    - Claude lê: metadata + resumo_curto + decisão recente + arquivos locais
                 + regras_atuacao.md (knowledge base editável)
    - Retorna JSON estruturado:
      {tipo_atuacao, tipo_peca, decisao_recorrida_*, analise_completa,
       fontes_auxiliares[], confianca, alertas[]}
  → JP edita campos OU pede refazer com feedback (Claude usa observação JP)
  → JP clica [Aprovar plano e atuar]
    - POST /api/elaborar/aprovar-plano/<paj>  (salva plano_elaboracao.json)
    - POST /api/elaborar/start/<paj>          (dispara Claude CLI elaboração)
  → Modal continua aberto com spinner + polling /api/elaborar/status
  → Done → reload página automático
```

### 5. JP corrige classificação errada (loop de aprendizado)

Widget "Corrigir" no PAJ detail:
- **Dropdown direto**: escolhe classe correta + razão → POST /api/correcao/<paj>
- **Chat livre** (Grok M4 parsea):
  - JP escreve em texto natural ("Essa é decisão do Presidente TNU, não cabe agravo")
  - POST /api/feedback/<paj> → SSH Grok M4 retorna JSON estruturado
  - JP confirma → mesmo endpoint /api/correcao/<paj>
  - Pode marcar "Ensinar regra geral" → cria regra em classif_aprendizadas.jsonl

Memory dual:
1. `dpuscript/memory/classif_aprendizadas.jsonl` — regras REGEX automáticas
   (aplicadas pelo classifier no pipeline)
2. `dpuscript/memory/regras_atuacao.md` — knowledge base TEXTUAL (injetada no
   prompt do Claude quando planeja atuação)

### 6. Busca textual em PAJs

```
Input no dashboard → /api/busca?q=<termo>
  → Indexa: metadata + resumo_curto + movs descrição + OCR peças/decisões
  → Score: meta=10, resumo=5, movs=3, OCR=1 por hit
  → Cache 60s
```

## Endpoints API completos

```
GET  /                                  → dashboard
GET  /paj/<paj_norm>                    → detalhe PAJ
GET  /api/pajs                          → lista PAJs ativos
GET  /api/pajs/<paj>                    → dados completos PAJ
GET  /files/<paj>/<caminho>             → serve arquivo (PDF/TXT/JSON, encoding robusto)
GET  /api/busca?q=&limite=              → busca textual
POST /api/busca/invalidar-cache

GET  /api/sync/reconciliar              → SSE — reconcilia (~10s)
GET  /api/sync/baixar-do-m4             → SSE — rsync sem pipeline (~30s)
GET  /api/sync/atualizar                → SSE — pipeline M4 + sync (5-20min)
GET  /api/sync/estado                   → SSE — só estado.json scp
GET  /api/pipeline/run                  → SSE — pipeline local (fallback)
GET  /api/pipeline/run/<paj>            → SSE — pipeline 1 PAJ

POST /api/elaborar/planejar/<paj>       → JSON — Claude CLI gera plano
POST /api/elaborar/aprovar-plano/<paj>  → salva plano
GET  /api/elaborar/plano/<paj>          → lê plano salvo
POST /api/elaborar/start/<paj>          → dispara Claude CLI elaboração
GET  /api/elaborar/status/<paj>         → status (idle/running/done/error)

POST /api/correcao/<paj>                → corrige classif (Form: classif_correta, razao, padrao_regex?)
GET  /api/correcao/regras               → lista regras aprendidas
POST /api/feedback/<paj>                → Grok M4 LLM-parsea (Form: mensagem)

GET  /api/paj/<paj>/sis-preview         → preview movimentação SISDPU
POST /api/paj/<paj>/sis-execute         → SSE — executa movimentação

GET  /api/watchlist                     → lista trânsitos monitorados
POST /api/watchlist                     → adiciona PAJ ao watch
DELETE /api/watchlist/<id>              → remove
```

## Dependências M4

UI lê **pastas locais PC**, mas pipeline canonical roda no M4. Quando UI quer
atualizar, faz `scp/tar+ssh` do `macmini@192.168.0.102`.

Crons M4 ativos:
- `08h00` — `verificar_pajs_puro.py` (Telegram resumo)
- `08h15` `12h30` `17h30` `21h00` — `preparar_pajs.py` completo (idempotente)
- domingo `03h00` — `limpar_antigos.sh` (PAJs arquivados >90d, logs >7d)

## Convenções

- **Encoding**: TXT/JSON sempre lidos com fallback UTF-8 → CP1252 → Latin-1
- **PAJ normalizado**: formato `YYYY-UUU-NNNNN` (substitui `/` por `-`)
- **PAJ original**: `YYYY/UUU-NNNNN`
- **CLAUDECODE env**: subprocess limpa `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
  `CLAUDE_CODE_SSE_PORT` pra evitar "nested Claude session" error
- **Python subprocess**: `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` no env
- **stdin do Claude CLI**: prompts via stdin (não argument) — Windows command line
  limita 32k chars

## Repos relacionados

- `jppicanco/dpu-workspace` — pipeline `preparar_pajs.py`, memory de aprendizado,
  skills do Claude Code, MCPs (sisdpu, datajud, tnu, stj, bnp, cjf)
- `jppicanco/jarbas-dpu` — config Hermes Agent + mirror M4 + scripts cron
- `jppicanco/dpuscript-ui` — **este projeto**

## Histórico de decisões importantes

- **2026-05-21**: Arquitetura dual-agent definitiva — Claude (PC) elabora,
  Grok 4.3 (M4) tria. Teste comparativo REPROVOU Grok pra elaboração.
- **2026-05-21**: R1 — classificar_caso distingue Relator/Presidente/Colegiado
  (bug de "RELATOR" in blob marcava tudo como agravável).
- **2026-05-21**: R2 — memory `classif_aprendizadas.jsonl` aprende com correções JP.
- **2026-05-21**: R3 — `resumo_curto.md` eager (2KB), `PROMPT_MAX.md` lazy on-click.
- **2026-05-21**: R4 — pipeline migrado pro M4. UI faz sync sob demanda.
- **2026-05-21**: R5-R7 — loop de aprendizado completo (correção direta + chat
  livre Grok + plano editável Claude + regras_atuacao.md knowledge base).
- **2026-05-21**: Idempotência — skip download PDFs já em disco.
- **2026-05-21**: M4 cron 4x/dia (não 1x). UI rsync rápido em vez de SSH+pipeline
  toda vez.

## Para Claude da próxima sessão

Quando começar trabalho aqui:
1. Ler este `CLAUDE.md` (todo)
2. Ler `/e/DPU/dpu-workspace/CLAUDE.md` (pipeline + skills + memory)
3. Ler `/e/JARBAS/DPU/CLAUDE.md` (M4 + Hermes + jarbas-dpu profile)
4. Buscar memória persistente:
   `mcp__plugin_claude-mem_mcp-search__search(query="dpuscript-ui sync")`
