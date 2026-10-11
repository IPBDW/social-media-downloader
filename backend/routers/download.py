import json
import asyncio
import os
import re
import tempfile
import unicodedata
import yt_dlp
from urllib.parse import quote
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from backend.services.downloader import DownloadManager

router = APIRouter(prefix="/api")
limiter = Limiter(key_func=get_remote_address)
dm = DownloadManager(max_concurrent=2)


class ScanRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4"


def sanitize_filename(title):
    # Hanya strip karakter yang ilegal di filesystem + control chars.
    # JANGAN transliterate ke ASCII; UTF-8 filename aman di semua OS modern
    # dan Content-Disposition sudah dukung filename*=UTF-8''.
    name = unicodedata.normalize('NFC', title)
    # Hapus control chars
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    # Ganti karakter yang ilegal di Windows/macOS/Linux
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    # Hapus trailing dot/spasi (aneh di Windows)
    name = re.sub(r'[.\s]+$', '', name)
    # Limit panjang (Windows MAX_PATH)
    if len(name) > 200:
        name = name[:200].strip()
    return name if name else "download"


@router.post("/scan")
@limiter.limit("20/minute")
async def scan_url(request: Request, data: ScanRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL tidak boleh kosong")

    try:
        info_opts = dm._base_opts()
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Video")
            thumbnail = info.get("thumbnail", "")
            duration = info.get("duration", 0)
            mins, secs = divmod(int(duration), 60)
            hrs, mins = divmod(mins, 60)
            duration_str = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"

            return {
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration_str,
            }
    except Exception as e:
        msg = str(e)
        if msg.startswith("ERROR: "):
            msg = msg[7:]
        if "authentication" in msg.lower() or "login" in msg.lower():
            msg += "\n\nTips: Upload cookies.txt lewat menu Cookie Auth"
        raise HTTPException(400, msg)


@router.post("/download")
@limiter.limit("10/minute")
async def start_download(request: Request, data: DownloadRequest):
    url = data.url.strip()
    fmt = data.format

    if not url:
        raise HTTPException(400, "URL tidak boleh kosong")
    if fmt not in ("mp3", "mp4"):
        raise HTTPException(400, "Format harus mp3 atau mp4")

    download_id = dm.create_download(url, fmt)
    asyncio.create_task(dm.run_download(download_id))

    return {"download_id": download_id, "message": "Download dimulai"}


MAX_COOKIE_SIZE = 1 * 1024 * 1024  # 1 MB; cookies.txt biasanya <50 KB


@router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    if not file.filename or not file.filename.endswith(".txt"):
        raise HTTPException(400, "File harus berupa cookies.txt")

    # Validasi ukuran pakai Content-Length header kalau ada
    if file.size is not None and file.size > MAX_COOKIE_SIZE:
        raise HTTPException(413, f"File terlalu besar (max {MAX_COOKIE_SIZE // 1024} KB)")

    ext_dir = os.path.join(tempfile.gettempdir(), "social-downloader")
    os.makedirs(ext_dir, exist_ok=True)
    dest = os.path.join(ext_dir, "cookies.txt")

    # Baca dengan batas ukuran
    content = bytearray()
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_COOKIE_SIZE:
            raise HTTPException(413, f"File terlalu besar (max {MAX_COOKIE_SIZE // 1024} KB)")

    with open(dest, "wb") as f:
        f.write(bytes(content))

    dm.set_cookies(dest)
    return {"message": "Cookies berhasil disimpan"}


@router.delete("/cookies")
async def delete_cookies():
    if not dm.cookies_path:
        return {"message": "Tidak ada cookies untuk dihapus"}

    try:
        os.remove(dm.cookies_path)
    except OSError:
        pass

    dm.cookies_path = None
    return {"message": "Cookies berhasil dihapus"}


@router.get("/cookies/status")
async def cookies_status():
    return {
        "active": dm.cookies_path is not None and os.path.exists(dm.cookies_path)
    }


@router.get("/progress/{download_id}")
async def progress(download_id: str):
    state = dm.get_state(download_id)
    if not state:
        raise HTTPException(404, "Download tidak ditemukan")

    async def event_generator():
        while True:
            s = dm.get_state(download_id)
            if not s:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break

            data = {
                "status": s["status"],
                "progress": s.get("progress", 0),
                "speed": s.get("speed", ""),
                "eta": s.get("eta", ""),
                "message": s.get("message", ""),
                "title": s.get("title", ""),
                "duration": s.get("duration_str", ""),
                "format": s.get("format", ""),
                "thumbnail": s.get("thumbnail", ""),
            }

            yield f"data: {json.dumps(data)}\n\n"

            if s["status"] in ("complete", "error"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/file/{download_id}")
async def get_file(download_id: str):
    state = dm.get_state(download_id)
    if not state or state["status"] != "complete":
        raise HTTPException(404, "File belum siap atau tidak ditemukan")

    filepath = state.get("filepath")
    if not filepath or not os.path.exists(filepath):
        dm.pop_state(download_id)
        raise HTTPException(404, "File tidak ditemukan di server")

    if state.get("served"):
        raise HTTPException(410, "File sudah pernah diunduh dan telah dihapus")

    title = state.get("title", "download")
    ext = "mp3" if state.get("format") == "mp3" else "mp4"
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    dm.update_state(download_id, served=True)

    safe_title = sanitize_filename(title)
    filename = f"{safe_title}.{ext}"
    encoded_filename = quote(filename)

    def cleanup():
        dm.delete_file_and_state(download_id)

    return FileResponse(
        path=filepath,
        media_type=media_type,
        filename=filename,
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{encoded_filename}"
        },
        background=BackgroundTask(cleanup),
    )
