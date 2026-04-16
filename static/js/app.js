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

/* HTMX global event handlers */
document.addEventListener('htmx:responseError', function(evt) {
    showToast('Erro na requisicao: ' + evt.detail.xhr.status, 'error');
});

/* Pipeline — streaming SSE via EventSource */
var _pipelineSource = null;

function runPipeline(url, title) {
    var modal = document.getElementById('pipeline-modal');
    var logEl = document.getElementById('pipeline-log');
    var statusEl = document.getElementById('pipeline-status');
    var titleEl = document.getElementById('pipeline-title');
    var closeBtn = document.getElementById('pipeline-close-btn');

    if (!modal || !logEl) return;

    // Reset
    logEl.textContent = '';
    titleEl.textContent = title || 'Pipeline';
    statusEl.textContent = 'rodando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    modal.showModal();

    // Fecha stream anterior se houver
    if (_pipelineSource) {
        _pipelineSource.close();
        _pipelineSource = null;
    }

    _pipelineSource = new EventSource(url);

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
        _pipelineSource.close();
        _pipelineSource = null;
        showToast('Pipeline concluido', 'success');
    });

    _pipelineSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_pipelineSource) {
            _pipelineSource.close();
            _pipelineSource = null;
        }
    };
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
