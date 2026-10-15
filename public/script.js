document.addEventListener('DOMContentLoaded', () => {
    const urlInput = document.getElementById('urlInput');
    const extractForm = document.getElementById('extractForm');
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
    const chooseFolderBtn = document.getElementById('chooseFolderBtn');
    const saveFolderName = document.getElementById('saveFolderName');
    const clearFolderBtn = document.getElementById('clearFolderBtn');
    const audioToggleWrapper = document.getElementById('audioToggleWrapper');
    const audioOnlyToggle = document.getElementById('audioOnlyToggle');
    const queueIndicator = document.getElementById('queueIndicator');

    let currentExtractionData = null;
    let currentSourceUrl = null;
    let activeAbortController = null;

    const EXTRACT_TIMEOUT_MS = 90_000;
    const DOWNLOAD_TIMEOUT_MS = 300_000;
    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1500;

    // ── Save-to-folder ──────────────────────────────────────────────────────
    // The File System Access API needs a secure context (https, or localhost) and is Chromium-only.
    // Everywhere else the picker stays hidden and media goes to the browser's own download folder.
    const DIR_HANDLE_KEY = 'saveDirHandle';
    const IDB_NAME = 'anygrab';
    const IDB_STORE = 'handles';
    const supportsDirectoryPicker =
        typeof window.showDirectoryPicker === 'function' && window.isSecureContext;

    let saveDirHandle = null;

    function openHandleDb() {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open(IDB_NAME, 1);
            req.onupgradeneeded = () => {
                if (!req.result.objectStoreNames.contains(IDB_STORE)) {
                    req.result.createObjectStore(IDB_STORE);
                }
            };
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
    }

    async function handleDbOp(mode, run) {
        const db = await openHandleDb();
        try {
            return await new Promise((resolve, reject) => {
                const tx = db.transaction(IDB_STORE, mode);
                const req = run(tx.objectStore(IDB_STORE));
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => reject(req.error);
            });
        } finally {
            db.close();
        }
    }

    // A directory handle survives reloads, but its permission grant does not: the browser downgrades
    // it to "prompt", and re-granting is only allowed from a user gesture. Hence `interactive`.
    async function hasFolderPermission(handle, interactive) {
        const opts = { mode: 'readwrite' };
        if ((await handle.queryPermission(opts)) === 'granted') return true;
        if (!interactive) return false;
        return (await handle.requestPermission(opts)) === 'granted';
    }

    function renderSaveTarget(needsReauth) {
        if (!saveFolderName) return;
        if (saveDirHandle) {
            saveFolderName.textContent = needsReauth
                ? `${saveDirHandle.name} — click to allow`
                : saveDirHandle.name;
            clearFolderBtn.classList.remove('hide');
        } else {
            saveFolderName.textContent = supportsDirectoryPicker
                ? 'Choose folder…'
                : 'Browser downloads';
            clearFolderBtn.classList.add('hide');
        }
    }

    async function restoreSaveFolder() {
        if (!supportsDirectoryPicker) {
            chooseFolderBtn.disabled = true;
            chooseFolderBtn.title = 'Folder picking needs Chrome or Edge over https/localhost';
            renderSaveTarget(false);
            return;
        }
        try {
            const stored = await handleDbOp('readonly', store => store.get(DIR_HANDLE_KEY));
            if (!stored) return renderSaveTarget(false);
            saveDirHandle = stored;
            renderSaveTarget(!(await hasFolderPermission(stored, false)));
        } catch {
            renderSaveTarget(false);
        }
    }

    async function chooseSaveFolder() {
        if (!supportsDirectoryPicker) return;
        let handle;
        try {
            handle = await window.showDirectoryPicker({ mode: 'readwrite', startIn: 'downloads' });
        } catch (err) {
            if (err.name !== 'AbortError') showToast(`Could not open folder picker: ${err.message}`, true);
            return;
        }
        if (!(await hasFolderPermission(handle, true))) {
            showToast('Write permission denied for that folder.', true);
            return;
        }
        saveDirHandle = handle;
        try {
            await handleDbOp('readwrite', store => store.put(handle, DIR_HANDLE_KEY));
        } catch {
            showToast('Folder set, but it could not be remembered for next time.', true);
        }
        renderSaveTarget(false);
        showToast(`Saving to "${handle.name}"`, false);
    }

    async function clearSaveFolder() {
        saveDirHandle = null;
        try {
            await handleDbOp('readwrite', store => store.delete(DIR_HANDLE_KEY));
        } catch { /* the in-memory handle is already cleared, which is what matters */ }
        renderSaveTarget(false);
        showToast('Using browser downloads.', false);
    }

    // The server knows the real container format (YouTube audio arrives as .m4a, not .mp3), so trust
    // its Content-Disposition over anything guessed from the media URL.
    function extensionFor(response, fallbackExt) {
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
        if (match) {
            const ext = match[1].trim().split('.').pop();
            if (ext && /^[A-Za-z0-9]{1,5}$/.test(ext)) return ext.toLowerCase();
        }
        return fallbackExt;
    }

    async function authorizeSaveFolder() {
        const folder = saveDirHandle;
        if (!folder) return null;
        // Renew permission during the save gesture, before a slow network request can
        // consume the browser's transient user activation. Keep this handle for the batch.
        if (!(await hasFolderPermission(folder, true))) {
            throw new Error(`Permission to write into "${folder.name}" was denied. Choose another folder.`);
        }
        renderSaveTarget(false);
        return folder;
    }

    async function writeToChosenFolder(response, filename, folder) {
        const fileHandle = await folder.getFileHandle(filename, { create: true });
        const writable = await fileHandle.createWritable();
        try {
            // Stream straight to disk so a large video never has to sit in memory as a Blob.
            if (response.body) {
                await response.body.pipeTo(writable);
            } else {
                await writable.write(await response.blob());
                await writable.close();
            }
        } catch (err) {
            try { await writable.abort(); } catch { /* already closed */ }
            throw err;
        }
        return (await fileHandle.getFile()).size;
    }

    async function saveViaBrowser(response, filename) {
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = objectUrl;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
        return blob.size;
    }

    async function deliverMedia(mediaUrl, baseName, fallbackExt, folder, mediaIndex = 0) {
        const savedFind = {
            source_url: currentSourceUrl,
            title: currentExtractionData.metadata?.title || currentExtractionData.caption || 'Untitled media',
            platform: currentExtractionData.platform,
            destination: folder ? 'folder' : 'browser',
            media_index: mediaIndex,
            audio_only: audioOnlyToggle.checked,
        };
        const response = await fetchWithRetry('/api/v1/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                original_url: currentSourceUrl,
                url: mediaUrl,
                headers: currentExtractionData.http_headers || {},
                audio_only: audioOnlyToggle.checked,
            }),
        }, DOWNLOAD_TIMEOUT_MS, 1);

        if (!response.ok) {
            let detail = 'Download failed';
            try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
            throw new Error(detail);
        }

        const filename = `${baseName}.${extensionFor(response, fallbackExt)}`;

        // Some proxied paths omit Content-Length, so measure what actually landed on disk.
        const size = folder
            ? await writeToChosenFolder(response, filename, folder)
            : await saveViaBrowser(response, filename);
        document.dispatchEvent(new CustomEvent('anygrab:saved', { detail: { ...savedFind, filename } }));
        return { filename, size };
    }

    chooseFolderBtn.addEventListener('click', chooseSaveFolder);
    clearFolderBtn.addEventListener('click', clearSaveFolder);
    restoreSaveFolder();

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

    function cancelActiveRequest() {
        if (activeAbortController) {
            activeAbortController.abort();
            activeAbortController = null;
        }
    }

    function validateLink(value) {
        if (!value) return 'Paste a media link to get started.';
        try {
            const url = new URL(value);
            if (url.protocol === 'https:' || url.protocol === 'http:') return null;
        } catch { /* Give the same guidance for malformed links. */ }
        return 'Enter a valid link starting with https:// or http://.';
    }

    function saveActionLabel() {
        if (!currentExtractionData) return 'Save video';
        if (audioOnlyToggle.checked) return 'Save audio (MP3)';
        const urls = currentExtractionData.media_urls;
        if (urls.length > 1) return `Save all (${urls.length})`;
        return isImageUrl(urls[0]) ? 'Save photo' : 'Save video';
    }

    mediaThumbnail.addEventListener('error', () => {
        if (!mediaThumbnail.src.endsWith('/media-placeholder.svg')) {
            mediaThumbnail.src = 'media-placeholder.svg';
        }
    });

    urlInput.addEventListener('input', () => {
        urlInput.removeAttribute('aria-invalid');
        errorBox.classList.add('hide');
    });

    extractForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (extractBtn.disabled) return;
        const url = urlInput.value.trim();
        const validationError = validateLink(url);
        if (validationError) {
            errorBox.textContent = validationError;
            errorBox.classList.remove('hide');
            urlInput.setAttribute('aria-invalid', 'true');
            urlInput.focus();
            return;
        }

        cancelActiveRequest();
        currentExtractionData = null;
        currentSourceUrl = null;
        document.dispatchEvent(new CustomEvent('anygrab:media', { detail: null }));
        urlInput.removeAttribute('aria-invalid');
        errorBox.classList.add('hide');
        resultCard.classList.add('hide');
        carouselGallery.classList.add('hide');
        carouselGallery.innerHTML = '';
        audioToggleWrapper.classList.add('hide');
        audioOnlyToggle.checked = false;
        extractSpinner.classList.remove('hide');
        extractText.textContent = 'Finding media…';
        extractBtn.disabled = true;
        extractBtn.setAttribute('aria-busy', 'true');

        try {
            const response = await fetchWithRetry('/api/v1/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            }, EXTRACT_TIMEOUT_MS);

            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Extraction failed.');

            if (!Array.isArray(data.media_urls) || data.media_urls.length === 0 ||
                data.media_urls.some(item => typeof item !== 'string' || !item.trim())) {
                throw new Error('No downloadable media was found. Try another link.');
            }

            currentExtractionData = data;
            currentSourceUrl = url;
            const metadata = data.metadata || {};
            platformBadge.textContent = data.platform;
            mediaTitle.textContent = metadata.title || data.caption || 'Untitled media';
            mediaThumbnail.src = metadata.thumbnail ||
                (isImageUrl(data.media_urls[0]) ? data.media_urls[0] : 'media-placeholder.svg');

            const views = metadata.view_count;
            const likes = metadata.like_count ?? metadata.likes;
            mediaViews.textContent = views == null ? 'Not available' : Number(views).toLocaleString('en-US');
            mediaLikes.textContent = likes == null ? 'Not available' : Number(likes).toLocaleString('en-US');

            const isCarousel = data.media_urls.length > 1;
            const isYouTube = data.platform === 'youtube';

            if (isYouTube) audioToggleWrapper.classList.remove('hide');

            if (isCarousel) {
                renderCarouselGallery(data.media_urls);
            }
            downloadText.textContent = saveActionLabel();

            document.dispatchEvent(new CustomEvent('anygrab:media', { detail: {
                source_url: url, title: mediaTitle.textContent, platform: data.platform,
                media_count: data.media_urls.length, audio_only: audioOnlyToggle.checked,
            } }));
            resultCard.classList.remove('hide');
            mediaTitle.focus({ preventScroll: true });
            resultCard.scrollIntoView({ block: 'nearest' });
        } catch (err) {
            errorBox.textContent = err.message;
            errorBox.classList.remove('hide');
        } finally {
            extractSpinner.classList.add('hide');
            extractText.textContent = 'Find media';
            extractBtn.disabled = false;
            extractBtn.removeAttribute('aria-busy');
        }
    });

    audioOnlyToggle.addEventListener('change', () => {
        if (!currentExtractionData) return;
        downloadText.textContent = saveActionLabel();
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
                vid.preload = 'metadata';
                vid.addEventListener('mouseenter', () => {
                    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
                        vid.play().catch(() => { /* Preview playback may be blocked by the browser. */ });
                    }
                });
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
            btn.type = 'button';
            btn.textContent = `Save ${i + 1}`;
            btn.title = `Save item ${i + 1}`;
            btn.setAttribute('aria-label', `Save item ${i + 1}`);
            btn.addEventListener('click', async () => {
                if (downloadBtn.disabled) return;
                setDownloadBusy(true);
                btn.disabled = true;
                try {
                    const folder = await authorizeSaveFolder();
                    await saveSingleItem(url, i, folder);
                } catch (err) {
                    showToast(`Failed: ${err.message}`, true);
                }
                finally {
                    btn.disabled = false;
                    setDownloadBusy(false);
                }
            });
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
        toast.setAttribute('role', isError ? 'alert' : 'status');
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

    async function saveSingleItem(mediaUrl, index, folder) {
        const fallbackExt = isImageUrl(mediaUrl) ? 'jpg' : 'mp4';
        const baseName = `AnyGrab_${Date.now()}_${index + 1}`;
        const data = await deliverMedia(mediaUrl, baseName, fallbackExt, folder, index);
        showToast(`Saved ${data.filename} (${formatSize(data.size)})`, false);
        return data;
    }

    function setDownloadBusy(busy) {
        downloadBtn.disabled = busy;
        downloadBtn.setAttribute('aria-busy', String(busy));
        extractBtn.disabled = busy;
        audioOnlyToggle.disabled = busy;
        chooseFolderBtn.disabled = busy || !supportsDirectoryPicker;
        clearFolderBtn.disabled = busy;
    }

    async function saveAllItems(mediaUrls, folder) {
        downloadText.textContent = `Saving 0/${mediaUrls.length}…`;
        let completed = 0;
        const BATCH_SIZE = 3;
        for (let i = 0; i < mediaUrls.length; i += BATCH_SIZE) {
            const batch = mediaUrls.slice(i, i + BATCH_SIZE);
            await Promise.all(batch.map((url, batchIdx) => {
                const globalIdx = i + batchIdx;
                return saveSingleItem(url, globalIdx, folder)
                    .then(() => { completed++; downloadText.textContent = `Saving ${completed}/${mediaUrls.length}…`; })
                    .catch(err => console.error(`Failed item ${globalIdx + 1}:`, err));
            }));
        }
        const partial = completed < mediaUrls.length;
        showToast(`Saved ${completed}/${mediaUrls.length} files${partial ? '. Try saving the remaining items individually.' : ''}`, partial);
    }

    downloadBtn.addEventListener('click', async () => {
        if (!currentExtractionData || downloadBtn.disabled) return;
        downloadSpinner.classList.remove('hide');
        setDownloadBusy(true);
        downloadText.textContent = 'Saving…';
        try {
            const folder = await authorizeSaveFolder();
            const mediaUrls = currentExtractionData.media_urls;
            if (mediaUrls.length > 1) {
                await saveAllItems(mediaUrls, folder);
            } else {
                const fallbackExt = audioOnlyToggle.checked
                    ? 'm4a'
                    : (isImageUrl(mediaUrls[0]) ? 'jpg' : 'mp4');
                const data = await deliverMedia(mediaUrls[0], `AnyGrab_${Date.now()}`, fallbackExt, folder);
                showToast(`Saved ${data.filename} (${formatSize(data.size)})`, false);
            }
        } catch (err) {
            showToast(`Failed: ${err.message}`, true);
        } finally {
            downloadText.textContent = saveActionLabel();
            downloadSpinner.classList.add('hide');
            setDownloadBusy(false);
        }
    });

});
