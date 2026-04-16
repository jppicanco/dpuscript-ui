"""Servico para executar Claude Code CLI como subprocess com streaming."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import queue
from typing import AsyncGenerator

from config import DPU_WORKSPACE, ENTRADA_DIR

CLAUDE_CMD = "claude"


async def elaborar_peca(paj_norm: str) -> AsyncGenerator[str, None]:
    """Roda `claude -p` com o PROMPT_MAX do PAJ e faz yield do texto gerado."""
    prompt_path = ENTRADA_DIR / paj_norm / "PROMPT_MAX.md"
    if not prompt_path.exists():
        yield "[ERRO] PROMPT_MAX.md nao encontrado para este PAJ.\n"
        return

    prompt_content = prompt_path.read_text(encoding="utf-8", errors="replace")

    instrucao = (
        "Analise o PAJ abaixo conforme as instrucoes do CLAUDE.md deste workspace. "
        "Faca a triagem (tribunal, decisao, recursos cabiveis, viabilidade) e elabore "
        "a peca cabivel (arquivamento ou recurso). Salve o resultado na subpasta de "
        "entrada do processo.\n\n"
        "---\n\n"
        f"{prompt_content}"
    )

    cmd = [
        CLAUDE_CMD,
        "-p",
        "--verbose",
        "--output-format", "stream-json",
        "--include-partial-messages",
    ]

    yield f"[dpuscript-ui] Elaborando peca para PAJ {paj_norm}...\n"

    q: queue.Queue[str | None] = queue.Queue()

    def _run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(DPU_WORKSPACE),
            )
            # Envia prompt via stdin e fecha
            proc.stdin.write(instrucao.encode("utf-8"))
            proc.stdin.close()

            # Le output linha por linha (stream-json = 1 JSON por linha)
            for line in iter(proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    etype = event.get("type", "")
                    subtype = event.get("subtype", "")

                    # Mensagem parcial (texto chegando em tempo real)
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            q.put(delta.get("text", ""))

                    # Mensagem completa do assistente (fallback se nao tem deltas)
                    elif etype == "assistant":
                        msg = event.get("message", {})
                        content = msg.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                q.put(block.get("text", ""))

                    # Tool use (mostra que Claude esta usando ferramentas)
                    elif etype == "tool_use":
                        tool_name = event.get("name", event.get("tool", "?"))
                        q.put(f"\n[tool: {tool_name}] ")

                    # Tool result
                    elif etype == "tool_result":
                        pass  # Nao mostra resultado de tools (muito verbose)

                    # Resultado final
                    elif etype == "result":
                        # Extrai texto do resultado se nao veio via deltas
                        result_text = ""
                        result = event.get("result", "")
                        if isinstance(result, str) and result:
                            result_text = result
                        elif isinstance(result, dict):
                            result_text = result.get("text", "")

                        if result_text:
                            q.put(f"\n\n{result_text}")

                        cost = event.get("cost_usd")
                        session_id = event.get("session_id", "")
                        q.put(f"\n\n[dpuscript-ui] Concluido (session={session_id[:8]})")
                        if cost:
                            q.put(f" | custo=${cost:.4f}")
                        q.put("\n")

                    # System events (hooks, etc) — ignora silenciosamente
                    elif etype == "system":
                        pass

                except json.JSONDecodeError:
                    # Linha nao-JSON (possivel log ou erro)
                    q.put(text + "\n")

            # Captura stderr
            stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
            proc.wait()

            if proc.returncode != 0:
                q.put(f"\n[ERRO] Claude Code saiu com codigo {proc.returncode}\n")
                if stderr_out:
                    q.put(f"[stderr] {stderr_out[:500]}\n")

        except FileNotFoundError:
            q.put("[ERRO] Comando 'claude' nao encontrado no PATH.\n")
        except Exception as e:
            q.put(f"[ERRO] {type(e).__name__}: {e}\n")
        finally:
            q.put(None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        try:
            chunk = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        if chunk is None:
            break
        yield chunk
