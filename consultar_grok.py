#!/usr/bin/env python3
"""Interrogar o Grok sobre o raciocínio de uma decisão dele num PAJ.

Usado pelo Claude (tela "Discutir" da UI, que roda no M4) para entender a RAIZ
de um erro do Grok ANTES de gravar a regra de correção — em vez de só deduzir.

O Grok não tem memória entre sessões, mas re-recebe aqui sua própria decisão
(atuacao.json) + o contexto que teve (PROMPT_MAX) e é convidado a explicar
honestamente o raciocínio e onde pode ter errado.

Uso:
    python consultar_grok.py --paj 2025-039-10834 \
        --pergunta "Por que mandou acompanhar ate 2028 se o STJ ja deu provimento?"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(os.getenv("DPU_WORKSPACE", "/Users/macmini/dpu-workspace"))
ENTRADA = WORKSPACE / "Entrada" / "dpuscript"
HERMES = Path.home() / ".hermes/hermes-agent/venv/bin/hermes"
GROK_MODEL = "grok-4.20-0309-reasoning"
GROK_PROVIDER = "xai-oauth"
TIMEOUT = 180


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paj", required=True)
    ap.add_argument("--pergunta", required=True)
    args = ap.parse_args()

    pasta = ENTRADA / args.paj
    if not pasta.exists():
        print(f"ERRO: PAJ {args.paj} nao encontrado em {ENTRADA}", file=sys.stderr)
        return 1

    atuacao = {}
    f = pasta / "atuacao.json"
    if f.exists():
        try:
            atuacao = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    decisao_txt = json.dumps(atuacao, ensure_ascii=False, indent=2) if atuacao else "(sem atuacao.json)"

    prompt_max = ""
    pm = pasta / "PROMPT_MAX.md"
    if pm.exists():
        prompt_max = pm.read_text(encoding="utf-8")[:8000]

    prompt = (
        "Voce e' o Grok, o sistema que decidiu a atuacao deste PAJ da Defensoria "
        "Publica da Uniao. Um revisor (Claude) esta analisando sua decisao e quer "
        "ENTENDER seu raciocinio para corrigir um possivel erro.\n\n"
        "Responda em texto livre, direto e honesto. Explique seu raciocinio real: "
        "em que se baseou, que trecho do processo te levou a essa conclusao, e se "
        "voce ve agora algum ponto em que pode ter errado. NAO produza JSON nem "
        "peca formal — so a explicacao do raciocinio.\n\n"
        f"## Sua decisao registrada sobre o PAJ {args.paj}:\n{decisao_txt}\n\n"
        f"## Contexto do PAJ que voce teve (resumido):\n{prompt_max}\n\n"
        f"## Pergunta do revisor:\n{args.pergunta}\n"
    )

    if not HERMES.exists():
        print(f"ERRO: hermes nao encontrado em {HERMES}", file=sys.stderr)
        return 1

    cmd = [str(HERMES), "chat", "-Q", "-q", prompt, "-m", GROK_MODEL,
           "--provider", GROK_PROVIDER, "--max-turns", "5"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        print("ERRO: timeout consultando Grok", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print(f"ERRO hermes rc={proc.returncode}: {(proc.stderr or '')[-300:]}", file=sys.stderr)
        return 1

    # Saida do hermes -Q comeca com uma linha "session_id: ..." — descartar.
    linhas = (proc.stdout or "").splitlines()
    if linhas and linhas[0].strip().startswith("session_id:"):
        linhas = linhas[1:]
    print("\n".join(linhas).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
