# Status da Central de Atuação — 2026-06-11 08:35

## Resumo
Decisão de **todos os 67 PAJs** concluída. Revise em `http://127.0.0.1:8001/atuacao`.
**Nada foi protocolado nem movimentado no SISDPU** — tudo só preparado.

| Tipo | Qtd |
|------|-----|
| DESPACHO / ciência | 44 |
| ARQUIVAMENTO | 20 |
| RECURSO | 3 |

Confiança: 60 alta · 5 média · 2 baixa.

## Revisar primeiro (baixa confiança)
- `2025-039-15957` (despacho)
- `2025-039-17591` (despacho)

## Recursos pendentes (estágio 2 — peça ainda NÃO redigida)
Estes 3 precisam da elaboração da peça (Opus agêntico + anti-alucinação + DOCX).
Rodar com supervisão de cota (`batch_atuacao.py --stage recurso` — a habilitar):
- `2026-039-02880` — agravo interno (não admissão de PUIL pela Relatora)
- `2026-039-03382` — agravo interno (monocrática do Relator TNU)
- `2026-039-04661` — agravo interno (monocrática do Relator TNU)

## O problema de cota — causa real e correção
- **Sintoma:** rodar a análise estourava a cota de 5h em <25 min / ~18-37 PAJs.
- **Causa real:** o rate limit conta **tokens**, não dólares. Cada processo
  `claude` frio recarregava ~35-40k tokens de **plugins + hooks de SessionStart**
  (o claude-mem despejava memória "$CMEM" em toda chamada) + ~15-19k de definições
  de ferramentas built-in. Isso × 67 chamadas = ~4M+ tokens → estourava a janela.
  Não era o modelo (Opus) nem o prompt — era o **contexto-base repetido**.
- **Correção (commit do batch):**
  1. `CLAUDE_CONFIG_DIR` apontando pra config limpa (só a credencial) → sem
     plugins/hooks/claude-mem/CLAUDE.md global.
  2. `--tools ""` → decisão não usa ferramenta, corta defs built-in.
  3. `--strict-mcp-config` + `--setting-sources project` → sem MCP/settings do user.
  4. Trava de orçamento: para sozinho em 1,5M tokens/run (folga p/ correções).
- **Resultado medido:** de **~66k → ~23k tokens/PAJ**. Os 29 últimos: 659k
  tokens / $2.92, zero rate limit.

## Qualidade da decisão (o passo crítico)
- Bug corrigido: a seleção do documento decisório ordenava por `mtime` (todos
  iguais = hora do sync) → pegava doc velho. Agora ordena pela **data no nome**
  (`YYYY-MM-DD_evNN`, posto pelo pipeline) + prioriza acórdãos/decisões; arquivos
  STJ (nome por hash, sem data) são todos incluídos; o backbone cronológico real
  são as **movimentações** (metadata, seq confiável).
- Confiança virou filtro de QA: revise as `baixa`/`media` primeiro.

## Próximos passos
1. JP revisa a Central de Atuação; corrige o que estiver fora do critério.
2. Correções viram regra em `dpu-workspace/dpuscript/memory/regras_atuacao.md`
   (carregado no prompt da decisão).
3. Rodar estágio 2 (recursos) dos 3 PAJs, com supervisão de cota.
4. Depois de validado, virar cron diário (após o cron 4x/dia do M4).
