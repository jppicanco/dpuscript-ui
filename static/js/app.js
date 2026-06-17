/* dpuscript-ui — utilidades globais */

function showToast(msg, type) {
    type = type || 'info';
    var container = document.getElementById('toast-container');
    if (!container) return;

    var alertClass = {
        'success': 'alert-success',
        'error': 'alert-error',
        'warning': 'alert-warning',
        'info': 'alert-info'
    }[type] || 'alert-info';

    var el = document.createElement('div');
    el.className = 'alert ' + alertClass + ' text-sm py-2 px-4 shadow-lg';
    el.innerHTML = '<span>' + msg + '</span>';
    container.appendChild(el);

    setTimeout(function() {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(function() { el.remove(); }, 300);
    }, 3000);
}

/* Copia texto pra área de transferência.
 * navigator.clipboard só existe em contexto seguro (HTTPS ou localhost).
 * A UI roda em http://192.168.0.102:8001 (HTTP via IP da LAN), onde a API
 * fica indisponível — por isso o fallback com textarea + execCommand.
 * Retorna Promise<bool>. */
async function copiarTexto(txt) {
    txt = txt == null ? '' : String(txt);
    if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(txt); return true; }
        catch (e) { /* cai no fallback abaixo */ }
    }
    try {
        var ta = document.createElement('textarea');
        ta.value = txt;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, txt.length);
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch (e) {
        return false;
    }
}

/* Reautenticação do Claude no M4 (modal global em base.html) */
function reautenticarM4() {
    var s1 = document.getElementById('reauth-step1');
    var s2 = document.getElementById('reauth-step2');
    if (!s1) return;
    s1.classList.remove('hidden'); s2.classList.add('hidden');
    document.getElementById('reauth-msg1').textContent = '';
    document.getElementById('reauth-msg2').textContent = '';
    document.getElementById('reauth-codigo').value = '';
    document.getElementById('reauth-modal').showModal();
}
async function reauthGerarLink() {
    var b = document.getElementById('reauth-btn-link');
    var msg = document.getElementById('reauth-msg1');
    b.disabled = true; msg.textContent = 'Gerando link (até ~30s)…';
    try {
        var r = await fetch('/api/auth/m4/iniciar', {method: 'POST'});
        var d = await r.json();
        if (d.ok && d.url) {
            document.getElementById('reauth-link').href = d.url;
            document.getElementById('reauth-url').textContent = d.url;
            document.getElementById('reauth-step1').classList.add('hidden');
            document.getElementById('reauth-step2').classList.remove('hidden');
        } else {
            msg.textContent = 'Erro: ' + (d.erro || 'falhou');
        }
    } catch (e) { msg.textContent = 'Erro: ' + e; }
    finally { b.disabled = false; }
}
async function reauthEnviarCodigo() {
    var cod = document.getElementById('reauth-codigo').value;
    var b = document.getElementById('reauth-btn-cod');
    var msg = document.getElementById('reauth-msg2');
    b.disabled = true; msg.textContent = 'Enviando…';
    try {
        var r = await fetch('/api/auth/m4/codigo', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({codigo: cod})
        });
        var d = await r.json();
        msg.textContent = d.ok ? ('✓ ' + (d.msg || 'ok')) : ('Erro: ' + (d.erro || 'falhou'));
        msg.className = 'text-xs mt-2 ' + (d.ok ? 'text-success' : 'text-error');
        if (d.ok) showToast('Claude reautenticado no M4', 'success');
    } catch (e) { msg.textContent = 'Erro: ' + e; }
    finally { b.disabled = false; }
}

/* HTMX global event handlers */
document.addEventListener('htmx:responseError', function(evt) {
    showToast('Erro na requisicao: ' + evt.detail.xhr.status, 'error');
});

/* Pipeline — streaming SSE via EventSource */
var _pipelineSource = null;
var _pipelineToken = null;

function _genToken() {
    return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

function runPipeline(url, title) {
    var modal = document.getElementById('pipeline-modal');
    var logEl = document.getElementById('pipeline-log');
    var statusEl = document.getElementById('pipeline-status');
    var titleEl = document.getElementById('pipeline-title');
    var closeBtn = document.getElementById('pipeline-close-btn');
    var cancelBtn = document.getElementById('pipeline-cancel-btn');

    if (!modal || !logEl) return;

    // Reset
    logEl.textContent = '';
    titleEl.textContent = title || 'Pipeline';
    statusEl.textContent = 'rodando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    if (cancelBtn) cancelBtn.style.display = '';
    modal.showModal();

    // Fecha stream anterior se houver
    if (_pipelineSource) {
        _pipelineSource.close();
        _pipelineSource = null;
    }

    _pipelineToken = _genToken();
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    _pipelineSource = new EventSource(url + sep + 'token=' + _pipelineToken);

    _pipelineSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _pipelineSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluido';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        _pipelineSource.close();
        _pipelineSource = null;
        _pipelineToken = null;
        showToast('Pipeline concluido', 'success');
    });

    _pipelineSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (cancelBtn) cancelBtn.style.display = 'none';
        if (_pipelineSource) {
            _pipelineSource.close();
            _pipelineSource = null;
        }
        _pipelineToken = null;
    };
}

function cancelarPipeline() {
    var statusEl = document.getElementById('pipeline-status');
    var cancelBtn = document.getElementById('pipeline-cancel-btn');
    var closeBtn = document.getElementById('pipeline-close-btn');
    if (!_pipelineToken) return;
    var token = _pipelineToken;
    // Fecha stream cliente imediato
    if (_pipelineSource) {
        _pipelineSource.close();
        _pipelineSource = null;
    }
    if (statusEl) {
        statusEl.textContent = 'cancelando...';
        statusEl.className = 'badge badge-sm badge-warning';
    }
    // Pede backend pra matar subprocess
    fetch('/api/sync/cancel/' + encodeURIComponent(token), {method: 'POST'})
        .then(function() {
            if (statusEl) {
                statusEl.textContent = 'cancelado';
                statusEl.className = 'badge badge-sm badge-ghost';
            }
            if (cancelBtn) cancelBtn.style.display = 'none';
            if (closeBtn) closeBtn.disabled = false;
            showToast('Pipeline cancelado', 'warning');
        })
        .catch(function(e) {
            showToast('Erro ao cancelar: ' + e.message, 'error');
        });
    _pipelineToken = null;
}

/* Chat livre — brainstorm vinculado ao PAJ */

async function abrirChatLivre(pajNorm) {
    try {
        // Lista conversas existentes
        const r = await fetch('/api/chat-livre/paj/' + encodeURIComponent(pajNorm) + '/conversas');
        const data = await r.json();
        if (data.conversas && data.conversas.length > 0) {
            // Reabre a mais recente
            const ultima = data.conversas.sort((a, b) => b.atualizado_em.localeCompare(a.atualizado_em))[0];
            window.location.href = '/chat-livre?conv=' + ultima.id;
            return;
        }
        // Cria nova
        const r2 = await fetch('/api/chat-livre/paj/' + encodeURIComponent(pajNorm) + '/conversas', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({titulo: 'Discussão ' + new Date().toLocaleDateString('pt-BR')}),
        });
        const conv = await r2.json();
        window.location.href = '/chat-livre?conv=' + conv.id;
    } catch (e) {
        showToast('Erro ao abrir chat: ' + e.message, 'error');
    }
}

/* Docgen — gerar DOCX/PDF a partir de .txt do PAJ */

var _docgenPaj = null;
var _docgenSource = null;

async function abrirDocgenModal(pajNorm) {
    _docgenPaj = pajNorm;
    var modal = document.getElementById('docgen-modal');
    if (!modal) return;

    // Reset UI
    document.getElementById('docgen-loading').style.display = '';
    document.getElementById('docgen-form').style.display = 'none';
    document.getElementById('docgen-log-box').style.display = 'none';
    document.getElementById('docgen-log').textContent = '';
    document.getElementById('docgen-arquivo').innerHTML = '<option value="">-- selecione --</option>';
    document.getElementById('docgen-tipo').value = '';
    document.getElementById('docgen-tribunal').value = '';
    document.getElementById('docgen-btn-gerar').disabled = false;

    modal.showModal();

    try {
        var resp = await fetch('/api/docgen/' + encodeURIComponent(pajNorm) + '/txts');
        var data = await resp.json();
        var sel = document.getElementById('docgen-arquivo');
        var noTxts = document.getElementById('docgen-no-txts');

        if (!data.txts || data.txts.length === 0) {
            noTxts.style.display = '';
        } else {
            noTxts.style.display = 'none';
            data.txts.forEach(function(t) {
                var opt = document.createElement('option');
                opt.value = t.nome;
                opt.textContent = t.nome + ' (' + (t.tamanho / 1024).toFixed(1) + ' KB)';
                sel.appendChild(opt);
            });
        }

        document.getElementById('docgen-loading').style.display = 'none';
        document.getElementById('docgen-form').style.display = '';
    } catch (e) {
        showToast('Erro ao listar arquivos: ' + e.message, 'error');
    }
}

function executarDocgen() {
    if (!_docgenPaj) return;
    var arquivo = document.getElementById('docgen-arquivo').value;
    var tipo = document.getElementById('docgen-tipo').value;
    var tribunal = document.getElementById('docgen-tribunal').value;

    if (!arquivo) { showToast('Selecione um arquivo .txt', 'warning'); return; }
    if (!tipo) { showToast('Selecione tipo de peça', 'warning'); return; }

    document.getElementById('docgen-btn-gerar').disabled = true;
    var logBox = document.getElementById('docgen-log-box');
    var logEl = document.getElementById('docgen-log');
    var statusEl = document.getElementById('docgen-status');
    logBox.style.display = '';
    logEl.textContent = '';
    statusEl.textContent = 'rodando';
    statusEl.className = 'badge badge-sm badge-warning';

    var token = _genToken();
    var qs = '?arquivo=' + encodeURIComponent(arquivo) +
             '&tipo_peca=' + encodeURIComponent(tipo) +
             (tribunal ? '&tribunal=' + encodeURIComponent(tribunal) : '') +
             '&token=' + token;

    if (_docgenSource) { _docgenSource.close(); _docgenSource = null; }
    _docgenSource = new EventSource('/api/docgen/' + encodeURIComponent(_docgenPaj) + '/gerar' + qs);

    _docgenSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _docgenSource.addEventListener('done', function() {
        statusEl.textContent = 'concluido';
        statusEl.className = 'badge badge-sm badge-success';
        document.getElementById('docgen-btn-gerar').disabled = false;
        _docgenSource.close();
        _docgenSource = null;
        showToast('DOCX gerado — veja na pasta do PAJ', 'success');
    });

    _docgenSource.onerror = function() {
        statusEl.textContent = 'erro';
        statusEl.className = 'badge badge-sm badge-error';
        document.getElementById('docgen-btn-gerar').disabled = false;
        if (_docgenSource) { _docgenSource.close(); _docgenSource = null; }
    };
}

/* Limpeza de anexos (modal — preview + execute) */

var _limpezaPajAtual = null;
var _limpezaPreview = null;

function _fmtBytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

async function abrirLimpezaModal(pajNorm) {
    _limpezaPajAtual = pajNorm;
    var modal = document.getElementById('limpeza-modal');
    if (!modal) return;

    // Reset UI
    document.getElementById('limpeza-loading').style.display = '';
    document.getElementById('limpeza-conteudo').style.display = 'none';
    document.getElementById('limpeza-resultado').style.display = 'none';
    document.getElementById('limpeza-erro').style.display = 'none';
    document.getElementById('limpeza-confirmar').checked = false;
    var forcarEl = document.getElementById('limpeza-forcar');
    if (forcarEl) forcarEl.checked = false;
    document.getElementById('limpeza-btn-executar').disabled = true;

    modal.showModal();

    try {
        var resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/limpar-anexos/preview');
        var data = await resp.json();
        _limpezaPreview = data;

        if (!data.ok) {
            _showLimpezaErro(data.erro || 'Falha no preview');
            return;
        }

        // Stats
        document.getElementById('limpeza-n-remover').textContent = data.arquivos_a_remover.length;
        document.getElementById('limpeza-bytes-liberar').textContent = _fmtBytes(data.bytes_total_disponivel);
        document.getElementById('limpeza-n-preservar').textContent = data.arquivos_preservados.length;

        // Listas
        var tbodyRem = document.getElementById('limpeza-lista-remover');
        tbodyRem.innerHTML = '';
        data.arquivos_a_remover.forEach(function(a) {
            var tr = document.createElement('tr');
            tr.innerHTML = '<td class="font-mono">' + a.nome + '</td>' +
                           '<td class="text-right">' + (a.tamanho / 1024).toFixed(1) + '</td>' +
                           '<td>' + (a.tem_ocr ? '<span class="text-success">sim</span>' : '<span class="text-error">não</span>') + '</td>';
            tbodyRem.appendChild(tr);
        });

        var tbodyPres = document.getElementById('limpeza-lista-preservar');
        tbodyPres.innerHTML = '';
        data.arquivos_preservados.forEach(function(a) {
            var tr = document.createElement('tr');
            tr.innerHTML = '<td class="font-mono">' + a.nome + '</td>' +
                           '<td class="text-right">' + (a.tamanho / 1024).toFixed(1) + '</td>';
            tbodyPres.appendChild(tr);
        });

        // Bloqueios
        var bloqEl = document.getElementById('limpeza-bloqueios');
        if (data.motivos_bloqueio && data.motivos_bloqueio.length) {
            bloqEl.style.display = '';
            var motUl = document.getElementById('limpeza-motivos');
            motUl.innerHTML = '';
            data.motivos_bloqueio.forEach(function(m) {
                var li = document.createElement('li');
                li.textContent = m;
                motUl.appendChild(li);
            });
        } else {
            bloqEl.style.display = 'none';
        }

        document.getElementById('limpeza-loading').style.display = 'none';
        document.getElementById('limpeza-conteudo').style.display = '';

        // Habilita botao quando confirmar marcado + (sem bloqueio OU forcar marcado)
        _ligarHandlersConfirmacao();
    } catch (e) {
        _showLimpezaErro('Erro: ' + e.message);
    }
}

function _ligarHandlersConfirmacao() {
    var confEl = document.getElementById('limpeza-confirmar');
    var forcarEl = document.getElementById('limpeza-forcar');
    var btn = document.getElementById('limpeza-btn-executar');

    function refresh() {
        if (!_limpezaPreview) { btn.disabled = true; return; }
        var bloqueado = _limpezaPreview.motivos_bloqueio && _limpezaPreview.motivos_bloqueio.length > 0;
        var podeExecutar = confEl.checked && (!bloqueado || (forcarEl && forcarEl.checked));
        btn.disabled = !podeExecutar || _limpezaPreview.arquivos_a_remover.length === 0;
    }
    confEl.onchange = refresh;
    if (forcarEl) forcarEl.onchange = refresh;
}

async function executarLimpeza() {
    if (!_limpezaPajAtual) return;
    var btn = document.getElementById('limpeza-btn-executar');
    var forcarEl = document.getElementById('limpeza-forcar');
    var forcar = forcarEl && forcarEl.checked;
    btn.disabled = true;
    btn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Apagando...';

    try {
        var url = '/api/paj/' + encodeURIComponent(_limpezaPajAtual) + '/limpar-anexos/executar?forcar=' + (forcar ? 'true' : 'false');
        var resp = await fetch(url, {method: 'POST'});
        var data = await resp.json();
        if (!data.ok) {
            _showLimpezaErro(data.erro || 'Falha na execucao');
            return;
        }
        var msg = 'Removidos ' + data.removidos + ' arquivo(s), ' + _fmtBytes(data.bytes_liberados) + ' liberados.';
        var resEl = document.getElementById('limpeza-resultado');
        document.getElementById('limpeza-resultado-msg').textContent = msg;
        resEl.style.display = '';
        showToast(msg, 'success');
        btn.innerHTML = 'Concluído';
    } catch (e) {
        _showLimpezaErro('Erro: ' + e.message);
    }
}

function _showLimpezaErro(msg) {
    document.getElementById('limpeza-loading').style.display = 'none';
    document.getElementById('limpeza-conteudo').style.display = '';
    var erEl = document.getElementById('limpeza-erro');
    document.getElementById('limpeza-erro-msg').textContent = msg;
    erEl.style.display = '';
    showToast(msg, 'error');
}

function fecharLimpezaModal() {
    var modal = document.getElementById('limpeza-modal');
    if (modal) modal.close();
    _limpezaPajAtual = null;
    _limpezaPreview = null;
}

/* Elaborar Peca (fluxo novo — background + polling + modal global) */

// Guarda o PAJ atual do modal pra funcoes auxiliares
var _resumoPajAtual = null;

async function abrirResumo(pajNorm) {
    _resumoPajAtual = pajNorm;
    const modal = document.getElementById('resumo-modal');
    const content = document.getElementById('resumo-content');
    const label = document.getElementById('resumo-paj-label');
    const linkPaj = document.getElementById('resumo-abrir-paj');
    if (!modal || !content) return;

    // Label e link pro detalhe
    if (label) label.textContent = pajNorm;
    if (linkPaj) linkPaj.href = '/paj/' + pajNorm;

    // Busca o resumo mais recente
    try {
        const resp = await fetch('/api/elaborar/status/' + pajNorm);
        const data = await resp.json();
        content.textContent = data.summary || '(resumo vazio — elaboracao ainda nao concluida?)';
    } catch (e) {
        content.textContent = 'Erro ao carregar resumo: ' + e.message;
    }

    modal.showModal();
}

async function enviarCorrecaoAtual() {
    if (!_resumoPajAtual) {
        showToast('Nenhum PAJ ativo', 'warning');
        return;
    }
    await enviarCorrecao(_resumoPajAtual);
}



function elaborarApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',        // idle | running | done | error
        lastAction: '',
        summary: '',
        error: '',
        _pollTimer: null,

        async init() {
            // Ao carregar a pagina, checa se ja existe sessao rodando ou pronta
            await this.fetchStatus();
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/elaborar/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.lastAction = data.last_action || '';
                this.summary = data.summary || '';
                this.error = data.error || '';
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'error';
            }
        },

        async iniciar() {
            this.status = 'running';
            this.lastAction = 'iniciando...';
            this.error = '';
            try {
                await fetch('/api/elaborar/start/' + this.pajNorm, {method: 'POST'});
                showToast('Claude iniciou a elaboracao — pode navegar livremente', 'info');
                this.startPolling();
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao iniciar: ' + e.message;
            }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error' || this.status === 'idle') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast('Peca elaborada — clique em Ver Resumo', 'success');
                    }
                }
            }, 2000);
        },

        verResumo() {
            abrirResumo(this.pajNorm);
        }
    };
}

async function enviarCorrecao(pajNorm) {
    const textarea = document.getElementById('correcao-text');
    const text = (textarea?.value || '').trim();
    if (!text) {
        showToast('Digite a correcao primeiro', 'warning');
        return;
    }
    try {
        const resp = await fetch('/api/elaborar/correcao/' + pajNorm, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        if (!resp.ok) {
            const err = await resp.json();
            showToast('Erro: ' + (err.erro || resp.status), 'error');
            return;
        }
        showToast('Correcao enviada — Claude refazendo', 'info');
        textarea.value = '';
        document.getElementById('resumo-modal')?.close();
        // Dashboard e detalhe atualizam via polling — nao precisa reload
    } catch (e) {
        showToast('Erro: ' + e.message, 'error');
    }
}

/* Acompanhar transito em julgado — na pagina do PAJ */
function acompanharTransito(pajNorm, paj, cnj) {
    return {
        pajNorm: pajNorm,
        paj: paj,
        cnj: cnj,
        item: null,
        frequencia: 15,

        async init() {
            await this.fetch();
            setInterval(() => this.fetch(), 15000);
        },

        async fetch() {
            try {
                const r = await fetch('/api/watchlist');
                const data = await r.json();
                const found = (data.itens || []).find(x => x.paj === this.paj);
                this.item = found || null;
            } catch (e) { /* silencia */ }
        },

        abrirDialog() {
            document.getElementById('transito-dialog').showModal();
        },

        async adicionar() {
            try {
                const r = await fetch('/api/watchlist/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        paj: this.paj,
                        cnj: this.cnj,
                        frequencia_dias: parseInt(this.frequencia, 10),
                    }),
                });
                if (!r.ok) throw new Error(r.status);
                showToast('Adicionado ao monitoramento', 'success');
                document.getElementById('transito-dialog').close();
                await this.fetch();
            } catch (e) {
                showToast('Erro: ' + e.message, 'error');
            }
        },

        async remover() {
            if (!confirm('Parar de acompanhar o transito deste PAJ?')) return;
            try {
                await fetch('/api/watchlist/remove/' + encodeURIComponent(this.paj), {method: 'POST'});
                await this.fetch();
                showToast('Removido do monitoramento', 'info');
            } catch (e) {
                showToast('Erro: ' + e.message, 'error');
            }
        }
    };
}

/* Elaborar Peca LEGADO — streaming SSE do Claude Code CLI (fluxo antigo) */
var _claudeSource = null;

function elaborarPeca(pajNorm) {
    var modal = document.getElementById('pipeline-modal');
    var logEl = document.getElementById('pipeline-log');
    var statusEl = document.getElementById('pipeline-status');
    var titleEl = document.getElementById('pipeline-title');
    var closeBtn = document.getElementById('pipeline-close-btn');

    if (!modal || !logEl) return;

    // Reset
    logEl.textContent = '';
    titleEl.textContent = 'Elaborar Peca — ' + pajNorm;
    statusEl.textContent = 'Claude trabalhando...';
    statusEl.className = 'badge badge-sm badge-info animate-pulse';
    closeBtn.disabled = true;
    modal.showModal();

    if (_claudeSource) {
        _claudeSource.close();
        _claudeSource = null;
    }

    _claudeSource = new EventSource('/api/elaborar/' + pajNorm);

    _claudeSource.addEventListener('chunk', function(e) {
        logEl.textContent += e.data;
        logEl.scrollTop = logEl.scrollHeight;
    });

    _claudeSource.addEventListener('done', function(e) {
        statusEl.textContent = 'concluido';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _claudeSource.close();
        _claudeSource = null;
        showToast('Peca elaborada', 'success');
    });

    _claudeSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_claudeSource) {
            _claudeSource.close();
            _claudeSource = null;
        }
    };
}
