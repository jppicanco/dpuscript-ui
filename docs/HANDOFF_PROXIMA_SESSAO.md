# Handoff — Central de Atuação (próxima sessão) — 2026-06-11

## Como retomar (IMPORTANTE: economia de cota)
- **Rode a sessão de orquestração em modelo leve** (`/model sonnet`) — orquestrar batch NÃO precisa de Opus/Fable. A maior queima de cota foi ESTA sessão (Opus 4.8 [1m] × ~50 re-invocações reprocessando histórico gigante), NÃO o batch.
- **Monitores só no FIM/erro** (nunca por PAJ). Respostas mínimas durante runs.
- O trabalho pesado (peças) roda no `claude` CLI via `claude_runner` em **Opus** — isso é separado do modelo da sessão de orquestração.

## Estado atual (o que está pronto)
- **Decisões: 67/67 PAJs prontas.** Central de Atuação em `http://127.0.0.1:8001/atuacao`. Distribuição: 44 despacho, 20 arquivamento, 3 recurso. Confiança: 60 alta, 5 média, 2 baixa (`2025-039-15957`, `2025-039-17591` — revisar 1º).
- **Recursos (estágio 2): 1 de 3 pronto.**
  - `2026-039-02880` ✅ peça gerada (agravo_tnu_*.docx/pdf/txt). MAS: verificar se citou precedentes do MCP (talvez não tenha pesquisado).
  - `2026-039-03382` ⏳ pendente (foi interrompido no meio). Tem fonte ok (4 docs). Só rodar.
  - `2026-039-04661` ⏳ pendente (parou na análise, não escreveu peça). Tem a recorrida (PUIL Vitoria, 13k). Correção já aplicada no prompt.

## Causa-raiz do estouro de cota (RESOLVIDA — não repetir)
A cota é por TOKENS/janela, ponderada por modelo. 3 causas (todas tratadas):
1. **Concorrência → rate limit POR MINUTO (TPM).** Rodar N chamadas `claude` em paralelo (workers>1) estoura o TPM ("limitando temporariamente"), cascata. Desktop do JP = 1 por vez = nunca trava. **Fix: lock global (`claude_runner`) = 1 chamada/vez no sistema todo.**
2. **Vazamento de processos.** claude CLI gera filhos (MCP servers/sub-procs) que não morrem no Windows. **Fix: reaper de órfãos (mata processo com pai morto; nunca mata sessão interativa).**
3. **Esta sessão de orquestração** (Opus pesado + muitas re-invocações) — o maior dreno. **Fix: sessão leve + monitores FIM-only.**
- Erro de medição corrigido: `cache_read` quase não conta pra cota. Medir por % real (Configurações→Uso). Cota real ≈ 13-18M token-equiv; 1 recurso agêntico ≈ 15-20% dela.

## Arquitetura (arquivos)
- `claude_runner.py` — **TODA chamada claude DEVE passar por `run_claude`** (lock global + reaper + tree-kill). `python claude_runner.py reap` mata órfãos avulsos.
- `batch_atuacao.py` — 2 estágios:
  - `--stage decisao` (default): Opus, 1 chamada, SEM ferramentas, config limpa (CLAUDE_CONFIG_DIR temp só com credencial), `--strict-mcp-config --setting-sources project --tools ""`. ~23k tok/PAJ. Pula concluídos (`--force` refaz).
  - `--stage recurso`: Opus agêntico, cwd=workspace (skills + MCP bnp/cjf), alimenta o TEXTO da recorrida + exige MCP jurisprudencial + força escrita do .txt/.docx. Trava `BATCH_MAX_TOKENS`.
  - workers=1 (lock serializa de qq forma).
- `services/atuacao_service.py` + `routes/atuacao.py` + `templates/atuacao.html` — Central de Atuação.
- Modelo: **decisão=Opus, escrita recurso=Opus** (decisão do JP). Leitura/pesquisa PODE ser Sonnet (otimização futura). **MCP jurisprudencial (bnp-api/cjf) é FUNDAMENTAL no recurso.**

## PRÓXIMO PASSO (1ª coisa da próxima sessão)
Testar **1 recurso** (`2026-039-03382`) com cota fresca, sessão leve, monitor FIM-only:
```
cd E:\DPU\dpuscript-ui
BATCH_MAX_TOKENS=20000000 python batch_atuacao.py --stage recurso --only 2026-039-03382 --workers 1
```
Verificar: (a) gerou .txt + .docx? (b) `atuacao.json` campo PRECEDENTES_USADOS / citou jurisprudência do MCP? (c) consumo (% real). Se OK → rodar `04661`. Se MCP não disparar → debugar bnp-api/cjf (servers em `E:\DPU\lista-trf\mcp\`).

Depois de validado: virar cron (decisões diárias após o cron 4x/dia do M4 no Mac; recursos sob supervisão).

## Repos / commits relevantes (dpuscript-ui, branch master)
- `9933ce3` fix recurso (recorrida+MCP+escrita) · `8ee1ec4` claude_runner (lock+reaper) · `3f2e562` decisão leve · `35a91a8` skills · `427e4e1` docgen · `55ded76` chat_livre · `f8f47b1` healthcheck.
