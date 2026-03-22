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

    let currentExtractionData = null;

    extractBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if(!url) return;

        errorBox.classList.add('hide');
        resultCard.classList.add('hide');
        carouselGallery.classList.add('hide');
        carouselGallery.innerHTML = '';
        extractSpinner.classList.remove('hide');
        extractText.textContent = 'Extracting...';
        extractBtn.disabled = true;

        try {
            const response = await fetch('/api/v1/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || 'Extraction failed.');
            }

            currentExtractionData = data;

            platformBadge.textContent = data.platform;
            mediaTitle.textContent = data.metadata.title || data.caption || 'Untitled Media';
            mediaThumbnail.src = data.metadata.thumbnail || data.media_urls[0] || 'https://via.placeholder.com/600x400?text=No+Thumbnail';
            
            const views = data.metadata.view_count;
            const likes = data.metadata.like_count || data.metadata.likes;
            mediaViews.textContent = `👁 ${views ? views.toLocaleString() : 'N/A'}`;
            mediaLikes.textContent = `❤️ ${likes ? likes.toLocaleString() : 'N/A'}`;

            const isCarousel = data.media_urls.length > 1;
            const isImage = !data.metadata.is_video && data.media_urls[0] && !data.metadata.duration;

            if (isCarousel) {
                downloadText.textContent = `Download All (${data.media_urls.length})`;
                renderCarouselGallery(data.media_urls);
            } else {
                downloadText.textContent = 'Download High Quality';
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
                item.appendChild(img);
            }

            const btn = document.createElement('button');
            btn.className = 'carousel-download-btn';
            btn.textContent = `${i + 1}`;
            btn.title = `Download item ${i + 1}`;
            btn.addEventListener('click', () => downloadSingleItem(url, i));
            item.appendChild(btn);

            carouselGallery.appendChild(item);
        });
        carouselGallery.classList.remove('hide');
    }

    async function downloadSingleItem(mediaUrl, index) {
        try {
            const response = await fetch('/api/v1/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    original_url: urlInput.value,
                    url: mediaUrl,
                    headers: currentExtractionData.http_headers || {}
                })
            });

            if (!response.ok) {
                const errData = await response.text();
                throw new Error('Download failed: ' + errData);
            }

            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            const ext = isImageUrl(mediaUrl) ? 'jpg' : 'mp4';
            a.download = `AnyGrab_${Date.now()}_${index + 1}.${ext}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(downloadUrl);
        } catch (err) {
            alert(err.message);
        }
    }

    function isImageUrl(url) {
        return /\.(jpg|jpeg|png|webp)/i.test(url) || (!url.includes('.mp4') && !url.includes('/video/'));
    }

    downloadBtn.addEventListener('click', async () => {
        if (!currentExtractionData) return;

        downloadSpinner.classList.remove('hide');
        downloadBtn.disabled = true;

        const mediaUrls = currentExtractionData.media_urls;
        const isCarousel = mediaUrls.length > 1;

        if (isCarousel) {
            downloadText.textContent = `Downloading 0/${mediaUrls.length}...`;
            let completed = 0;
            for (const [i, mediaUrl] of mediaUrls.entries()) {
                try {
                    await downloadSingleItem(mediaUrl, i);
                    completed++;
                    downloadText.textContent = `Downloading ${completed}/${mediaUrls.length}...`;
                    if (i < mediaUrls.length - 1) {
                        await new Promise(r => setTimeout(r, 500));
                    }
                } catch (err) {
                    console.error(`Failed to download item ${i + 1}:`, err);
                }
            }
            downloadText.textContent = `Download All (${mediaUrls.length})`;
        } else {
            downloadText.textContent = 'Downloading...';
            try {
                const response = await fetch('/api/v1/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        original_url: urlInput.value,
                        url: mediaUrls[0],
                        headers: currentExtractionData.http_headers || {}
                    })
                });

                if (!response.ok) {
                    const errData = await response.text();
                    throw new Error('Download failed: ' + errData);
                }

                const blob = await response.blob();
                const downloadUrl = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = downloadUrl;
                const ext = isImageUrl(mediaUrls[0]) ? 'jpg' : 'mp4';
                a.download = `AnyGrab_${Date.now()}.${ext}`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(downloadUrl);
            } catch (err) {
                alert(err.message);
            }
            downloadText.textContent = 'Download High Quality';
        }

        downloadSpinner.classList.add('hide');
        downloadBtn.disabled = false;
    });
});
