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
        # Estado pra UI de background:
        # "idle" | "running" | "done" | "error"
        self.status: str = "idle"
        self.last_action: str = ""       # "Usando Glob", "Escrevendo peca", etc.
        self.accumulated_text: str = ""  # texto acumulado da resposta atual
        self.summary: str = ""           # resumo final (ultima resposta do Claude)
        self.error: str = ""

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
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
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
                "Analise o PAJ abaixo conforme as instrucoes do CLAUDE.md deste workspace e "
                "**decida autonomamente** entre uma das tres opcoes possiveis:\n\n"
                "1. **Despacho de mero expediente** — quando nao ha prazo nem decisao a atacar "
                "(ex: vista ao MPF, aguardando distribuicao, intimacao simples sem demanda ativa).\n"
                "2. **Arquivamento** — quando houver transito em julgado, perda de objeto ou inviabilidade recursal.\n"
                "3. **Recurso** — quando houver decisao desfavoravel ao assistido com recurso cabivel e viavel.\n\n"
                "**NAO me pergunte nada.** Use seu melhor julgamento com base na analise tecnica (CLAUDE.md, "
                "skills de triagem, pesquisa-juridica, etc). Execute a acao escolhida integralmente: "
                "elabore a peca, valide contra alucinacoes (skill validacao), formate em DOCX/PDF (formatar_peca.py) "
                "e copie pra subpasta de entrada do processo. Tudo sem me consultar.\n\n"
                "Ao final, me apresente um **RESUMO ESTRUTURADO** seguindo exatamente este formato:\n\n"
                "```\n"
                "## Decisao: [DESPACHO | ARQUIVAMENTO | RECURSO — <tipo>]\n\n"
                "### Justificativa\n"
                "<3-5 linhas explicando POR QUE escolheu essa opcao>\n\n"
                "### Peca gerada\n"
                "<nome do arquivo final DOCX/PDF e onde esta salvo>\n\n"
                "### Pontos-chave da peca\n"
                "- <bullet 1>\n"
                "- <bullet 2>\n"
                "- <bullet 3>\n\n"
                "### Se discordar\n"
                "Me diga o que mudar e eu refaco.\n"
                "```\n\n"
                "Se eu responder com discordancia ou correcao, refaca conforme instruido.\n\n"
                "---\n\n"
                f"{prompt_content}"
            )
            # NAO fecha stdin — mantem aberto pra multi-turn
            self.status = "running"
            self.last_action = "iniciando..."
            self.accumulated_text = ""
            self.send_message(instrucao)
            return True

        except FileNotFoundError:
            self.status = "error"
            self.error = "Comando 'claude' nao encontrado."
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.output_queue.put({"type": "error", "text": self.error})
            self.output_queue.put({"type": "done"})
            return False

    def send_message(self, text: str):
        """Envia mensagem do usuario pro Claude via stdin (formato stream-json)."""
        if not self.proc or not self._alive:
            return
        msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
            # Reset estado pra novo turno
            self.status = "running"
            self.last_action = "processando mensagem..."
            self.accumulated_text = ""
        except (BrokenPipeError, OSError):
            self._alive = False
            self.status = "error"
            self.error = "Subprocess morto."

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
                    chunk = delta.get("text", "")
                    self.accumulated_text += chunk
                    self.last_action = "escrevendo resposta..."
                    return {"type": "text", "text": chunk}

            # Tool use start
            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "?")
                    self.last_action = f"usando {tool_name}"
                    return {"type": "tool", "text": f"[usando: {tool_name}]"}

            return None

        # Resultado final de um turno (Claude terminou de responder)
        if etype == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, dict):
                result_text = result_text.get("text", "")
            # Guarda o texto acumulado como summary (e o texto final desse turno)
            if self.accumulated_text.strip():
                self.summary = self.accumulated_text
            elif isinstance(result_text, str) and result_text:
                self.summary = result_text
            self.status = "done"
            self.last_action = "aguardando sua resposta"
            session_id = event.get("session_id", "")[:8]
            return {
                "type": "result",
                "text": result_text if isinstance(result_text, str) else "",
                "session_id": session_id,
            }

        # assistant_full — ignora (ja temos via deltas)
        if etype == "assistant":
            return None

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
