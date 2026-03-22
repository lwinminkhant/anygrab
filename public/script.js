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

    let currentExtractionParams = null;

    extractBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if(!url) return;

        errorBox.classList.add('hide');
        resultCard.classList.add('hide');
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

            platformBadge.textContent = data.platform;
            mediaTitle.textContent = data.metadata.title || data.caption || 'Untitled Video';
            mediaThumbnail.src = data.metadata.thumbnail || 'https://via.placeholder.com/600x400?text=No+Thumbnail';
            
            const views = data.metadata.view_count;
            const likes = data.metadata.like_count;
            mediaViews.textContent = `👁 ${views ? views.toLocaleString() : 'N/A'}`;
            mediaLikes.textContent = `❤️ ${likes ? likes.toLocaleString() : 'N/A'}`;

            currentExtractionParams = {
                media_url: data.media_urls[0],
                http_headers: data.http_headers || {}
            };

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

    downloadBtn.addEventListener('click', async () => {
        if (!currentExtractionParams) return;

        downloadSpinner.classList.remove('hide');
        downloadText.textContent = 'Downloading...';
        downloadBtn.disabled = true;

        try {
            const response = await fetch('/api/v1/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    original_url: urlInput.value,
                    url: currentExtractionParams.media_url,
                    headers: currentExtractionParams.http_headers || {}
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
            
            const ext = currentExtractionParams.media_url.includes('.jpg') || currentExtractionParams.media_url.includes('.webp') ? 'jpg' : 'mp4';
            a.download = `AnyGrab_${Date.now()}.${ext}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(downloadUrl);

        } catch (err) {
            alert(err.message);
        } finally {
            downloadSpinner.classList.add('hide');
            downloadText.textContent = 'Download High Quality';
            downloadBtn.disabled = false;
        }
    });
});
