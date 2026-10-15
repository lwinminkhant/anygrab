const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const VIDEO_LINK = 'https://www.youtube.com/watch?v=local-fixture';
const THUMBNAIL = '/fixtures/thumbnail.svg';
const VIDEO = '/fixtures/video.mp4';
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400"><rect width="640" height="400" fill="#4a4a4a"/><path d="M260 130 400 200 260 270Z" fill="#e8c547"/></svg>';

const media = (overrides = {}) => ({
    platform: 'youtube',
    media_urls: [VIDEO],
    metadata: {
        title: 'A quiet walk through Bangkok',
        thumbnail: THUMBNAIL,
        view_count: 128500,
        like_count: 8300,
    },
    http_headers: { Referer: 'https://www.youtube.com/' },
    ...overrides,
});

const availableQueue = {
    extractions: { active: 0, max: 4, available: 4 },
    downloads: { active: 0, max: 4, available: 4 },
};

async function preparePage(page, { extraction = media(), queue = availableQueue } = {}) {
    // API routes are the integration seam. No social platform is contacted.
    await page.route('**/api/v1/queue', route => route.fulfill({ json: queue }));
    await page.route('**/api/v1/extract', route => route.fulfill({ json: extraction }));
    await page.route('**/fixtures/**', route => route.fulfill({
        contentType: 'image/svg+xml',
        body: SVG,
    }));
    await page.route('https://fonts.googleapis.com/**', route => route.abort());
    await page.route('https://fonts.gstatic.com/**', route => route.abort());
    // Exercise browser downloads deterministically, independent of native folder support.
    await page.addInitScript(() => {
        if (!window.folderFixture) {
            Object.defineProperty(window, 'showDirectoryPicker', { value: undefined, configurable: true });
        }
    });
}

async function findMedia(page, link = VIDEO_LINK) {
    await page.locator('#urlInput').fill(link);
    await page.getByRole('button', { name: 'Find media', exact: true }).click();
    await expect(page.locator('#resultCard')).toBeVisible();
}

async function enableFolderPicker(page, options = {}) {
    await page.addInitScript(({ restored = false, permission = 'granted', writeFailure = false }) => {
        window.folderFixture = { permission, writeFailure, prompts: 0, files: [], aborted: false };
        const record = changes => { window.folderFixture = { ...window.folderFixture, ...changes }; };
        const handle = {
            name: 'Media archive',
            queryPermission: async () => window.folderFixture.permission,
            requestPermission: async () => {
                const renewedPermission = window.folderFixture.permission === 'prompt'
                    ? 'granted' : window.folderFixture.permission;
                record({ prompts: window.folderFixture.prompts + 1, permission: renewedPermission });
                return renewedPermission;
            },
            getFileHandle: async filename => {
                let chunks = [];
                return {
                    createWritable: async () => new WritableStream({
                        write: chunk => {
                            if (window.folderFixture.writeFailure) throw new Error('Disk is full. Choose another folder.');
                            chunks = [...chunks, chunk];
                        },
                        close: () => record({ files: [...window.folderFixture.files, {
                            name: filename, size: new Blob(chunks).size,
                        }] }),
                        abort: () => record({ aborted: true }),
                    }),
                    getFile: async () => new File(chunks, filename),
                };
            },
        };
        let rememberedHandle = restored ? handle : null;
        const requestFor = result => {
            const request = { result };
            queueMicrotask(() => request.onsuccess?.());
            return request;
        };
        const store = {
            get: () => requestFor(rememberedHandle),
            put: value => { rememberedHandle = value; return requestFor(); },
            delete: () => { rememberedHandle = null; return requestFor(); },
        };
        const database = {
            objectStoreNames: { contains: () => false },
            createObjectStore: () => store,
            transaction: () => ({ objectStore: () => store }),
            close: () => {},
        };
        Object.defineProperty(window, 'indexedDB', { configurable: true, value: {
            open: () => {
                const request = { result: database };
                queueMicrotask(() => { request.onupgradeneeded?.(); request.onsuccess?.(); });
                return request;
            },
        } });
        Object.defineProperty(window, 'showDirectoryPicker', {
            configurable: true, value: async () => handle,
        });
    }, options);
}

test.beforeEach(async ({ page, browserName }) => {
    if (browserName === 'chromium') {
        await page.coverage.startJSCoverage({ resetOnNavigation: false });
    }
});

test.afterEach(async ({ page, browserName }, testInfo) => {
    if (browserName !== 'chromium' || page.isClosed()) return;
    const entries = await page.coverage.stopJSCoverage();
    // Aggregate the union of these ranges across tests to assess executed UI code.
    // This deliberately reports coverage; it does not imply backend/folder-path coverage.
    await testInfo.attach('frontend-js-coverage', {
        body: JSON.stringify(entries.filter(entry => /\/script\.js(?:\?|$)/.test(entry.url))),
        contentType: 'application/json',
    });
});

test('empty and non-HTTP links show guidance without contacting extraction', async ({ page }) => {
    await preparePage(page);
    let requests = [];
    page.on('request', request => {
        if (request.url().endsWith('/api/v1/extract')) requests = [...requests, request];
    });
    await page.goto('/');
    const submit = page.getByRole('button', { name: 'Find media', exact: true });
    await submit.click();
    await expect(page.locator('#errorBox')).toBeVisible();
    await expect(page.locator('#errorBox')).toContainText(/paste|enter|link/i);
    await page.locator('#urlInput').fill('javascript:alert(1)');
    await submit.click();
    await expect(page.locator('#errorBox')).toBeVisible();
    await expect(page.locator('#errorBox')).toContainText(/valid|https?|link/i);
    await expect(submit).toBeEnabled();
    expect(requests).toHaveLength(0);
});

test('Enter submits the native form and presents extracted media metadata', async ({ page }) => {
    await preparePage(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Keep the good stuff.' })).toBeVisible();
    await expect(page.locator('#extractForm')).toHaveJSProperty('tagName', 'FORM');
    const requestPromise = page.waitForRequest('**/api/v1/extract');
    await page.locator('#urlInput').fill(`  ${VIDEO_LINK}  `);
    await page.locator('#urlInput').press('Enter');
    const request = await requestPromise;
    expect(request.method()).toBe('POST');
    expect(request.postDataJSON()).toEqual({ url: VIDEO_LINK });
    await expect(page.locator('#mediaTitle')).toHaveText('A quiet walk through Bangkok');
    await expect(page.locator('#platformBadge')).toContainText(/youtube/i);
    await expect(page.locator('#mediaViews')).toContainText('128,500');
    await expect(page.locator('#mediaLikes')).toContainText('8,300');
    const accessibleStats = await page.locator('.stats').ariaSnapshot();
    expect(accessibleStats).toMatch(/views/i);
    expect(accessibleStats).toMatch(/likes/i);
    await expect(page.locator('#mediaThumbnail')).toHaveAttribute('src', THUMBNAIL);
    await expect(page.locator('#audioToggleWrapper')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeVisible();
    await expect(page.locator('#errorBox')).toBeHidden();
});

test('loading prevents duplicate requests and restores the find action', async ({ page }) => {
    await preparePage(page);
    let finishExtraction;
    const releaseExtraction = new Promise(resolve => { finishExtraction = resolve; });
    await page.route('**/api/v1/extract', async route => {
        await releaseExtraction;
        await route.fulfill({ json: media() });
    });
    await page.goto('/');
    await page.locator('#urlInput').fill(VIDEO_LINK);
    await page.getByRole('button', { name: 'Find media', exact: true }).click();
    try {
        const busyButton = page.getByRole('button', { name: 'Finding media…', exact: true });
        await expect(busyButton).toBeDisabled();
        await expect(page.locator('#resultCard')).toBeHidden();
    } finally {
        finishExtraction();
    }
    await expect(page.locator('#resultCard')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Find media', exact: true })).toBeEnabled();
});

test('keyboard audio selection sends an audio request and saves the server format', async ({ page }) => {
    await preparePage(page);
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'audio/mp4',
        headers: { 'Content-Disposition': 'attachment; filename="walk.m4a"' },
        body: 'local audio fixture',
    }));
    await page.goto('/');
    await findMedia(page);
    const audioToggle = page.getByRole('checkbox', { name: /audio/i });
    await page.locator('#urlInput').focus();
    for (let tab = 0; tab < 12; tab += 1) {
        await page.keyboard.press('Tab');
        if (await audioToggle.evaluate(element => element === document.activeElement)) break;
    }
    await expect(audioToggle).toBeFocused();
    await page.keyboard.press('Space');
    await expect(audioToggle).toBeChecked();
    const audioButton = page.getByRole('button', { name: 'Save audio (MP3)', exact: true });
    await expect(audioButton).toBeVisible();
    const requestPromise = page.waitForRequest('**/api/v1/download');
    const downloadPromise = page.waitForEvent('download');
    await audioButton.click();
    const request = await requestPromise;
    expect(request.postDataJSON()).toEqual({
        original_url: VIDEO_LINK,
        url: VIDEO,
        headers: { Referer: 'https://www.youtube.com/' },
        audio_only: true,
    });
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.m4a$/);
    expect(await download.failure()).toBeNull();
    await expect(audioButton).toBeEnabled();
    await expect(page.locator('.toast')).toContainText(/saved/i);
    await audioToggle.uncheck();
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeVisible();
});

test('mobile carousel supports individually named saves and save all', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const images = ['/fixtures/first.jpg', '/fixtures/second.jpg'];
    await preparePage(page, { extraction: media({ platform: 'instagram', media_urls: images }) });
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'image/jpeg',
        headers: { 'Content-Disposition': 'attachment; filename="image.jpg"' },
        body: 'local image fixture',
    }));
    await page.goto('/');
    await findMedia(page, 'https://www.instagram.com/p/local-fixture/');
    await expect(page.locator('#audioToggleWrapper')).toBeHidden();
    const first = page.getByRole('button', { name: 'Save item 1', exact: true });
    const second = page.getByRole('button', { name: 'Save item 2', exact: true });
    await expect(first).toBeVisible();
    await expect(second).toBeVisible();
    const bounds = await first.boundingBox();
    expect(bounds.width).toBeGreaterThanOrEqual(44);
    expect(bounds.height).toBeGreaterThanOrEqual(44);
    const individualRequest = page.waitForRequest('**/api/v1/download');
    const individualDownload = page.waitForEvent('download');
    await first.click();
    expect((await individualRequest).postDataJSON().url).toBe(images[0]);
    await individualDownload;
    let downloadedItems = [];
    const recordRequest = request => {
        if (request.url().endsWith('/api/v1/download')) {
            downloadedItems = [...downloadedItems, request.postDataJSON().url];
        }
    };
    page.on('request', recordRequest);
    const all = page.getByRole('button', { name: 'Save all (2)', exact: true });
    await all.click();
    await expect(page.locator('.toast')).toContainText('Saved 2/2');
    await expect(all).toBeEnabled();
    expect([...downloadedItems].sort()).toEqual([...images].sort());
    page.off('request', recordRequest);
});

test('extraction errors recover when a new valid link is submitted', async ({ page }) => {
    await preparePage(page);
    await page.route('**/api/v1/extract', route => route.fulfill({
        status: 400,
        json: { detail: 'This link is private. Try a public video link.' },
    }));
    await page.goto('/');
    await page.locator('#urlInput').fill(VIDEO_LINK);
    await page.getByRole('button', { name: 'Find media', exact: true }).click();
    await expect(page.locator('#errorBox')).toHaveText('This link is private. Try a public video link.');
    await expect(page.locator('#resultCard')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Find media', exact: true })).toBeEnabled();
    await page.route('**/api/v1/extract', route => route.fulfill({ json: media() }));
    await findMedia(page);
    await expect(page.locator('#errorBox')).toBeHidden();
    await expect(page.locator('#mediaTitle')).toBeVisible();
});

test('download failure gives feedback and permits retry', async ({ page }) => {
    await preparePage(page);
    await page.route('**/api/v1/download', route => route.fulfill({
        status: 403,
        json: { detail: 'Media expired. Find media again.' },
    }));
    await page.goto('/');
    await findMedia(page);
    const save = page.getByRole('button', { name: 'Save video', exact: true });
    await save.click();
    await expect(page.locator('.toast')).toContainText('Media expired. Find media again.');
    await expect(save).toBeEnabled();
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'video/mp4',
        headers: { 'Content-Disposition': 'attachment; filename="walk.mp4"' },
        body: 'local video fixture',
    }));
    const downloadPromise = page.waitForEvent('download');
    await save.click();
    expect((await downloadPromise).suggestedFilename()).toMatch(/\.mp4$/);
    await expect(page.locator('.toast')).toContainText(/saved/i);
    await expect(save).toBeEnabled();
});

test('saving reports progress and prevents duplicate download actions', async ({ page }) => {
    await preparePage(page);
    let finishDownload;
    const releaseDownload = new Promise(resolve => { finishDownload = resolve; });
    await page.route('**/api/v1/download', async route => {
        await releaseDownload;
        await route.fulfill({ contentType: 'video/mp4', body: 'local video fixture' });
    });
    await page.goto('/');
    await findMedia(page);
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    try {
        await expect(page.getByRole('button', { name: 'Saving…', exact: true })).toBeDisabled();
    } finally {
        finishDownload();
    }
    await downloadPromise;
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeEnabled();
});

test('a chosen folder receives the file and can be cleared for browser downloads', async ({ page }) => {
    await preparePage(page);
    await enableFolderPicker(page);
    let finishDownload;
    const releaseDownload = new Promise(resolve => { finishDownload = resolve; });
    await page.route('**/api/v1/download', async route => {
        await releaseDownload;
        await route.fulfill({
            contentType: 'video/mp4', body: 'local video fixture',
            headers: { 'Content-Disposition': 'attachment; filename="walk.mp4"' },
        });
    });
    await page.goto('/');
    await expect(page.locator('#chooseFolderBtn')).toBeEnabled();
    await page.locator('#chooseFolderBtn').click();
    await expect(page.locator('#saveFolderName')).toHaveText('Media archive');
    await expect(page.locator('#clearFolderBtn')).toBeVisible();
    await findMedia(page);
    const save = page.getByRole('button', { name: 'Save video', exact: true });
    await save.click();
    try {
        await expect(page.locator('#chooseFolderBtn')).toBeDisabled();
        await expect(page.locator('#clearFolderBtn')).toBeDisabled();
    } finally {
        finishDownload();
    }
    await expect(page.locator('.toast')).toContainText(/saved/i);
    await expect(page.locator('#chooseFolderBtn')).toBeEnabled();
    await expect(page.locator('#clearFolderBtn')).toBeEnabled();
    const files = await page.evaluate(() => window.folderFixture.files);
    expect(files).toHaveLength(1);
    expect(files[0]).toEqual({ name: expect.stringMatching(/\.mp4$/), size: 19 });
    await page.locator('#clearFolderBtn').click();
    await expect(page.locator('#clearFolderBtn')).toBeHidden();
    await expect(page.locator('#saveFolderName')).toContainText(/choose folder|browser downloads/i);
    const downloadPromise = page.waitForEvent('download');
    await save.click();
    expect((await downloadPromise).suggestedFilename()).toMatch(/\.mp4$/);
});

test('editing the link after a preview preserves the extracted source when saving', async ({ page }) => {
    await preparePage(page);
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'video/mp4', body: 'local video fixture',
    }));
    await page.goto('/');
    await findMedia(page);
    await page.locator('#urlInput').fill('https://www.youtube.com/watch?v=another-video');
    const requestPromise = page.waitForRequest('**/api/v1/download');
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    expect((await requestPromise).postDataJSON()).toMatchObject({
        original_url: VIDEO_LINK, url: VIDEO,
    });
    await downloadPromise;
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeEnabled();
});

test('a remembered folder requests renewed permission only when saving', async ({ page }) => {
    await preparePage(page);
    await enableFolderPicker(page, { restored: true, permission: 'prompt' });
    let finishDownload;
    const releaseDownload = new Promise(resolve => { finishDownload = resolve; });
    await page.route('**/api/v1/download', async route => {
        await releaseDownload;
        await route.fulfill({ contentType: 'video/mp4', body: 'local video fixture' });
    });
    await page.goto('/');
    await expect(page.locator('#saveFolderName')).toContainText('Media archive');
    await expect(page.locator('#saveFolderName')).toContainText(/allow|permission/i);
    expect(await page.evaluate(() => window.folderFixture.prompts)).toBe(0);
    await findMedia(page);
    const requestPromise = page.waitForRequest('**/api/v1/download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    try {
        await requestPromise;
        expect(await page.evaluate(() => window.folderFixture.prompts)).toBe(1);
        expect(await page.evaluate(() => window.folderFixture.files)).toHaveLength(0);
    } finally {
        finishDownload();
    }
    await expect(page.locator('.toast')).toContainText(/saved/i);
    expect(await page.evaluate(() => window.folderFixture.prompts)).toBe(1);
    expect(await page.evaluate(() => window.folderFixture.files.length)).toBe(1);
});

test('denied folder permission leaves browser downloads available', async ({ page }) => {
    await preparePage(page);
    await enableFolderPicker(page, { permission: 'denied' });
    await page.goto('/');
    await page.locator('#chooseFolderBtn').click();
    await expect(page.locator('.toast')).toContainText(/permission.*denied/i);
    await expect(page.locator('#clearFolderBtn')).toBeHidden();
    await expect(page.locator('#saveFolderName')).toContainText(/choose folder|browser downloads/i);
});

test('a folder write error is shown and a later save can recover', async ({ page }) => {
    await preparePage(page);
    await enableFolderPicker(page, { writeFailure: true });
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'video/mp4', body: 'local video fixture',
    }));
    await page.goto('/');
    await page.locator('#chooseFolderBtn').click();
    await expect(page.locator('#saveFolderName')).toHaveText('Media archive');
    await findMedia(page);
    const save = page.getByRole('button', { name: 'Save video', exact: true });
    await save.click();
    await expect(page.locator('.toast')).toContainText('Disk is full. Choose another folder.');
    await expect(save).toBeEnabled();
    await page.evaluate(() => { window.folderFixture = { ...window.folderFixture, writeFailure: false }; });
    await save.click();
    await expect(page.locator('.toast')).toContainText(/saved/i);
    expect(await page.evaluate(() => window.folderFixture.files.length)).toBe(1);
});

test('video without artwork has a local fallback and keeps zero counts visible', async ({ page }) => {
    await preparePage(page, { extraction: media({ metadata: { view_count: 0, like_count: 0 } }) });
    await page.goto('/');
    await findMedia(page);
    await expect(page.locator('#mediaTitle')).toContainText(/untitled/i);
    await expect(page.locator('#mediaViews')).toContainText('0');
    await expect(page.locator('#mediaLikes')).toContainText('0');
    await expect.poll(() => page.locator('#mediaThumbnail').evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
    const thumbnail = await page.locator('#mediaThumbnail').getAttribute('src');
    expect(thumbnail).not.toMatch(/^https?:\/\//);
});

test('a single photo offers a photo save and uses its caption when the title is missing', async ({ page }) => {
    await preparePage(page, { extraction: media({
        platform: 'instagram', media_urls: ['/fixtures/first.jpg'],
        metadata: {}, caption: 'Rain on the river at dusk',
    }) });
    await page.route('**/api/v1/download', route => route.fulfill({
        contentType: 'image/jpeg', body: 'local photo fixture',
    }));
    await page.goto('/');
    await findMedia(page);
    await expect(page.locator('#mediaTitle')).toHaveText('Rain on the river at dusk');
    await expect(page.locator('#audioToggleWrapper')).toBeHidden();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save photo', exact: true }).click();
    expect((await downloadPromise).suggestedFilename()).toMatch(/\.jpg$/);
    await expect(page.getByRole('button', { name: 'Save photo', exact: true })).toBeEnabled();
});

test('malformed extraction data shows usable guidance instead of a broken result', async ({ page }) => {
    await preparePage(page, { extraction: { platform: 'youtube', metadata: null, media_urls: [] } });
    await page.goto('/');
    await page.locator('#urlInput').fill(VIDEO_LINK);
    await page.getByRole('button', { name: 'Find media', exact: true }).click();
    await expect(page.locator('#errorBox')).toBeVisible();
    await expect(page.locator('#errorBox')).toContainText(/media|valid|available|find/i);
    await expect(page.locator('#errorBox')).not.toContainText(/TypeError|Cannot read/i);
    await expect(page.locator('#resultCard')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Find media', exact: true })).toBeEnabled();
});

for (const status of [429, 504]) {
    test(`a temporary ${status} response retries and completes extraction`, async ({ page }) => {
        await preparePage(page);
        await page.clock.install();
        let attempts = 0;
        await page.route('**/api/v1/extract', route => {
            attempts += 1;
            return attempts === 1
                ? route.fulfill({ status, headers: { 'Retry-After': '1' }, json: { detail: 'Try again shortly.' } })
                : route.fulfill({ json: media() });
        });
        await page.goto('/');
        await page.locator('#urlInput').fill(VIDEO_LINK);
        await page.getByRole('button', { name: 'Find media', exact: true }).click();
        await expect(page.locator('.toast')).toContainText(/retry/i);
        await page.clock.runFor(1600);
        await expect(page.locator('#resultCard')).toBeVisible();
        await expect(page.getByRole('button', { name: 'Find media', exact: true })).toBeEnabled();
        expect(attempts).toBe(2);
    });
}

test('server capacity is described while requests are queued', async ({ page }) => {
    await preparePage(page, {
        queue: {
            extractions: { active: 4, max: 4, available: 0 },
            downloads: { active: 2, max: 4, available: 2 },
        },
    });
    await page.goto('/');
    await expect(page.locator('#queueIndicator')).toBeVisible({ timeout: 7000 });
    await expect(page.locator('#queueIndicator')).toContainText(/4\s*\/\s*4/);
    await expect(page.locator('#queueIndicator')).toContainText(/2\s*\/\s*4/);
    await expect(page.locator('#queueIndicator')).toContainText(/extractions/i);
    await expect(page.locator('#queueIndicator')).toContainText(/downloads/i);
});

test('the initial page and media result have no automated accessibility violations', async ({ page }) => {
    await preparePage(page);
    // Scan the settled UI: transition opacity is not a persistent contrast state.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/');
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await findMedia(page);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

for (const width of [1440, 768, 390, 320]) {
    test(`the form and populated result fit a ${width}px viewport`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await preparePage(page, { extraction: media({
            platform: 'instagram',
            media_urls: ['/fixtures/first.jpg', '/fixtures/second.jpg', '/fixtures/third.jpg'],
            metadata: {
                title: 'A long title should wrap without widening the screen or hiding the save controls',
                thumbnail: THUMBNAIL,
                view_count: 128500,
                like_count: 8300,
            },
        }) });
        await page.goto('/');
        const horizontalOverflow = () => page.evaluate(() =>
            document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
        expect(await horizontalOverflow()).toBe(false);
        await findMedia(page);
        expect(await horizontalOverflow()).toBe(false);
        await expect(page.getByRole('button', { name: 'Save all (3)', exact: true })).toBeVisible();
    });
}
