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
            paj_pasta = ENTRADA_DIR / self.paj_norm
            instrucao = (
                "Analise o PAJ abaixo e **decida autonomamente** (sem me perguntar) "
                "entre uma das tres opcoes:\n\n"
                "1. **DESPACHO de mero expediente** — vista ao MPF, aguardando distribuicao, "
                "intimacao simples sem demanda ativa, sem resultado produzido ainda.\n"
                "2. **ARQUIVAMENTO** — um destes tres casos (ver `/skills/arquivamento/SKILL.md`):\n"
                "   a. **Tipo 1** — irrecorribilidade (decisao monocratica do Presidente TNU, art. 15 §1o RI-TNU).\n"
                "   b. **Tipo 2** — inviabilidade de merito (jurisprudencia consolidada contra, sem distinguishing).\n"
                "   c. **Tipo 3** — VITORIA JA OBTIDA (acordao favoravel transitado, acordo cumprido, "
                "resultado atingido). NAO confundir com despacho: se a DPU ja ganhou e so resta baixa burocratica, "
                "e arquivamento por vitoria (com remessa ao Defensor de 1a categoria na Turma Recursal/1o grau), "
                "NAO despacho de aguardar. Essa e uma falha comum — sempre que ha vitoria ja cumprida, e Tipo 3.\n"
                "3. **RECURSO** — decisao desfavoravel com recurso cabivel e viavel.\n\n"
                "**OBRIGATORIO em TODOS os casos**: produzir o TEXTO da peca/despacho, em linguagem "
                "juridica apropriada, pronto pra Joao colar no SISDPU. Nao basta dizer 'e mero expediente' — "
                "redija o despacho mesmo que tenha 3 linhas (ex: 'Tomo ciencia... Aguardar manifestacao do MPF').\n\n"
                "**ESFORCO PROPORCIONAL**:\n"
                f"- **Despacho**: salve o texto em `{paj_pasta}\\despacho.txt` (so TXT, sem DOCX/PDF, sem validacao pesada). Texto curto e direto.\n"
                f"- **Arquivamento**: skill arquivamento, salve em `{paj_pasta}\\`. SE for peca formal, rode validacao + formatar_peca.py.\n"
                f"- **Recurso**: peca completa em `{paj_pasta}\\`, com validacao anti-alucinacao + formatar_peca.py gerando DOCX/PDF.\n\n"
                "Ao final, apresente um **RESUMO ESTRUTURADO**:\n\n"
                "```\n"
                "## Decisao: [DESPACHO | ARQUIVAMENTO | RECURSO — <tipo>]\n\n"
                "### Justificativa\n"
                "<3-5 linhas explicando POR QUE>\n\n"
                "### Texto da peca/despacho\n"
                "```\n"
                "<TEXTO COMPLETO DA PECA/DESPACHO aqui, formatado, pronto pra copiar>\n"
                "```\n\n"
                "### Arquivos gerados\n"
                "- <caminho absoluto do arquivo .txt/.docx/.pdf gerado>\n\n"
                "### Pontos-chave\n"
                "- <bullet 1>\n"
                "- <bullet 2>\n\n"
                "### Se discordar\n"
                "Me diga o que mudar e eu refaco.\n"
                "```\n\n"
                "Se eu responder com discordancia, refaca conforme instruido.\n\n"
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
        finally:
            # Subprocess morreu — libera slot e promove proximo da fila
            try:
                _process_queue()
            except Exception:
                pass

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

            # Tool use input — captura o comando/arquivo sendo acessado
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "input_json_delta":
                    # Concatena partial inputs (Claude vai mandando em chunks)
                    partial = delta.get("partial_json", "")
                    # Simples heuristica: se parece com comando/file path, atualiza last_action
                    if partial and len(partial) > 3:
                        snippet = partial.strip().strip('{},:"').strip()[:60]
                        if snippet:
                            self.last_action = (self.last_action or "") + " " + snippet
                            self.last_action = self.last_action[-120:]  # limita tamanho

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
            # Persiste em disco pra sobreviver reinicio do servidor
            self._persist()
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

    def _persist(self) -> None:
        """Salva status + summary em Entrada/{paj}/elaboracao.json.

        Assim o resultado sobrevive a reinicio do servidor — a UI le do disco
        quando nao ha sessao em memoria.
        """
        try:
            pasta = ENTRADA_DIR / self.paj_norm
            if not pasta.exists():
                return
            import datetime as _dt
            data = {
                "status": self.status,
                "summary": self.summary,
                "last_action": self.last_action,
                "concluido_em": _dt.datetime.now().isoformat(),
            }
            (pasta / "elaboracao.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def ler_elaboracao_disco(paj_norm: str) -> dict | None:
    """Le o estado persistido da elaboracao (ou None se nao existe).

    Precedencia:
    1. elaboracao.json (salvo automaticamente por _persist — tem resumo completo)
    2. Se nao tem elaboracao.json mas TEM arquivo gerado (despacho.txt, *.docx,
       *.pdf na raiz), considera "done" sem resumo detalhado.
    """
    try:
        pasta = ENTRADA_DIR / paj_norm
        if not pasta.exists():
            return None

        f = pasta / "elaboracao.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))

        # Fallback: tem arquivo gerado na raiz?
        IGNORAR = {
            "metadata.json",
            "eventos_tnu.json",
            "datajud.json",
            "PROMPT_MAX.md",
            "elaboracao.json",
            "resumo_curto.md",        # gerado pelo pipeline (R3)
            "prazos_detectados.json", # gerado pelo pipeline (módulo prazos)
            "resumo.md",              # legado
        }
        gerados = [x for x in pasta.iterdir() if x.is_file() and x.name not in IGNORAR]
        if gerados:
            nomes = ", ".join(sorted(x.name for x in gerados))
            return {
                "status": "done",
                "last_action": "arquivos ja gerados",
                "summary": (
                    "(Resumo detalhado nao esta disponivel — este PAJ foi "
                    "elaborado antes da implementacao da persistencia em disco.)\n\n"
                    f"Arquivos gerados na pasta: {nomes}\n\n"
                    "Abra a tab 'Pecas Geradas' do PAJ pra ver/baixar os arquivos, "
                    "ou clique em 'Elaborar Peca' de novo pra regerar o resumo."
                ),
                "concluido_em": "",
            }
        return None
    except Exception:
        return None


# Sessoes ativas (in-memory, single user) + fila de espera
_sessions: dict[str, ChatSession] = {}
_queue: list[str] = []  # paj_norms aguardando slot livre
MAX_PARALLEL = 5


def _count_running() -> int:
    return sum(1 for s in _sessions.values() if s.status == "running")


def _process_queue() -> None:
    """Promove proximos da fila enquanto houver slots livres."""
    while _queue and _count_running() < MAX_PARALLEL:
        next_paj = _queue.pop(0)
        session = _sessions.get(next_paj)
        if not session:
            continue
        # Promove: inicia o subprocess agora
        session.status = "idle"  # reset pra start() funcionar
        session.start()


def get_or_create_session(paj_norm: str) -> ChatSession:
    """Retorna sessao existente ou cria nova (sem iniciar)."""
    if paj_norm in _sessions and _sessions[paj_norm].is_alive():
        return _sessions[paj_norm]
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
    session = ChatSession(paj_norm)
    _sessions[paj_norm] = session
    return session


def start_or_queue(paj_norm: str) -> dict:
    """Inicia sessao se houver slot, senao enfileira. Retorna status atual."""
    existing = _sessions.get(paj_norm)
    # Se ja esta rodando, nao faz nada
    if existing and existing.is_alive() and existing.status == "running":
        return {"status": "running", "last_action": existing.last_action}
    # Se esta na fila, permanece
    if paj_norm in _queue:
        return {"status": "queued", "last_action": "aguardando slot"}

    # Recria sessao limpa
    if existing:
        existing.stop()
    session = ChatSession(paj_norm)
    _sessions[paj_norm] = session

    if _count_running() >= MAX_PARALLEL:
        # Enfileira
        session.status = "queued"
        session.last_action = f"aguardando slot (fila: {len(_queue) + 1})"
        _queue.append(paj_norm)
        return {"status": "queued", "last_action": session.last_action}

    # Ha slot livre — inicia imediatamente
    session.start()
    return {"status": session.status, "last_action": session.last_action}


def stop_session(paj_norm: str):
    if paj_norm in _queue:
        _queue.remove(paj_norm)
    if paj_norm in _sessions:
        _sessions[paj_norm].stop()
        del _sessions[paj_norm]
    _process_queue()


def get_stats() -> dict:
    return {
        "running": _count_running(),
        "queued": len(_queue),
        "max_parallel": MAX_PARALLEL,
    }
