"""Reautenticação do Claude CLI no M4 via UI.

Conduz o `claude setup-token` (TUI interativa) através de um pseudo-terminal:
1. iniciar_setup_token() -> abre o setup-token, extrai a URL de OAuth e a devolve.
2. JP abre a URL no navegador, autoriza e recebe um código.
3. enviar_codigo(codigo) -> digita o código no setup-token, que salva o token
   de longa duração em ~/.claude (resolve o 401 sem depender de credencial copiada).

O processo vive entre as duas chamadas (estado de módulo). Roda NO M4 (onde a UI
roda), então o setup-token é um subprocesso local — sem SSH.
"""
from __future__ import annotations

import os
import re
import select
import time

try:
    import pty
    import fcntl
    import termios
    import struct
    _PTY_OK = True
except Exception:  # Windows não tem pty — feature só existe no M4
    _PTY_OK = False

import shutil

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
_CTRL = re.compile(rb"[\x00-\x08\x0e-\x1f]")
_URL = re.compile(rb"https://claude\.com/\S{60,}")

# estado do setup-token em andamento
_sess: dict = {"pid": None, "fd": None, "url": None}


def _limpar() -> None:
    pid, fd = _sess.get("pid"), _sess.get("fd")
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    if pid:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
    _sess.update({"pid": None, "fd": None, "url": None})


def disponivel() -> bool:
    return _PTY_OK and bool(shutil.which("claude"))


def iniciar_setup_token() -> dict:
    """Abre o setup-token e devolve a URL de OAuth pra JP autorizar."""
    if not _PTY_OK:
        return {"ok": False, "erro": "Disponível apenas no M4 (servidor sem pty)."}
    claude = shutil.which("claude") or "claude"
    _limpar()

    pid, fd = pty.fork()
    if pid == 0:
        # filho: vira o setup-token num terminal largo (URL não quebra)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            os.environ.pop(k, None)
        os.environ["COLUMNS"] = "999"
        os.environ["LINES"] = "50"
        os.execvp(claude, [claude, "setup-token"])
        os._exit(127)

    # pai: terminal largo, lê até achar a URL
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 999, 0, 0))
    except Exception:
        pass
    _sess.update({"pid": pid, "fd": fd, "url": None})

    buf = b""
    t0 = time.time()
    while time.time() - t0 < 35:
        try:
            r, _, _ = select.select([fd], [], [], 1)
        except Exception:
            break
        if r:
            try:
                data = os.read(fd, 16384)
            except OSError:
                break
            if not data:
                break
            buf += data
        clean = _CTRL.sub(b"", _ANSI.sub(b"", buf))
        m = _URL.search(clean)
        if m and time.time() - t0 > 8:
            url = re.split(r"[\s\"']", m.group(0).decode(errors="replace"))[0]
            _sess["url"] = url
            return {"ok": True, "url": url}
    _limpar()
    return {"ok": False, "erro": "Não consegui obter a URL do setup-token (timeout)."}


def enviar_codigo(codigo: str) -> dict:
    """Digita o código no setup-token; ele salva o token longo em ~/.claude."""
    fd = _sess.get("fd")
    if fd is None:
        return {"ok": False, "erro": "Nenhuma autenticação em andamento. Gere o link de novo."}
    codigo = (codigo or "").strip()
    if not codigo:
        return {"ok": False, "erro": "Código vazio."}
    try:
        os.write(fd, codigo.encode() + b"\r")
    except OSError as e:
        _limpar()
        return {"ok": False, "erro": f"Falha ao enviar código: {e}"}

    buf = b""
    t0 = time.time()
    while time.time() - t0 < 15:
        try:
            r, _, _ = select.select([fd], [], [], 1)
        except Exception:
            break
        if r:
            try:
                data = os.read(fd, 16384)
            except OSError:
                break
            if not data:
                break
            buf += data
        low = _CTRL.sub(b"", _ANSI.sub(b"", buf)).lower()
        if any(k in low for k in (b"success", b"logged in", b"authenticated",
                                  b"token", b"saved", b"sucesso")):
            _limpar()
            return {"ok": True, "msg": "Token salvo no M4. Claude reautenticado."}
        if any(k in low for k in (b"invalid", b"error", b"failed", b"expired")):
            _limpar()
            return {"ok": False, "erro": "Código inválido ou expirado. Gere o link de novo."}
    _limpar()
    # sem marcador claro: valida testando o claude
    return {"ok": True, "msg": "Código enviado. Verifique o status do Claude."}
