import os
import re
import sys
import threading
import asyncio
import tempfile
import time
import logging
import uuid
import yt_dlp

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("downloader")


# Limit ukuran file download (default 1 GB; override via env)
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "1024"))


class DownloadManager:
    def __init__(self, max_concurrent=2):
        self.states = {}
        self.lock = threading.Lock()
        self.semaphore = threading.Semaphore(max_concurrent)
        self.downloads_dir = os.path.join(tempfile.gettempdir(), "social-downloader")
        self.cookies_path = None
        os.makedirs(self.downloads_dir, exist_ok=True)
        log.info("DownloadManager initialized, temp dir: %s", self.downloads_dir)

    def set_cookies(self, path):
        self.cookies_path = path
        log.info("Cookies set: %s", path)

    def create_download(self, url, fmt):
        # UUID lebih aman dari collision dibanding timestamp
        download_id = uuid.uuid4().hex[:16]
        with self.lock:
            self.states[download_id] = {
                "url": url,
                "format": fmt,
                "status": "queued",
                "progress": 0,
                "speed": "",
                "eta": "",
                "message": "Menunggu antrian...",
                "title": "",
                "duration": 0,
                "duration_str": "",
                "thumbnail": "",
                "filepath": None,
                "created_at": time.time(),
                "served": False,
            }
        log.info("Download created: id=%s url=%s fmt=%s", download_id, url, fmt)
        return download_id

    def get_state(self, download_id):
        with self.lock:
            return self.states.get(download_id)

    def update_state(self, download_id, **kwargs):
        with self.lock:
            if download_id in self.states:
                self.states[download_id].update(kwargs)

    def delete_file(self, download_id):
        state = self.get_state(download_id)
        if state and state.get("filepath"):
            try:
                os.remove(state["filepath"])
                log.info("File deleted: %s", state["filepath"])
            except OSError as e:
                log.warning("Failed to delete file: %s", e)

    def pop_state(self, download_id):
        with self.lock:
            return self.states.pop(download_id, None)

    def delete_file_and_state(self, download_id):
        self.delete_file(download_id)
        self.pop_state(download_id)

    def purge_old_states(self, max_age=60):
        """Bersihkan state yang:
        - sudah lebih dari `max_age` detik sejak dibuat dan tidak aktif, ATAU
        - status complete + sudah pernah didownload user (served), ATAU
        - status complete tapi lebih dari 5 menit tanpa di-serve (file nyangkut).
        """
        now = time.time()
        active_statuses = ("queued", "extracting", "downloading", "processing")
        stuck_complete_age = 300  # 5 menit

        with self.lock:
            stale = [
                did for did, s in self.states.items()
                if s.get("created_at", 0) < now - max_age
                and s.get("status") not in active_statuses
            ]
            served = [
                did for did, s in self.states.items()
                if s.get("served") and s.get("status") == "complete"
            ]
            stuck_complete = [
                did for did, s in self.states.items()
                if s.get("status") == "complete"
                and not s.get("served")
                and s.get("created_at", 0) < now - stuck_complete_age
            ]
            all_stale = list(set(stale + served + stuck_complete))

            has_active = any(
                s.get("status") in active_statuses
                for s in self.states.values()
            )

        if all_stale:
            log.info("Purging %d states (stale: %d, served: %d, stuck: %d)",
                     len(all_stale), len(stale), len(served), len(stuck_complete))
        for did in all_stale:
            self.delete_file_and_state(did)

        # Hanya bersihkan orphan kalau TIDAK ada download aktif
        if not has_active:
            self._cleanup_dir()
        else:
            log.debug("Skipping dir cleanup (active downloads in progress)")

    def _cleanup_dir(self):
        """Hapus file orphan yang bukan milik state aktif.

        Penting: JANGAN hapus partial files (.part, .ytdl, .tmp) dari
        download yang sedang berjalan (status extracting/downloading/processing).
        """
        try:
            if not os.path.exists(self.downloads_dir):
                return

            # Kumpulkan semua download_id yang masih punya state (aktif ATAU complete)
            with self.lock:
                active_ids = {
                    did for did, s in self.states.items()
                    if s.get("status") in (
                        "queued", "extracting", "downloading", "processing",
                    )
                }
                all_ids = set(self.states.keys())

            # File yang harus di-protect dari cleanup
            protected = set()
            if self.cookies_path:
                protected.add(os.path.basename(self.cookies_path))

            for f in os.listdir(self.downloads_dir):
                filepath = os.path.join(self.downloads_dir, f)

                if not os.path.isfile(filepath):
                    continue

                # Never delete cookies file
                if f in protected:
                    continue

                # Skip kalau filename diawali download_id yang aktif
                belongs_to_active = any(
                    f == did or f.startswith(f"{did}.")
                    for did in active_ids
                )
                if belongs_to_active:
                    log.debug("Skipping active file: %s", filepath)
                    continue

                # Skip partial files kalau ada active download apapun
                is_partial = f.endswith((".part", ".ytdl", ".tmp"))
                if is_partial and active_ids:
                    log.debug("Skipping orphan partial (active download exists): %s",
                              filepath)
                    continue

                # Skip files milik state yang belum stale (complete tapi belum served)
                belongs_to_any = any(
                    f == did or f.startswith(f"{did}.")
                    for did in all_ids
                )
                if belongs_to_any:
                    log.debug("Skipping file with live state: %s", filepath)
                    continue

                try:
                    os.remove(filepath)
                    log.debug("Cleaned up orphan file: %s", filepath)
                except OSError:
                    pass
        except Exception as e:
            log.warning("Dir cleanup error: %s", e)

    def _base_opts(self, progress_hook=None, quiet=True):
        opts = {
            "quiet": quiet,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]
        if self.cookies_path and os.path.exists(self.cookies_path):
            opts["cookiefile"] = self.cookies_path
            log.debug("Using cookies: %s", self.cookies_path)
        return opts

    async def run_download(self, download_id):
        log.debug("run_download called: id=%s", download_id)
        await asyncio.to_thread(self._download_worker, download_id)

    def _download_worker(self, download_id):
        log.info("Worker started: id=%s", download_id)
        self.semaphore.acquire()
        log.debug("Semaphore acquired: id=%s", download_id)
        start_time = time.time()
        try:
            state = self.get_state(download_id)
            if not state:
                log.warning("State not found for id=%s", download_id)
                return

            url = state["url"]
            fmt = state["format"]

            log.info("Processing: id=%s url=%s fmt=%s", download_id, url, fmt)

            self.update_state(
                download_id,
                status="extracting",
                message="Mendapatkan informasi video...",
                progress=0,
            )

            progress_tracker = {
                "max_pct": 0,
                "max_total": 0,
                "last_bytes": 0,
            }

            def progress_hook(d):
                status = d.get("status", "")
                log.debug("Progress hook: id=%s status=%s progress=%s",
                          download_id, status, d.get("_percent_str", "?"))

                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    frag_idx = d.get("fragment_index", 0)

                    if total > progress_tracker["max_total"]:
                        progress_tracker["max_total"] = total

                    if total and downloaded <= total:
                        percent = (downloaded / total * 100)
                    elif frag_idx:
                        percent = min(frag_idx * 10, 99)
                    else:
                        percent = progress_tracker["max_pct"]

                    if percent > progress_tracker["max_pct"]:
                        progress_tracker["max_pct"] = percent

                    percent = progress_tracker["max_pct"]

                    def strip_ansi(s):
                        return re.sub(r'\x1b\[[0-9;]*m', '', s).strip()

                    speed_raw = d.get("_speed_str", "")
                    speed = strip_ansi(speed_raw) if isinstance(speed_raw, str) else ""
                    if not speed:
                        speed_val = d.get("speed", 0)
                        if isinstance(speed_val, (int, float)) and speed_val:
                            speed = f"{speed_val / 1024 / 1024:.1f} MB/s"

                    eta_raw = d.get("_eta_str", "")
                    eta = strip_ansi(eta_raw) if isinstance(eta_raw, str) else ""
                    if not eta:
                        eta_val = d.get("eta")
                        if isinstance(eta_val, (int, float)) and eta_val:
                            eta = f"{int(eta_val)}s"

                    self.update_state(
                        download_id,
                        status="downloading",
                        progress=round(percent, 1),
                        speed=str(speed) if speed else "",
                        eta=str(eta) if eta else "",
                        message=f"Mendownload... {round(percent, 1)}%",
                    )
                elif status == "finished":
                    log.info("Download finished: id=%s", download_id)
                    self.update_state(
                        download_id,
                        status="processing",
                        progress=99,
                        message="Memproses file...",
                    )

            log.info("Extracting video info... id=%s", download_id)
            try:
                # Pakai _base_opts yang sama dengan download (User-Agent dll)
                info_opts = self._base_opts()
                with yt_dlp.YoutubeDL(info_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    title = info.get("title", "video")
                    thumbnail = info.get("thumbnail", "")
                    duration = info.get("duration", 0)

                    # Cek estimasi ukuran file — reject kalau terlalu besar
                    filesize = (
                        info.get("filesize")
                        or info.get("filesize_approx")
                        or 0
                    )
                    if filesize and filesize > MAX_FILE_SIZE_MB * 1024 * 1024:
                        msg = (f"File terlalu besar ({filesize / 1024 / 1024:.1f} MB). "
                               f"Max {MAX_FILE_SIZE_MB} MB.")
                        log.warning("Reject: id=%s size=%s", download_id, filesize)
                        self.update_state(download_id, status="error", message=msg)
                        return

                    mins, secs = divmod(int(duration), 60)
                    hrs, mins = divmod(mins, 60)
                    if hrs:
                        duration_str = f"{hrs}:{mins:02d}:{secs:02d}"
                    else:
                        duration_str = f"{mins}:{secs:02d}"
                    log.info("Info extracted: id=%s title='%s' duration=%ss size=%s",
                             download_id, title, duration, filesize)
                    self.update_state(
                        download_id,
                        title=title,
                        thumbnail=thumbnail,
                        duration=duration,
                        duration_str=duration_str,
                    )
            except Exception as e:
                msg = str(e)
                if msg.startswith("ERROR: "):
                    msg = msg[7:]
                log.error("Info extraction failed: id=%s error=%s", download_id, msg)
                if "authentication" in msg.lower() or "login" in msg.lower():
                    msg += "\n\nTips: Upload cookies.txt lewat menu 'Cookie Auth' di atas"
                self.update_state(
                    download_id, status="error", message=msg,
                )
                return

            output_template = os.path.join(
                self.downloads_dir, f"{download_id}.%(ext)s"
            )
            log.info("Output template: %s", output_template)

            if fmt == "mp3":
                ydl_opts = {
                    **self._base_opts(progress_hook),
                    "format": "ba/b",
                    "outtmpl": output_template,
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                }
                log.debug("Using MP3 opts")
            else:
                ydl_opts = {
                    **self._base_opts(progress_hook),
                    "format": "bv*+ba/b",
                    "merge_output_format": "mp4",
                    "outtmpl": output_template,
                }
                log.debug("Using MP4 opts")

            log.info("Starting download... id=%s", download_id)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                    log.info("ydl.download returned successfully: id=%s", download_id)
            except Exception as e:
                msg = str(e)
                if msg.startswith("ERROR: "):
                    msg = msg[7:]
                log.error("Download failed: id=%s error=%s", download_id, msg)
                if "authentication" in msg.lower() or "login" in msg.lower():
                    msg += "\n\nTips: Upload cookies.txt lewat menu 'Cookie Auth' di atas"
                self.update_state(
                    download_id, status="error", message=msg,
                )
                return

            ext = "mp3" if fmt == "mp3" else "mp4"
            expected = os.path.join(self.downloads_dir, f"{download_id}.{ext}")
            log.info("Checking output file: %s", expected)

            if os.path.exists(expected):
                size = os.path.getsize(expected)
                log.info("File found: %s size=%d bytes", expected, size)
                self.update_state(
                    download_id,
                    status="complete",
                    progress=100,
                    message="Selesai!",
                    filepath=expected,
                )
            else:
                found = None
                for f in os.listdir(self.downloads_dir):
                    if f.startswith(download_id):
                        found = os.path.join(self.downloads_dir, f)
                        break
                if found:
                    size = os.path.getsize(found)
                    log.info("File found (alt): %s size=%d bytes", found, size)
                    self.update_state(
                        download_id,
                        status="complete",
                        progress=100,
                        message="Selesai!",
                        filepath=found,
                    )
                else:
                    log.error("Output file not found for id=%s", download_id)
                    self.update_state(
                        download_id,
                        status="error",
                        message="File tidak ditemukan setelah download",
                    )

            elapsed = time.time() - start_time
            log.info("Worker done: id=%s elapsed=%.1fs", download_id, elapsed)

        finally:
            self.semaphore.release()
            log.debug("Semaphore released: id=%s", download_id)
