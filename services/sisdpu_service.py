"""Servico de integração com o SISDPU via Playwright.

Responsabilidades:
- get_preview(): lê metadados + despacho e retorna preview sem executar nada
- executar_movimentacao(): abre Edge visível, faz login, executa fluxo completo
  e faz yield de linhas de log via async generator (para SSE).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import AsyncGenerator

from config import DPUSCRIPT_DIR, ENTRADA_DIR

# ---------------------------------------------------------------------------
# Mapeamento classificação → fase SISDPU
# ---------------------------------------------------------------------------

FASE_MAP: dict[str, str] = {
    "ARQUIVADO_VITORIA_PROVIMENTO": "Arquivado. Com vitória total na via judicial",
    "DECISAO_MONOCRATICA_TNU_AGRAVAVEL": "Arquivado. Inviabilidade recursal",
    "DECISAO_MONOCRATICA_STJ_AGRAVAVEL": "Arquivado. Inviabilidade recursal",
}

# Credenciais vêm do .env no DPUSCRIPT_DIR
DOTENV_PATH = DPUSCRIPT_DIR / ".env"

# Perfil persistente do Edge para o SISDPU
SISDPU_PROFILE_DIR = DPUSCRIPT_DIR / "sisdpu_profile"

SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"

# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _carregar_credenciais() -> tuple[str, str]:
    """Lê SISDPU_USERNAME e SISDPU_PASSWORD do .env em DPUSCRIPT_DIR."""
    import os
    from dotenv import load_dotenv
    load_dotenv(DOTENV_PATH, override=False)
    usuario = os.environ.get("SISDPU_USERNAME", "")
    senha = os.environ.get("SISDPU_PASSWORD", "")
    return usuario, senha


def _ler_metadata(paj_norm: str) -> dict:
    """Lê metadata.json do PAJ. Retorna dict vazio se não encontrar."""
    meta_path = ENTRADA_DIR / paj_norm / "metadata.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ler_despacho(paj_norm: str, despacho_nome: str) -> str:
    """Lê o texto do arquivo de despacho. Retorna string vazia se falhar."""
    despacho_path = ENTRADA_DIR / paj_norm / despacho_nome
    if not despacho_path.exists():
        return ""
    try:
        return despacho_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _inferir_fase_do_despacho(texto: str) -> str:
    """
    Infere a fase SISDPU mais provável a partir do conteúdo do despacho.

    Usa correspondência por palavras-chave em português.
    Retorna a string exata do dropdown do SISDPU ou "" se não identificar.

    Ordem de prioridade: vitória > arquivamento > recurso > aguardar.
    """
    t = texto.lower()

    # ── Arquivamento com vitória ────────────────────────────────────────────
    _VITORIA = [
        "vitória total", "vitória parcial", "arquivado com vitória",
        "deu provimento ao recurso da dpu", "deu provimento ao pedilef",
        "provimento ao pedido", "favorável ao assistido",
        "concessão do benefício", "concedido o benefício",
        "restabelecer o benefício", "restabelecimento do benefício",
        "procedente", "acolhida a tese da dpu",
    ]
    if any(kw in t for kw in _VITORIA):
        return "Arquivado. Com vitória total na via judicial"

    # ── Arquivamento por inviabilidade recursal ─────────────────────────────
    _INVIABILIDADE = [
        "inviabilidade recursal", "esgotadas as vias recursais",
        "inviável o recurso", "sem recurso cabível", "irrecorrível",
        "não cabe recurso", "nao cabe recurso",
        "arquivamento por inviabilidade", "arquivar por inviabilidade",
        "arquivamento é de rigor", "arquivamento e de rigor",
        "decurso de prazo", "prazo já decorrido",
    ]
    if any(kw in t for kw in _INVIABILIDADE):
        return "Arquivado. Inviabilidade recursal"

    # ── Recurso protocolado ─────────────────────────────────────────────────
    _RECURSO = [
        "recurso interposto", "recurso foi protocolado", "petição protocolada",
        "protocolo do recurso", "foi protocolado", "interpomos recurso",
        "peticionei", "protocolamos", "recurso de agravo",
        "embargos de declaração protocolados", "agravo interno protocolado",
        "recurso especial interposto", "resp interposto",
        "pedilef interposto", "pedilef foi interposto",
    ]
    if any(kw in t for kw in _RECURSO):
        return "Petição. Recurso"

    # ── Aguardando tramitação judicial/administrativa ───────────────────────
    _AGUARDAR = [
        "aguardar", "aguarde", "aguardando", "aguarda-se",
        "pauta de julgamento", "sessão virtual", "sessão de julgamento",
        "incluído em pauta", "incluido em pauta",
        "não há providência", "nao ha providencia",
        "sem providência", "sem providencia",
        "aguardar o resultado", "aguardar a decisão", "aguardar a decisao",
        "aguardar o julgamento", "julgamento pendente",
        "em julgamento", "será julgado", "sera julgado",
        "aguardar o trânsito", "aguardar o transito",
        "nenhuma providência a adotar", "nenhuma providencia a adotar",
    ]
    if any(kw in t for kw in _AGUARDAR):
        return "Aguardando tramitação judicial/Administrativa"

    return ""


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def get_preview(paj_norm: str, despacho_nome: str) -> dict:
    """
    Retorna preview do que seria executado no SISDPU, sem fazer nada.

    Retorna dict com:
      paj, classificacao, fase, despacho_texto (primeiros 500 chars),
      despacho_completo, erro (se não conseguiu determinar a fase).
    """
    meta = _ler_metadata(paj_norm)
    classificacao = meta.get("classificacao", "")
    paj_numero = meta.get("paj", paj_norm)

    despacho_completo = _ler_despacho(paj_norm, despacho_nome)
    despacho_texto = despacho_completo[:500] if despacho_completo else ""

    fase = FASE_MAP.get(classificacao, "")
    erro = None
    aviso = None  # aviso leve — não bloqueia o modal
    fase_inferida = False

    if not despacho_completo:
        erro = f"Arquivo de despacho não encontrado: {despacho_nome}"
    elif not classificacao:
        erro = "Classificação não encontrada em metadata.json"
    elif not fase:
        # Tenta inferir a fase pelo conteúdo do despacho
        fase = _inferir_fase_do_despacho(despacho_completo)
        if fase:
            fase_inferida = True
            aviso = f"Fase inferida do texto do despacho (classificação: '{classificacao}'). Confirme antes de executar."
        else:
            aviso = f"Fase não identificada para '{classificacao}'. Selecione a fase manualmente no dropdown."

    return {
        "paj": paj_numero,
        "classificacao": classificacao,
        "fase": fase,           # vazio → dropdown fica sem seleção pré-definida
        "fase_inferida": fase_inferida,
        "despacho_texto": despacho_texto,
        "despacho_completo": despacho_completo,
        "erro": erro,
        "aviso": aviso,
    }


async def executar_movimentacao(
    paj_norm: str,
    despacho_nome: str,
    fase: str,
) -> AsyncGenerator[str, None]:
    """
    Executa a movimentação no SISDPU via Playwright (Edge visível).

    Yield de strings de log prefixadas com "[SISDPU]".
    Linha final: "[SISDPU] ✅ PAJ concluído com sucesso" ou
                 "[SISDPU] ❌ Erro: {msg}".
    """

    def log(msg: str) -> str:
        return f"[SISDPU] {msg}"

    # Fila para comunicação entre coroutines
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def _put(msg: str | None) -> None:
        """Envia mensagem para a fila SSE. Seguro de chamar de qualquer contexto."""
        try:
            q.put_nowait(msg)
        except Exception:
            pass  # fila cheia ou loop encerrado — ignorar

    async def _run_playwright() -> None:
        """Executa todo o fluxo Playwright em coroutine assíncrona."""
        from playwright.async_api import async_playwright

        despacho_completo = _ler_despacho(paj_norm, despacho_nome)
        if not despacho_completo:
            _put(log(f"❌ Erro: arquivo de despacho não encontrado: {despacho_nome}"))
            _put(None)
            return

        usuario, senha = _carregar_credenciais()
        if not usuario or not senha:
            _put(log("❌ Erro: credenciais SISDPU_USERNAME / SISDPU_PASSWORD não configuradas"))
            _put(None)
            return

        SISDPU_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        _put(log("Iniciando Edge (visível)..."))

        async with async_playwright() as p:
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(SISDPU_PROFILE_DIR),
                    headless=False,
                    channel="msedge",
                    args=["--start-maximized"],
                )
            except Exception as e:
                _put(log(f"❌ Erro ao abrir Edge: {e}"))
                _put(None)
                return

            page = context.pages[0] if context.pages else await context.new_page()

            try:
                # ----------------------------------------------------------
                # PASSO 1 — Verificar login / fazer login
                # ----------------------------------------------------------
                _put(log("Navegando para o SISDPU..."))

                # Vai direto para a caixa de entrada — se sessão ativa, cai lá direto.
                # Se não estiver logado, o SISDPU redireciona para login.xhtml.
                await page.goto(f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml")
                await page.wait_for_load_state("networkidle", timeout=20000)

                # O SISDPU tem 3 estados possíveis após o goto:
                #   A) URL tem caixaEntrada → logado e na caixa ✓
                #   B) URL é login.xhtml + "já efetuou login" → sessão ativa,
                #      mas o sistema mostra tela de redirecionamento — clicar no link
                #   C) URL é login.xhtml + formulário de login → sessão expirada

                if "caixaEntrada" not in page.url:
                    conteudo = (await page.content()).lower()

                    if "efetuou login" in conteudo:
                        # Caso B — sessão ativa, clicar em "Clique para voltar ao SIS-DPU"
                        _put(log("Sessão ativa detectada — clicando em 'Voltar ao SIS-DPU'..."))
                        try:
                            link = page.locator("a:has-text('Clique para voltar'), a:has-text('voltar ao SIS')").first
                            await link.click()
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            # Fallback: navegar direto para caixaEntrada via JS
                            await page.evaluate(
                                f"window.location.href = '{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml'"
                            )
                            await page.wait_for_load_state("networkidle", timeout=15000)

                    else:
                        # Caso C — formulário de login
                        _login_auto_ok = False
                        if usuario and senha:
                            _put(log("Sessão expirada — tentando login automático..."))
                            try:
                                await page.fill('[id="frmLogin:epaj_input_usuario"]', usuario)
                                await page.fill('[id="frmLogin:epaj_input_senha"]', senha)
                                await page.evaluate(
                                    'PrimeFaces.ab({s:"frmLogin:loginButton",f:"frmLogin"})'
                                )
                                await page.wait_for_url("**/caixaEntrada**", timeout=15000)
                                _login_auto_ok = True
                            except Exception:
                                pass

                        if not _login_auto_ok and "caixaEntrada" not in page.url:
                            _put(log("⚠️ Login automático falhou. Faça login manualmente no Edge. Aguardando 60s..."))
                            try:
                                await page.wait_for_url("**/caixaEntrada**", timeout=60000)
                            except Exception:
                                pass

                    # Verificação final
                    if "caixaEntrada" not in page.url:
                        _put(log("❌ Não foi possível acessar a Caixa de Entrada"))
                        return

                _put(log("✓ Autenticado — Caixa de Entrada aberta"))

                # ----------------------------------------------------------
                # PASSO 2 — Encontrar o PAJ na tabela
                # ----------------------------------------------------------
                meta = _ler_metadata(paj_norm)
                paj_numero = meta.get("paj", "")
                if not paj_numero:
                    _put(log("❌ Erro: número do PAJ não encontrado em metadata.json"))
                    return

                _put(log(f"Procurando PAJ {paj_numero} na caixa de entrada..."))
                # Link com o número do PAJ
                link_paj = page.locator(f"a:has-text('{paj_numero}')").first
                try:
                    await link_paj.wait_for(state="visible", timeout=10000)
                except Exception:
                    _put(log(f"❌ Erro: PAJ {paj_numero} não encontrado na caixa de entrada"))
                    return
                _put(log(f"✓ PAJ {paj_numero} encontrado"))

                # ----------------------------------------------------------
                # PASSO 3 — Clicar no PAJ e extrair id + idTramite da URL
                # ----------------------------------------------------------
                await link_paj.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                _put(log("✓ Página de detalhe do PAJ aberta"))

                current_url = page.url
                _put(log(f"URL: {current_url}"))

                # Extrair parâmetros da URL
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                paj_id = params.get("id", [None])[0]
                id_tramite = params.get("idTramite", [None])[0]

                if not paj_id or not id_tramite:
                    _put(log(f"❌ Erro: não foi possível extrair id/idTramite da URL: {current_url}"))
                    return
                _put(log(f"✓ id={paj_id}, idTramite={id_tramite}"))

                # ----------------------------------------------------------
                # PASSO 4 — Clicar "Movimentar" no nav superior
                # ----------------------------------------------------------
                _put(log("Clicando em 'Movimentar' (nav superior)..."))
                # O link no nav superior — não o botão azulado
                btn_mov_nav = page.locator("nav a:has-text('Movimentar'), .ui-menubar a:has-text('Movimentar'), a[href*='movimenta']:has-text('Movimentar')").first
                try:
                    await btn_mov_nav.wait_for(state="visible", timeout=8000)
                    await btn_mov_nav.click()
                except Exception:
                    # Fallback: navegar diretamente para a URL de movimentação
                    url_mov = f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml?id={paj_id}&idTramite={id_tramite}"
                    _put(log(f"  Navegando direto para: {url_mov}"))
                    await page.goto(url_mov)
                await page.wait_for_load_state("networkidle", timeout=20000)
                _put(log("✓ Página de movimentação aberta"))

                # ----------------------------------------------------------
                # PASSO 5 — Selecionar Fase no SelectOneMenu PrimeFaces
                # ----------------------------------------------------------
                _put(log(f"Selecionando fase: '{fase}'..."))
                try:
                    # Tenta select nativo primeiro
                    select_fase = page.locator("select[id*='fase']").first
                    await select_fase.select_option(label=fase)
                except Exception:
                    # PrimeFaces SelectOneMenu
                    dropdown = page.locator(".ui-selectonemenu").first
                    await dropdown.click()
                    await _pf_ajax(page)
                    item = page.locator(f"li.ui-selectonemenu-item:has-text('{fase[:30]}')").first
                    await item.click()
                await _pf_ajax(page)
                _put(log("✓ Fase selecionada"))

                # ----------------------------------------------------------
                # PASSO 6 — Inserir despacho no CKEditor
                # ----------------------------------------------------------
                _put(log("Inserindo despacho no editor..."))
                await _inserir_ckeditor(page, despacho_completo)
                _put(log("✓ Despacho inserido"))

                # ----------------------------------------------------------
                # PASSO 7 — Clicar botão "Movimentar" (último da página)
                # ----------------------------------------------------------
                _put(log("Clicando em 'Movimentar' (botão de submissão)..."))
                btn_submit = page.locator(
                    "input[value='Movimentar'], button:has-text('Movimentar')"
                ).last
                await btn_submit.click()
                await _pf_ajax(page)
                _put(log("✓ Formulário submetido"))

                # ----------------------------------------------------------
                # PASSO 8 — Modal "Cadastrar Honorário?" → Não
                # ----------------------------------------------------------
                _put(log("Modal 'Cadastrar Honorário?' → Não..."))
                clicou = await _clicar_modal(page, "Não", timeout=5000)
                if clicou:
                    _put(log("✓ Honorários: Não"))
                else:
                    _put(log("  (modal de honorários não apareceu — continuando)"))

                # ----------------------------------------------------------
                # PASSO 9 — Modal "Tramitar processo(s)?" → Sim
                # ----------------------------------------------------------
                _put(log("Modal 'Tramitar processo(s)?' → Sim..."))
                clicou = await _clicar_modal(page, "Sim", timeout=20000)
                if not clicou:
                    _put(log("❌ Erro: modal 'Tramitar processo(s)' não respondeu — verifique o browser"))
                    return
                await page.wait_for_load_state("networkidle", timeout=20000)
                _put(log("✓ Tramitar: Sim"))

                # ----------------------------------------------------------
                # PASSO 10 — Selecionar destino "01. COMUNICACAO"
                # ----------------------------------------------------------
                _put(log("Selecionando destino '01. COMUNICACAO'..."))
                try:
                    destino = page.locator(
                        "select[id*='destino'], select[id*='tramite'], select[id*='Destino']"
                    ).first
                    await destino.wait_for(state="visible", timeout=10000)
                    try:
                        await destino.select_option(value="190")
                    except Exception:
                        await destino.select_option(label="01. COMUNICACAO")
                except Exception:
                    # PrimeFaces SelectOneMenu
                    dropdown = page.locator(".ui-selectonemenu").first
                    await dropdown.click()
                    await _pf_ajax(page)
                    item = page.locator("li.ui-selectonemenu-item:has-text('COMUNICACAO')").first
                    await item.click()
                await _pf_ajax(page)
                _put(log("✓ Destino selecionado: 01. COMUNICACAO"))

                # ----------------------------------------------------------
                # PASSO 11 — Inserir resumo na descrição do trâmite
                # ----------------------------------------------------------
                resumo = "Tramitado ao setor de Comunicação para as providências cabíveis."
                _put(log(f"Inserindo resumo: '{resumo}'..."))
                try:
                    await _inserir_ckeditor(page, resumo)
                except Exception:
                    try:
                        textarea = page.locator(
                            "textarea[id*='descricao'], textarea[id*='resumo']"
                        ).first
                        await textarea.fill(resumo)
                    except Exception:
                        _put(log("  (campo de resumo não encontrado — continuando)"))
                _put(log("✓ Resumo inserido"))

                # ----------------------------------------------------------
                # PASSO 12 — Clicar "Tramitar"
                # ----------------------------------------------------------
                _put(log("Clicando em 'Tramitar'..."))
                btn_tramitar = page.locator(
                    "input[value='Tramitar'], button:has-text('Tramitar')"
                ).first
                await btn_tramitar.click()
                await _pf_ajax(page)
                _put(log("✓ Tramitar clicado"))

                # ----------------------------------------------------------
                # PASSO 13 — Modal "Movimentar novamente?" → Não
                # ----------------------------------------------------------
                _put(log("Modal 'Movimentar novamente?' → Não..."))
                clicou = await _clicar_modal(page, "Não", timeout=6000)
                if clicou:
                    _put(log("✓ Movimentar novamente: Não"))
                else:
                    _put(log("  (modal não apareceu — continuando)"))
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # ----------------------------------------------------------
                # PASSO 14/15 — Voltar ao detalhe do PAJ
                # ----------------------------------------------------------
                _put(log("Voltando para o detalhe do PAJ..."))
                try:
                    btn_voltar = page.locator("a:has-text('Voltar'), input[value='Voltar']").first
                    await btn_voltar.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    try:
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                await _pf_ajax(page)
                _put(log("✓ De volta ao detalhe do PAJ"))

                # ----------------------------------------------------------
                # PASSO 17 — Concluir PAJ da caixa de entrada
                # ----------------------------------------------------------
                _put(log("Clicando em 'Concluir PAJ da minha caixa de entrada'..."))
                btn_concluir = page.locator(
                    "input[value*='Concluir PAJ'], button:has-text('Concluir PAJ')"
                ).first
                try:
                    await btn_concluir.wait_for(state="visible", timeout=10000)
                    await btn_concluir.click()
                    await _pf_ajax(page)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as e:
                    _put(log(f"  Aviso: botão Concluir PAJ não encontrado: {e}"))
                _put(log("✓ PAJ concluído"))

                _put(log("✅ PAJ concluído com sucesso"))

            except Exception as exc:
                _put(log(f"❌ Erro: {exc}"))
            finally:
                await context.close()

        _put(None)  # sentinel

    async def _run_in_background():
        """Wrapper que GARANTE que o sentinel None chega na fila, custe o que custar."""
        try:
            await _run_playwright()
        except Exception as exc:
            # Exceção não tratada em _run_playwright — reportar e encerrar
            _put(f"[SISDPU] ❌ Erro fatal: {type(exc).__name__}: {exc}")
        finally:
            # Sentinel sempre enviado — nunca mais trava esperando para sempre
            _put(None)

    # Iniciar task
    task = asyncio.create_task(_run_in_background())

    # Fazer yield das mensagens à medida que chegam
    try:
        while True:
            msg = await asyncio.wait_for(q.get(), timeout=180.0)  # 3 min timeout de segurança
            if msg is None:
                break
            yield msg
    except asyncio.TimeoutError:
        yield "[SISDPU] ❌ Timeout: automação não respondeu em 3 minutos"
    finally:
        # Se o consumidor encerrou (ex: cliente desconectou), cancelar a task
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Helpers Playwright (usados dentro da coroutine)
# ---------------------------------------------------------------------------

_WAIT_PF_AJAX = """() => new Promise((resolve) => {
    const check = () => {
        if (typeof PrimeFaces === 'undefined' || !PrimeFaces.ajax || !PrimeFaces.ajax.Queue || PrimeFaces.ajax.Queue.isEmpty()) {
            resolve();
        } else { setTimeout(check, 100); }
    };
    check();
})"""


async def _pf_ajax(page) -> None:
    """Aguarda PrimeFaces terminar todos os requests AJAX pendentes."""
    try:
        await page.evaluate(_WAIT_PF_AJAX)
    except Exception:
        await page.wait_for_timeout(800)


async def _inserir_ckeditor(page, texto: str, instancia: str = "formHonorario_editorValue") -> None:
    """Insere texto no CKEditor 4 via setData()."""
    if "<p>" not in texto and "<br" not in texto:
        paragrafos = [f"<p>{linha}</p>" for linha in texto.split("\n") if linha.strip()]
        html = "\n".join(paragrafos) or f"<p>{texto}</p>"
    else:
        html = texto

    await page.evaluate(
        """([instancia, html]) => {
            if (typeof CKEDITOR === 'undefined') throw new Error('CKEditor não encontrado');
            const editor = CKEDITOR.instances[instancia];
            if (!editor) {
                const keys = Object.keys(CKEDITOR.instances);
                if (keys.length === 0) throw new Error('Nenhuma instância CKEditor encontrada');
                CKEDITOR.instances[keys[0]].setData(html);
            } else {
                editor.setData(html);
            }
        }""",
        [instancia, html],
    )
    await page.wait_for_timeout(300)


async def _clicar_modal(page, texto_botao: str, timeout: int = 5000) -> bool:
    """
    Clica em botão dentro de modal/confirmDialog PrimeFaces.

    Estratégia:
    1. Polling com retry (intervalo 500ms) até atingir timeout
    2. Em cada tentativa: JS que procura SÓ em modais visíveis primeiro
       (evita pegar botão "Sim" de outra parte da página por engano)
    3. Fallback: qualquer botão visível com texto exato
    """
    # Pausa inicial: dialog PrimeFaces tem animação de fade-in
    await page.wait_for_timeout(1200)

    js_clicar = """
        () => {
            const texto = TEXTO_PLACEHOLDER;
            // Helper: elemento está visível?
            const visivel = (el) => {
                if (!el) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' &&
                       s.visibility !== 'hidden' &&
                       el.offsetParent !== null;
            };

            // 1. Procura em modais PrimeFaces visíveis (.ui-dialog / .ui-confirmdialog)
            const dialogs = Array.from(document.querySelectorAll(
                '.ui-dialog, .ui-confirmdialog, [role="dialog"]'
            )).filter(visivel);

            for (const dlg of dialogs) {
                const botoes = dlg.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], a.ui-button'
                );
                for (const el of botoes) {
                    if (!visivel(el)) continue;
                    const t = (el.textContent || el.value || '').trim();
                    if (t === texto || t.toLowerCase() === texto.toLowerCase()) {
                        el.click();
                        return {ok: true, fonte: 'modal'};
                    }
                }
            }

            // 2. Fallback: qualquer botão visível com texto exato
            const todos = Array.from(document.querySelectorAll(
                'button, input[type="submit"], input[type="button"]'
            ));
            for (const el of todos) {
                if (!visivel(el)) continue;
                const t = (el.textContent || el.value || '').trim();
                if (t === texto) {
                    el.click();
                    return {ok: true, fonte: 'fallback'};
                }
            }

            // 3. Debug: lista o que encontrou pra log
            return {
                ok: false,
                modais_visiveis: dialogs.length,
                botoes_visiveis: todos.filter(visivel).map(
                    el => (el.textContent || el.value || '').trim().substring(0, 30)
                ).filter(t => t.length).slice(0, 20)
            };
        }
    """
    # Injeta texto com encoding seguro pra JS (evita quebra de aspas)
    import json as _json
    js_clicar = js_clicar.replace("TEXTO_PLACEHOLDER", _json.dumps(texto_botao))

    deadline_ticks = max(4, timeout // 500)
    ultimo_debug = None
    for tentativa in range(deadline_ticks):
        try:
            r = await page.evaluate(js_clicar)
            if isinstance(r, dict) and r.get("ok"):
                await _pf_ajax(page)
                return True
            if isinstance(r, dict):
                ultimo_debug = r
        except Exception:
            pass
        await page.wait_for_timeout(500)

    # Se chegou aqui, falhou. Log do que viu na última tentativa.
    if ultimo_debug:
        try:
            from textwrap import shorten
            modais = ultimo_debug.get("modais_visiveis", 0)
            botoes = ultimo_debug.get("botoes_visiveis", [])
            print(
                f"[_clicar_modal] FALHA buscando '{texto_botao}'. "
                f"Modais visíveis: {modais}. "
                f"Botões visíveis: {botoes}",
                flush=True,
            )
        except Exception:
            pass

    return False
