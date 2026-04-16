"""Servico de chat interativo com Claude Code CLI via stream-json bidirecional."""

from __future__ import annotations

import json
import subprocess
import threading
import queue
from pathlib import Path

from config import DPU_WORKSPACE, ENTRADA_DIR

CLAUDE_CMD = "claude"


class ChatSession:
    """Sessao interativa com Claude Code CLI."""

    def __init__(self, paj_norm: str):
        self.paj_norm = paj_norm
        self.output_queue: queue.Queue[dict] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._alive = False

    def start(self) -> bool:
        """Inicia o subprocess Claude Code em modo stream-json."""
        prompt_path = ENTRADA_DIR / self.paj_norm / "PROMPT_MAX.md"
        if not prompt_path.exists():
            self.output_queue.put({"type": "error", "text": "PROMPT_MAX.md nao encontrado."})
            self.output_queue.put({"type": "done"})
            return False

        cmd = [
            CLAUDE_CMD,
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(DPU_WORKSPACE),
            )
            self._alive = True

            # Thread pra ler stdout continuamente
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()

            # Envia PROMPT_MAX como prompt direto (texto, nao JSON)
            prompt_content = prompt_path.read_text(encoding="utf-8", errors="replace")
            instrucao = (
                "Analise o PAJ abaixo conforme as instrucoes do CLAUDE.md deste workspace. "
                "Faca a triagem (tribunal, decisao, recursos cabiveis, viabilidade). "
                "Se tiver duvida sobre o que fazer (arquivar ou recorrer), ME PERGUNTE antes de elaborar. "
                "Aguarde minha resposta antes de prosseguir.\n\n"
                "---\n\n"
                f"{prompt_content}"
            )
            try:
                self.proc.stdin.write(instrucao.encode("utf-8"))
                self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                self._alive = False

            return True

        except FileNotFoundError:
            self.output_queue.put({"type": "error", "text": "Comando 'claude' nao encontrado."})
            self.output_queue.put({"type": "done"})
            return False
        except Exception as e:
            self.output_queue.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
            self.output_queue.put({"type": "done"})
            return False

    def _read_output(self):
        """Le stdout do Claude e coloca eventos na queue."""
        try:
            for line in iter(self.proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    parsed = self._parse_event(event)
                    if parsed:
                        self.output_queue.put(parsed)
                except json.JSONDecodeError:
                    self.output_queue.put({"type": "text", "text": text + "\n"})

            self.proc.wait()
            self._alive = False
            self.output_queue.put({"type": "done"})
        except Exception as e:
            self.output_queue.put({"type": "error", "text": str(e)})
            self.output_queue.put({"type": "done"})
            self._alive = False

    def _parse_event(self, event: dict) -> dict | None:
        """Converte evento stream-json do Claude em formato simplificado pro frontend."""
        etype = event.get("type", "")

        # stream_event — wrapper dos eventos da API Anthropic
        if etype == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type", "")

            # Texto parcial (streaming em tempo real)
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    return {"type": "text", "text": delta.get("text", "")}

            # Tool use start
            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    return {"type": "tool", "text": f"[usando: {block.get('name', '?')}]"}

            return None

        # Mensagem completa do assistente (inclui texto final)
        if etype == "assistant":
            msg = event.get("message", {})
            content = msg.get("content", [])
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                return {"type": "assistant_full", "text": "".join(texts)}
            return None

        # Resultado final
        if etype == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, dict):
                result_text = result_text.get("text", "")
            session_id = event.get("session_id", "")[:8]
            return {
                "type": "result",
                "text": result_text if isinstance(result_text, str) else "",
                "session_id": session_id,
            }

        # System events — ignora
        if etype == "system":
            return None

        # rate_limit_event — ignora
        if etype == "rate_limit_event":
            return None

        return None

    def is_alive(self) -> bool:
        return self._alive

    def stop(self):
        """Encerra o subprocess."""
        self._alive = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


# Sessoes ativas (in-memory, single user)
_sessions: dict[str, ChatSession] = {}


def get_or_create_session(paj_norm: str) -> ChatSession:
    """Retorna sessao existente ou cria nova."""
    if paj_norm in _sessions and _sessions[paj_norm].is_alive():
        return _sessions[paj_norm]
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
    session = ChatSession(paj_norm)
    _sessions[paj_norm] = session
    return session


def stop_session(paj_norm: str):
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
        del _sessions[paj_norm]
