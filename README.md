# dpuscript-ui

Interface web para o pipeline **dpuscript** — sistema de monitoramento e elaboração de peças para Defensores Públicos Federais atuantes na TNU e no STJ.

> Desenvolvido para uso interno na DPU. Projeto pessoal, sem vínculo institucional oficial.

---

## O que é

O **dpuscript** é um pipeline que:
1. Autentica no e-Proc TNU e baixa automaticamente as peças dos processos da caixa do Defensor
2. Classifica cada processo (decisão monocrática, acórdão, vista ao MP, vitória, etc.)
3. Extrai texto dos PDFs — com OCR automático para documentos escaneados
4. Gera um prompt otimizado para elaboração de peças via Claude AI

O **dpuscript-ui** é o painel web que dá visibilidade a esse pipeline: mostra todos os PAJs (Processos de Assistência Jurídica), seu estado de processamento, peças baixadas, classificação automática e facilita o acesso a cada processo para elaboração das peças.

---

## Pré-requisitos

- **Python 3.11+**
- **[dpu-workspace](https://github.com/jppicanco/dpu-workspace)** — repositório companion com o pipeline e as skills do Claude
- **Claude Code** com modelo Max (claude-opus ou claude-sonnet) — a elaboração das peças é feita pelo Claude via CLI, não por API paga
- Playwright (instalado automaticamente pelo pipeline)
- Tesseract OCR (para PDFs escaneados) — opcional, mas recomendado

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/jppicanco/dpuscript-ui.git
cd dpuscript-ui

# 2. Crie o ambiente virtual
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/Mac

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Configure o .env
copy .env.example .env
# Edite .env e aponte DPU_WORKSPACE para sua pasta do dpu-workspace
```

---

## Configuração

Edite o arquivo `.env`:

```env
DPU_WORKSPACE=C:\DPU\dpu-workspace
```

O `DPU_WORKSPACE` deve apontar para a pasta onde você clonou o [dpu-workspace](https://github.com/jppicanco/dpu-workspace) — é onde ficam os arquivos dos processos, o pipeline e as skills do Claude.

---

## Uso

```bash
# Iniciar o servidor (porta 8001)
.venv\Scripts\python.exe app.py

# Acessar no navegador
# http://localhost:8001
```

O painel mostra todos os PAJs detectados na pasta `Entrada/dpuscript/` do workspace. Para cada PAJ você pode:
- Ver as peças baixadas do e-Proc, classificadas por tipo e data
- Ver decisões TNU/STJ (separadas das peças do processo de origem)
- Acessar PDFs e TXTs extraídos
- Acionar a elaboração de peças pelo Claude
- Ver despachos SISDPU gerados

---

## Estrutura

```
dpuscript-ui/
├── app.py              # FastAPI + Uvicorn
├── config.py           # Configurações (lê .env)
├── routes/             # Endpoints (dashboard, PAJs, pipeline, chat, arquivos)
├── services/           # Lógica de negócio (lê dados do workspace)
├── templates/          # HTML (Jinja2 + DaisyUI + Alpine.js)
├── static/             # CSS e JS estáticos
└── requirements.txt
```

---

## Repositório companion

Este frontend depende do **[dpu-workspace](https://github.com/jppicanco/dpu-workspace)**, que contém:
- O pipeline Python (`dpuscript/preparar_pajs.py`) que baixa e classifica os processos
- As skills do Claude para elaboração de peças (triagem, arquivamento, embargos, agravo interno, etc.)
- O `CLAUDE.md` com as instruções do sistema para o Claude Code
- Os regimentos internos da TNU e do STJ em texto

---

## Tecnologias

- **Backend:** FastAPI + Uvicorn
- **Frontend:** DaisyUI (Tailwind CSS) + Alpine.js + HTMX
- **Pipeline:** Playwright (automação e-Proc), PyMuPDF, Tesseract OCR
- **IA:** Claude Code (claude-opus/sonnet) via CLI — sem API paga

---

## Aviso

Este projeto foi desenvolvido para uso pessoal por um Defensor Público Federal. Ele **não armazena dados de assistidos** — os arquivos de processos ficam apenas na máquina local do Defensor e são ignorados pelo Git. Consulte o `.gitignore` para confirmar o que é e não é versionado.

Contribuições são bem-vindas. Abra uma issue ou PR.
