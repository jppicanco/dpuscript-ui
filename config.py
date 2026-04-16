"""Configuracao central do dpuscript-ui."""

from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

# Paths derivados do workspace
DPU_WORKSPACE = Path(os.getenv("DPU_WORKSPACE", r"E:\DPU\dpu-workspace"))
DPUSCRIPT_DIR = DPU_WORKSPACE / "dpuscript"
ENTRADA_DIR = DPU_WORKSPACE / "Entrada" / "dpuscript"
ESTADO_FILE = DPUSCRIPT_DIR / "estado" / "pajs_processados.json"

# Validacao basica
if not DPU_WORKSPACE.exists():
    raise RuntimeError(f"DPU_WORKSPACE nao encontrado: {DPU_WORKSPACE}")
