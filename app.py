"""dpuscript-ui — Interface web para o pipeline dpuscript da DPU."""

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import jinja2

from routes.dashboard import router as dashboard_router
from routes.paj import router as paj_router
from routes.files import router as files_router
from routes.pipeline import router as pipeline_router
from routes.chat import router as chat_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="dpuscript-ui", version="0.1.0")

# Jinja2 direto (contorna bug Starlette + Jinja2 3.1.6 + Python 3.13)
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)

# Static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Routes
app.include_router(dashboard_router)
app.include_router(paj_router)
app.include_router(files_router)
app.include_router(pipeline_router)
app.include_router(chat_router)


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8001, reload=True)
