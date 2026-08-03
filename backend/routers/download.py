import json
import asyncio
import os
import re
import tempfile
import yt_dlp
from urllib.parse import quote
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from services.downloader import DownloadManager

router = APIRouter(prefix="/api")
limiter = Limiter(key_func=get_remote_address)
dm = DownloadManager(max_concurrent=2)


class ScanRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4"


@router.post("/scan")
@limiter.limit("20/minute")
async def scan_url(request: Request, data: ScanRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(400, "URL tidak boleh kosong")

    try:
        info_opts = {"quiet": True, "no_warnings": True}
        if dm.cookies_path and os.path.exists(dm.cookies_path):
            info_opts["cookiefile"] = dm.cookies_path
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


@router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    if not file.filename.endswith(".txt"):
        raise HTTPException(400, "File harus berupa cookies.txt")

    ext_dir = os.path.join(tempfile.gettempdir(), "social-downloader")
    os.makedirs(ext_dir, exist_ok=True)
    dest = os.path.join(ext_dir, "cookies.txt")

    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    dm.set_cookies(dest)
    return {"message": "Cookies berhasil disimpan"}


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

def sanitize_filename(title):
    name = re.sub(r'[^\x00-\xFF]', '_', title)
    name = re.sub(r'_+', '_', name).strip('_')
    return name if name else "download"


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
    encoded_filename = quote(safe_title) + f".{ext}"

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
