"""Rota para servir arquivos de PAJs (PDFs, TXTs, JSONs)."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse

from services.paj_service import ler_arquivo

router = APIRouter()


@router.get("/files/{paj_norm}/{path:path}")
async def serve_file(paj_norm: str, path: str):
    arquivo, content_type = ler_arquivo(paj_norm, path)
    if not arquivo:
        return PlainTextResponse("Arquivo nao encontrado", status_code=404)

    if "pdf" in content_type:
        return FileResponse(arquivo, media_type=content_type, filename=arquivo.name)

    # TXT, JSON, MD — retorna como texto
    if "text" in content_type or "json" in content_type:
        conteudo = _ler_texto_robusto(arquivo)
        # Força UTF-8 no header da resposta pra browser renderizar acentos certos
        media = content_type
        if "charset" not in media.lower():
            media += "; charset=utf-8"
        return PlainTextResponse(conteudo, media_type=media)

    return FileResponse(arquivo, media_type=content_type)


def _ler_texto_robusto(arquivo) -> str:
    """Lê texto tentando múltiplos encodings.

    PDFs convertidos via PyMuPDF/OCR podem ter sido salvos como UTF-8, CP1252
    (Windows latin) ou Latin-1. Tenta na ordem; usa replace só como último
    recurso pra não perder o arquivo inteiro com caracteres '?'.
    """
    raw = arquivo.read_bytes()
    # BOM UTF-8 explícito
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
