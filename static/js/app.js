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

/* Elaborar Peca — streaming SSE do Claude Code CLI */
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
