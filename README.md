# Social Downloader

Download video/audio dari TikTok, YouTube, Instagram, Reddit, Facebook, dan lainnya.

## Features

- 🎬 Download MP4 video
- 🎵 Download MP3 audio
- 📱 Support TikTok, YouTube, Instagram, Reddit, Facebook
- 🔒 Full privacy - data otomatis terhapus setelah dikirim
- ⚡ Fast download dengan progress tracking
- 🎨 Modern dark UI

## Tech Stack

- **Backend**: FastAPI + yt-dlp
- **Frontend**: Vanilla JS + CSS
- **Container**: Docker

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/social-media-downloader.git
cd social-media-downloader
docker compose up -d
```

Akses di: `http://localhost:8080`

## Privacy

- File dihapus otomatis setelah dikirim ke browser
- Data tersimpan di RAM (tmpfs), bukan disk
- Cleanup otomatis tiap 30 detik
- Tidak ada data yang tersimpan permanen

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/scan` | Scan URL video |
| POST | `/api/download` | Mulai download |
| GET | `/api/progress/{id}` | SSE progress |
| GET | `/api/file/{id}` | Download file |
| POST | `/api/cookies` | Upload cookies.txt |

## License

MIT
