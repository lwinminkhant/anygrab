document.addEventListener('DOMContentLoaded', () => {
    const urlInput = document.getElementById('urlInput');
    const extractBtn = document.getElementById('extractBtn');
    const extractSpinner = document.getElementById('extractSpinner');
    const extractText = extractBtn.querySelector('.btn-text');
    
    const errorBox = document.getElementById('errorBox');
    const resultCard = document.getElementById('resultCard');
    
    const mediaThumbnail = document.getElementById('mediaThumbnail');
    const platformBadge = document.getElementById('platformBadge');
    const mediaTitle = document.getElementById('mediaTitle');
    const mediaViews = document.getElementById('mediaViews');
    const mediaLikes = document.getElementById('mediaLikes');
    
    const downloadBtn = document.getElementById('downloadBtn');
    const downloadSpinner = document.getElementById('downloadSpinner');
    const downloadText = downloadBtn.querySelector('.btn-text');

    const carouselGallery = document.getElementById('carouselGallery');
    const saveFolderDisplay = document.getElementById('saveFolder');
    const audioToggleWrapper = document.getElementById('audioToggleWrapper');
    const audioOnlyToggle = document.getElementById('audioOnlyToggle');
    const queueIndicator = document.getElementById('queueIndicator');
    const cookieFileInput = document.getElementById('cookieFileInput');
    const cookieUploadBtn = document.getElementById('cookieUploadBtn');
    const cookieStatus = document.getElementById('cookieStatus');

    let currentExtractionData = null;
    let activeAbortController = null;

    const EXTRACT_TIMEOUT_MS = 90_000;
    const DOWNLOAD_TIMEOUT_MS = 300_000;
    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1500;

    fetch('/api/v1/settings').then(r => r.json()).then(data => {
        if (saveFolderDisplay) saveFolderDisplay.textContent = data.download_dir;
        if (cookieStatus && data.cookies_present) {
            cookieStatus.textContent = 'Cookies loaded';
            cookieStatus.classList.add('ok');
        }
    }).catch(() => {});

    function startQueuePolling() {
        if (!queueIndicator) return;
        async function poll() {
            try {
                const r = await fetch('/api/v1/queue');
                const q = await r.json();
                const extAvail = q.extractions.available;
                const dlAvail = q.downloads.available;
                if (extAvail === 0 || dlAvail === 0) {
                    queueIndicator.textContent = `Queue: ${q.extractions.active}/${q.extractions.max} extractions, ${q.downloads.active}/${q.downloads.max} downloads`;
                    queueIndicator.classList.remove('hide');
                } else {
                    queueIndicator.classList.add('hide');
                }
            } catch { /* ignore */ }
        }
        setInterval(poll, 5000);
    }
    startQueuePolling();

    async function fetchWithRetry(url, options, timeoutMs, maxRetries = MAX_RETRIES) {
        let lastError;
        for (let attempt = 0; attempt <= maxRetries; attempt++) {
            const controller = new AbortController();
            activeAbortController = controller;
            const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

            try {
                const resp = await fetch(url, { ...options, signal: controller.signal });
                clearTimeout(timeoutId);

                if (resp.status === 429) {
                    const retryAfter = parseInt(resp.headers.get('Retry-After') || '5', 10);
                    showToast(`Rate limited — retrying in ${retryAfter}s…`, true);
                    await sleep(retryAfter * 1000);
                    continue;
                }

                if (resp.status === 504 && attempt < maxRetries) {
                    showToast(`Server busy — retry ${attempt + 1}/${maxRetries}…`, true);
                    await sleep(RETRY_DELAY_MS * (attempt + 1));
                    continue;
                }

                return resp;
            } catch (err) {
                clearTimeout(timeoutId);
                lastError = err;

                if (err.name === 'AbortError') {
                    if (attempt < maxRetries) {
                        showToast(`Timed out — retry ${attempt + 1}/${maxRetries}…`, true);
                        await sleep(RETRY_DELAY_MS);
                        continue;
                    }
                    throw new Error('Request timed out. The server may be overloaded — try again later.');
                }
                if (attempt < maxRetries) {
                    await sleep(RETRY_DELAY_MS * (attempt + 1));
                    continue;
                }
            }
        }
        throw lastError || new Error('Request failed after retries.');
    }

    function sleep(ms) {
        return new Promise(r => setTimeout(r, ms));
    }

    async function uploadCookies() {
        if (!cookieFileInput || !cookieFileInput.files.length) {
            showToast('Choose a cookies.txt file first.', true);
            return;
        }
        const file = cookieFileInput.files[0];
        const formData = new FormData();
        formData.append('file', file);

        cookieUploadBtn.disabled = true;
        const prevText = cookieUploadBtn.textContent;
        cookieUploadBtn.textContent = 'Uploading…';
        if (cookieStatus) {
            cookieStatus.textContent = '';
            cookieStatus.classList.remove('ok');
        }

        try {
            const resp = await fetch('/api/v1/cookies', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json();
            if (!resp.ok || !data.ok) {
                throw new Error(data.detail || 'Upload failed');
            }
            showToast('Cookies uploaded successfully.', false);
            if (cookieStatus) {
                cookieStatus.textContent = 'Cookies loaded';
                cookieStatus.classList.add('ok');
            }
        } catch (err) {
            showToast(err.message || 'Upload failed.', true);
            if (cookieStatus) {
                cookieStatus.textContent = 'Upload error';
                cookieStatus.classList.remove('ok');
            }
        } finally {
            cookieUploadBtn.disabled = false;
            cookieUploadBtn.textContent = prevText;
        }
    }

    function cancelActiveRequest() {
        if (activeAbortController) {
            activeAbortController.abort();
            activeAbortController = null;
        }
    }

    extractBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if (!url) return;

        cancelActiveRequest();
        errorBox.classList.add('hide');
        resultCard.classList.add('hide');
        carouselGallery.classList.add('hide');
        carouselGallery.innerHTML = '';
        audioToggleWrapper.classList.add('hide');
        audioOnlyToggle.checked = false;
        extractSpinner.classList.remove('hide');
        extractText.textContent = 'Extracting…';
        extractBtn.disabled = true;

        try {
            const response = await fetchWithRetry('/api/v1/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            }, EXTRACT_TIMEOUT_MS);

            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Extraction failed.');

            currentExtractionData = data;
            platformBadge.textContent = data.platform;
            mediaTitle.textContent = data.metadata.title || data.caption || 'Untitled Media';
            mediaThumbnail.src = data.metadata.thumbnail || data.media_urls[0] || 'https://via.placeholder.com/600x400?text=No+Thumbnail';

            const views = data.metadata.view_count;
            const likes = data.metadata.like_count || data.metadata.likes;
            mediaViews.textContent = `👁 ${views ? views.toLocaleString() : 'N/A'}`;
            mediaLikes.textContent = `❤️ ${likes ? likes.toLocaleString() : 'N/A'}`;

            const isCarousel = data.media_urls.length > 1;
            const isYouTube = data.platform === 'youtube';

            if (isYouTube) audioToggleWrapper.classList.remove('hide');

            if (isCarousel) {
                downloadText.textContent = `Save All (${data.media_urls.length})`;
                renderCarouselGallery(data.media_urls);
            } else {
                downloadText.textContent = 'Save to Disk';
            }

            resultCard.classList.remove('hide');
        } catch (err) {
            errorBox.textContent = err.message;
            errorBox.classList.remove('hide');
        } finally {
            extractSpinner.classList.add('hide');
            extractText.textContent = 'Extract';
            extractBtn.disabled = false;
        }
    });

    audioOnlyToggle.addEventListener('change', () => {
        if (!currentExtractionData) return;
        const isCarousel = currentExtractionData.media_urls.length > 1;
        downloadText.textContent = audioOnlyToggle.checked
            ? 'Save Audio (MP3)'
            : isCarousel ? `Save All (${currentExtractionData.media_urls.length})` : 'Save to Disk';
    });

    function renderCarouselGallery(mediaUrls) {
        carouselGallery.innerHTML = '';
        mediaUrls.forEach((url, i) => {
            const item = document.createElement('div');
            item.className = 'carousel-item';

            const isVideo = url.includes('.mp4') || url.includes('/video/');
            if (isVideo) {
                const vid = document.createElement('video');
                vid.src = url;
                vid.muted = true;
                vid.loop = true;
                vid.addEventListener('mouseenter', () => vid.play());
                vid.addEventListener('mouseleave', () => { vid.pause(); vid.currentTime = 0; });
                item.appendChild(vid);
            } else {
                const img = document.createElement('img');
                img.src = url;
                img.alt = `Slide ${i + 1}`;
                img.loading = 'lazy';
                item.appendChild(img);
            }

            const btn = document.createElement('button');
            btn.className = 'carousel-download-btn';
            btn.textContent = `${i + 1}`;
            btn.title = `Save item ${i + 1}`;
            btn.addEventListener('click', () => saveSingleItem(url, i));
            item.appendChild(btn);

            carouselGallery.appendChild(item);
        });
        carouselGallery.classList.remove('hide');
    }

    function isImageUrl(url) {
        return /\.(jpg|jpeg|png|webp)/i.test(url) || (!url.includes('.mp4') && !url.includes('/video/'));
    }

    function showToast(message, isError) {
        const existing = document.querySelector('.toast');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = 'toast' + (isError ? ' toast-error' : '');
        toast.textContent = message;
        document.body.appendChild(toast);
        requestAnimationFrame(() => toast.classList.add('toast-show'));
        setTimeout(() => {
            toast.classList.remove('toast-show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    async function saveSingleItem(mediaUrl, index) {
        const ext = isImageUrl(mediaUrl) ? 'jpg' : 'mp4';
        const filename = `AnyGrab_${Date.now()}_${index + 1}.${ext}`;
        try {
            const response = await fetchWithRetry('/api/v1/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    original_url: urlInput.value,
                    url: mediaUrl,
                    headers: currentExtractionData.http_headers || {},
                    filename,
                    audio_only: audioOnlyToggle.checked,
                }),
            }, DOWNLOAD_TIMEOUT_MS, 1);

            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Save failed');
            showToast(`Saved ${data.filename} (${formatSize(data.size)})`, false);
            return data;
        } catch (err) {
            showToast(`Failed: ${err.message}`, true);
            throw err;
        }
    }

    downloadBtn.addEventListener('click', async () => {
        if (!currentExtractionData) return;

        downloadSpinner.classList.remove('hide');
        downloadBtn.disabled = true;

        const mediaUrls = currentExtractionData.media_urls;
        const isCarousel = mediaUrls.length > 1;

        if (isCarousel) {
            downloadText.textContent = `Saving 0/${mediaUrls.length}…`;
            let completed = 0;

            const BATCH_SIZE = 3;
            for (let i = 0; i < mediaUrls.length; i += BATCH_SIZE) {
                const batch = mediaUrls.slice(i, i + BATCH_SIZE);
                const promises = batch.map((url, batchIdx) => {
                    const globalIdx = i + batchIdx;
                    return saveSingleItem(url, globalIdx)
                        .then(() => { completed++; downloadText.textContent = `Saving ${completed}/${mediaUrls.length}…`; })
                        .catch(err => console.error(`Failed item ${globalIdx + 1}:`, err));
                });
                await Promise.all(promises);
            }

            showToast(`Saved ${completed}/${mediaUrls.length} files`, completed < mediaUrls.length);
            downloadText.textContent = `Save All (${mediaUrls.length})`;
        } else {
            downloadText.textContent = 'Saving…';
            try {
                const ext = audioOnlyToggle.checked ? 'mp3' : (isImageUrl(mediaUrls[0]) ? 'jpg' : 'mp4');
                const filename = `AnyGrab_${Date.now()}.${ext}`;
                const response = await fetchWithRetry('/api/v1/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        original_url: urlInput.value,
                        url: mediaUrls[0],
                        headers: currentExtractionData.http_headers || {},
                        filename,
                        audio_only: audioOnlyToggle.checked,
                    }),
                }, DOWNLOAD_TIMEOUT_MS, 1);

                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Save failed');
                showToast(`Saved ${data.filename} (${formatSize(data.size)})`, false);
            } catch (err) {
                showToast(`Failed: ${err.message}`, true);
            }
            downloadText.textContent = audioOnlyToggle.checked ? 'Save Audio (MP3)' : 'Save to Disk';
        }

        downloadSpinner.classList.add('hide');
        downloadBtn.disabled = false;
    });

    urlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') extractBtn.click();
    });

    if (cookieUploadBtn) {
        cookieUploadBtn.addEventListener('click', uploadCookies);
    }
});
