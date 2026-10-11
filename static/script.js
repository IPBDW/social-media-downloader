let state = {
  scannedUrl: '',
  downloadId: null,
  eventSource: null,
  isScanning: false,
  isDownloading: false,
};

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  clearTimeout(t._hide);
  t._hide = setTimeout(() => t.classList.add('hidden'), 4000);
}

async function checkCookieStatus() {
  const el = document.getElementById('cookieStatus');
  try {
    const res = await fetch('/api/cookies/status');
    const data = await res.json();
    if (data.active) {
      el.textContent = 'Aktif';
      el.className = 'cookie-status active';
      document.getElementById('cookieClearBtn').classList.remove('hidden');
    } else {
      el.textContent = 'Tidak aktif';
      el.className = 'cookie-status';
      document.getElementById('cookieClearBtn').classList.add('hidden');
    }
  } catch (e) {
    el.textContent = 'Gagal cek';
    el.className = 'cookie-status error';
  }
}

async function uploadCookies(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.txt')) {
    showToast('File harus berupa cookies.txt', 'error');
    return;
  }
  const fd = new FormData();
  fd.append('file', file);
  const el = document.getElementById('cookieStatus');
  el.textContent = 'Uploading...';
  el.className = 'cookie-status';
  try {
    const res = await fetch('/api/cookies', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Gagal upload cookies');
    el.textContent = 'Aktif';
    el.className = 'cookie-status active';
    document.getElementById('cookieClearBtn').classList.remove('hidden');
    showToast('Cookies berhasil disimpan!', 'success');
  } catch (e) {
    el.textContent = 'Gagal upload';
    el.className = 'cookie-status error';
    showToast(e.message, 'error');
  }
}

async function clearCookies() {
  const el = document.getElementById('cookieStatus');
  el.textContent = 'Menghapus...';
  el.className = 'cookie-status';
  try {
    const res = await fetch('/api/cookies', { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Gagal hapus cookies');
    el.textContent = 'Tidak aktif';
    el.className = 'cookie-status';
    document.getElementById('cookieClearBtn').classList.add('hidden');
    document.getElementById('cookieFile').value = '';
    showToast('Cookies dihapus', 'success');
  } catch (e) {
    el.textContent = 'Gagal hapus';
    el.className = 'cookie-status error';
    showToast(e.message, 'error');
  }
}

function showScanResult() {
  document.getElementById('scanResult').classList.remove('hidden');
}

function hideScanResult() {
  document.getElementById('scanResult').classList.add('hidden');
}

function setButtonLoading(btn, loading, originalText) {
  btn.disabled = loading;
  btn.textContent = loading ? originalText + '...' : originalText;
}

function closeEventSource() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function resetProgressArea() {
  document.getElementById('pickFormat').classList.add('hidden');
  document.getElementById('progressArea').classList.add('hidden');
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('progressPercent').textContent = '0%';
  document.getElementById('progressSpeed').textContent = '';
  document.getElementById('videoStatus').textContent = '';
  document.getElementById('videoStatus').className = 'video-status';
}

function finish() {
  closeEventSource();
  state.isDownloading = false;
  state.downloadId = null;
  setButtonLoading(document.getElementById('scanBtn'), false, 'Scan');
  document.getElementById('pickFormat').querySelectorAll('button').forEach(b => b.disabled = false);
}

async function scanUrl() {
  if (state.isScanning) return;

  const url = document.getElementById('urlInput').value.trim();
  if (!url) {
    showToast('Masukkan link video terlebih dahulu!', 'error');
    return;
  }

  const btn = document.getElementById('scanBtn');
  state.isScanning = true;
  setButtonLoading(btn, true, 'Scan');
  state.scannedUrl = url;

  // Tutup EventSource lama kalau ada download yang masih jalan
  closeEventSource();
  if (state.isDownloading) {
    state.isDownloading = false;
    state.downloadId = null;
  }

  hideScanResult();
  resetProgressArea();

  try {
    const res = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });

    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || 'Gagal scan link');
    }

    document.getElementById('thumbnail').src = data.thumbnail || '';
    document.getElementById('thumbDuration').textContent = data.duration || '';
    document.getElementById('videoTitle').textContent = data.title || 'Video';
    document.getElementById('videoStatus').textContent = data.duration || '';

    document.getElementById('pickFormat').classList.remove('hidden');
    showScanResult();

  } catch (e) {
    showToast(e.message, 'error');
    const status = document.getElementById('videoStatus');
    status.textContent = e.message;
    status.className = 'video-status error';
    document.getElementById('pickFormat').classList.add('hidden');
    showScanResult();
  } finally {
    state.isScanning = false;
    setButtonLoading(btn, false, 'Scan');
  }
}

async function startDownload(fmt) {
  if (state.isDownloading || !state.scannedUrl) return;

  fmt = fmt.toLowerCase();
  if (fmt !== 'mp3' && fmt !== 'mp4') {
    showToast('Format tidak valid', 'error');
    return;
  }

  closeEventSource();
  resetProgressArea();

  state.isDownloading = true;
  state.downloadId = null;

  document.getElementById('videoStatus').textContent = 'Memulai download ' + fmt.toUpperCase() + '...';
  document.getElementById('progressArea').classList.remove('hidden');

  const pickBtns = document.getElementById('pickFormat').querySelectorAll('button');
  pickBtns.forEach(b => b.disabled = true);

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: state.scannedUrl, format: fmt }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Gagal mulai download');
    }

    const data = await res.json();
    state.downloadId = data.download_id;
    listenProgress(state.downloadId, fmt);

  } catch (e) {
    document.getElementById('videoStatus').textContent = e.message;
    document.getElementById('videoStatus').className = 'video-status error';
    showToast(e.message, 'error');
    finish();
  }
}

function listenProgress(id, fmt) {
  closeEventSource();

  state.eventSource = new EventSource('/api/progress/' + id);

  let progressTimeout = null;

  const resetProgressTimeout = () => {
    clearTimeout(progressTimeout);
    progressTimeout = setTimeout(() => {
      if (state.eventSource) {
        state.eventSource.close();
        document.getElementById('videoStatus').textContent = 'Koneksi terputus, coba lagi';
        document.getElementById('videoStatus').className = 'video-status error';
        showToast('Koneksi progress timeout', 'error');
        finish();
      }
    }, 300000);
  };

  state.eventSource.onmessage = (e) => {
    resetProgressTimeout();
    try {
      const data = JSON.parse(e.data);

      if (data.status === 'not_found') {
        document.getElementById('videoStatus').textContent = 'Download tidak ditemukan';
        finish();
        return;
      }

      if (data.title) {
        document.getElementById('videoTitle').textContent = data.title;
      }
      if (data.duration) {
        document.getElementById('thumbDuration').textContent = data.duration;
        document.getElementById('videoStatus').textContent = data.duration;
      }

      const fill = document.getElementById('progressFill');
      const pct = document.getElementById('progressPercent');
      const speed = document.getElementById('progressSpeed');
      const status = document.getElementById('videoStatus');

      fill.style.width = data.progress + '%';
      pct.textContent = data.progress + '%';

      if (data.speed) {
        speed.textContent = data.speed + (data.eta ? ' - sisa ' + data.eta : '');
      }

      if (data.status === 'extracting') {
        status.textContent = 'Mendapatkan informasi...';
      } else if (data.status === 'downloading') {
        status.textContent = 'Mendownload ' + fmt.toUpperCase() + '... ' + data.progress + '%';
      } else if (data.status === 'processing') {
        status.textContent = 'Memproses file...';
      } else if (data.status === 'complete') {
        status.textContent = 'Selesai!';
        status.className = 'video-status';
        fill.style.width = '100%';
        pct.textContent = '100%';
        speed.textContent = '';
        const a = document.createElement('a');
        a.href = '/api/file/' + id;
        a.download = '';
        document.body.appendChild(a);
        a.click();
        a.remove();
        finish();
      } else if (data.status === 'error') {
        status.textContent = data.message;
        status.className = 'video-status error';
        showToast(data.message, 'error');
        finish();
      } else if (data.status === 'queued') {
        status.textContent = 'Menunggu antrian...';
      }
    } catch (err) {}
  };

  state.eventSource.onerror = () => {
    if (state.eventSource) {
      state.eventSource.close();
      if (state.isDownloading) {
        document.getElementById('videoStatus').textContent = 'Koneksi terputus';
        document.getElementById('videoStatus').className = 'video-status error';
        showToast('Koneksi server terputus', 'error');
        finish();
      }
    }
  };

  resetProgressTimeout();
}

document.getElementById('urlInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') scanUrl();
});

document.getElementById('cookieFile').addEventListener('change', (e) => {
  uploadCookies(e.target.files[0]);
});

checkCookieStatus();
