const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
test.beforeEach(async ({ page }) => { await page.coverage.startJSCoverage({ resetOnNavigation: false }); });
test.afterEach(async ({ page }, testInfo) => {
    const coverage = await page.coverage.stopJSCoverage();
    await testInfo.attach('library-js-coverage', { body: JSON.stringify(coverage.filter(entry => entry.url.endsWith('/library.js'))), contentType: 'application/json' });
});

const source = 'https://www.youtube.com/watch?v=library-fixture';
const item = changes => ({ id: 'find-1', source_url: source, title: 'Learning in the rain', platform: 'youtube', filename: 'rain.mp4', destination: 'browser', created_at: '2026-10-13T07:00:00Z', drive_url: null, transcript: null, study_notes: null, learning_status: 'idle', learning_error: null, ...changes });
const account = changes => ({ configured: true, user: { name: 'Reader', email: 'reader@example.com' }, csrf_token: 'test-csrf', drive_connected: true, transcription_available: true, notes_available: true, ...changes });
async function setup(page, { profile = account(), items = [] } = {}) {
    await page.route('**/api/v1/account', route => route.fulfill({ json: profile }));
    await page.route('**/api/v1/library', route => route.fulfill({ json: { items } }));
    await page.route('**/api/v1/queue', route => route.fulfill({ json: { extractions: { available: 4 }, downloads: { available: 4 } } }));
    await page.route('**/api/v1/extract', route => route.fulfill({ json: { platform: 'youtube', metadata: { title: 'Learning in the rain' }, media_urls: ['/fixtures/video.mp4'] } }));
    await page.addInitScript(() => { Object.defineProperty(window, 'showDirectoryPicker', { value: undefined, configurable: true }); });
}
async function extract(page) {
    await page.locator('#urlInput').fill(source);
    await page.getByRole('button', { name: 'Find media', exact: true }).click();
}
test('Google account signs in and signs out with the session CSRF token', async ({ page }) => {
    await setup(page);
    let signedOut = false;
    await page.route('**/api/v1/auth/logout', route => {
        expect(route.request().headers()['x-csrf-token']).toBe('test-csrf');
        signedOut = true;
        return route.fulfill({ json: { success: true } });
    });
    await page.route('**/api/v1/account', route => route.fulfill({ json: account({ user: signedOut ? null : account().user, drive_connected: !signedOut }) }));
    await page.goto('/');
    await expect(page.locator('#accountName')).toHaveText('Reader');
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('link', { name: 'Sign in with Google', exact: true })).toHaveAttribute('href', '/api/v1/auth/google');
    await expect(page.locator('#libraryHint')).toContainText('Sign in');
});
test('history records delivered downloads only and preserves the extracted source', async ({ page }) => {
    await setup(page);
    let records = [];
    await page.route('**/api/v1/library', route => {
        if (route.request().method() === 'POST') {
            expect(route.request().headers()['x-csrf-token']).toBe('test-csrf');
            const data = route.request().postDataJSON();
            records = [item(data)];
            return route.fulfill({ json: { item: records[0] } });
        }
        return route.fulfill({ json: { items: records } });
    });
    await page.route('**/api/v1/download', route => route.fulfill({ status: 410, json: { detail: 'Media expired. Find media again.' } }));
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    await expect(page.locator('.toast')).toContainText('expired');
    expect(records).toHaveLength(0);
    await page.route('**/api/v1/download', route => route.fulfill({ contentType: 'video/mp4', body: 'media' }));
    await page.locator('#urlInput').fill('https://www.youtube.com/watch?v=other');
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    await downloaded;
    await expect(page.locator('#libraryList')).toContainText('Learning in the rain');
    expect(records[0]).toMatchObject({ source_url: source, destination: 'browser' });
    await page.getByRole('button', { name: 'Find again', exact: true }).click();
    await expect(page.locator('#urlInput')).toHaveValue(source);
});
test('Drive failure is not retried and offers reconnect without losing browser downloads', async ({ page }) => {
    await setup(page);
    let requests = 0;
    await page.route('**/api/v1/drive', route => {
        requests += 1;
        return route.fulfill({ status: 403, json: { detail: 'Google Drive permission expired. Reconnect Google Drive.' } });
    });
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect(page.locator('#driveStatus')).toContainText('permission expired');
    await expect(page.getByRole('link', { name: 'Reconnect Google Drive', exact: true })).toHaveAttribute('href', '/api/v1/auth/google?drive=true');
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeEnabled();
    expect(requests).toBe(1);
});
test('transcription and AI notes poll completion, export and save artifacts to Drive', async ({ page }) => {
    let current = item();
    await setup(page);
    await page.route('**/api/v1/library', route => route.fulfill({ json: { items: [current] } }));
    await page.route('**/api/v1/library/find-1/transcribe', route => {
        current = item({ learning_status: 'transcribing' });
        return route.fulfill({ json: { item: current } });
    });
    await page.route('**/api/v1/library/find-1/notes', route => {
        current = item({ transcript: 'Keep practising.', learning_status: 'summarizing' });
        return route.fulfill({ json: { item: current } });
    });
    await page.route('**/api/v1/library/find-1/drive', route => {
        expect(route.request().postDataJSON()).toMatchObject({ artifact: 'study_notes', request_id: expect.any(String) });
        return route.fulfill({ json: { drive_url: 'https://drive.google.com/file/d/notes/view' } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
    await expect(page.locator('#libraryList')).toContainText('Transcribing');
    current = item({ transcript: 'Keep practising.', learning_status: 'transcribed' });
    await expect(page.getByRole('button', { name: 'Write AI study notes', exact: true })).toBeEnabled({ timeout: 7000 });
    await page.getByRole('button', { name: 'Write AI study notes', exact: true }).click();
    await expect(page.locator('#libraryList')).toContainText('Writing study notes');
    current = item({ transcript: 'Keep practising.', study_notes: '# Practice\n\nTry recalling the lesson.', learning_status: 'ready' });
    await expect(page.getByText('Study notes', { exact: true })).toBeVisible({ timeout: 7000 });
    await page.getByText('Study notes', { exact: true }).click();
    await expect(page.getByRole('button', { name: 'Download notes (.md)', exact: true })).toBeVisible();
    const exported = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download notes (.md)', exact: true }).click();
    expect((await exported).suggestedFilename()).toMatch(/\.md$/);
    await page.getByRole('button', { name: 'Save notes to Drive', exact: true }).click();
    await expect(page.getByRole('link', { name: 'Open saved artifact in Drive', exact: true })).toHaveAttribute('href', 'https://drive.google.com/file/d/notes/view');
});
test('learning errors allow recovery and untrusted notes render as plain text', async ({ page }) => {
    await setup(page, { items: [item({ learning_status: 'error', learning_error: 'Transcription failed. Try again.', transcript: '<script>bad()</script>', study_notes: '<img src=x onerror=bad()>' })] });
    await page.goto('/');
    await expect(page.locator('#libraryList')).toContainText('Transcription failed');
    await expect(page.getByRole('button', { name: 'Transcribe', exact: true })).toBeEnabled();
    await page.getByText('Study notes', { exact: true }).click();
    await expect(page.locator('.artifact-text').last()).toHaveText('<img src=x onerror=bad()>');
    expect(await page.locator('#libraryList img, #libraryList script').count()).toBe(0);
});
test('unconfigured features explain setup while anonymous downloading remains usable', async ({ page }) => {
    await setup(page, { profile: account({ configured: false, user: null, transcription_available: false, notes_available: false }) });
    await page.goto('/');
    await expect(page.locator('#accountStatus')).toContainText('Google sign-in is not set up');
    await expect(page.locator('#learningHint')).toContainText('AI');
    await extract(page);
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeEnabled();
});
for (const width of [1440, 390, 320]) {
    test(`account and library fit ${width}px and have no accessibility violations`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await setup(page, { items: [item({ title: 'A long lesson about learning something new every single day', transcript: 'A transcript.', study_notes: '# Ideas\nKeep going.', learning_status: 'ready' })] });
        await page.goto('/');
        await expect(page.locator('#libraryList')).toContainText('long lesson');
        expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)).toBe(false);
        expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    });
}

test('anonymous saves stay usable and do not read or write private history', async ({ page }) => {
    await setup(page, { profile: account({ user: null, drive_connected: false, csrf_token: null }) });
    let libraryRequests = 0;
    await page.route('**/api/v1/library', route => { libraryRequests += 1; return route.fulfill({ status: 401, json: { detail: 'Sign in to view saved finds.' } }); });
    await page.route('**/api/v1/download', route => route.fulfill({ contentType: 'video/mp4', body: 'media' }));
    await page.route('**/api/v1/auth/google', route => route.fulfill({ contentType: 'text/html', body: 'Google sign-in' }));
    await page.goto('/');
    await expect(page.locator('#libraryHint')).toContainText('Sign in');
    await extract(page);
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    await downloaded;
    await expect(page.locator('.toast')).toContainText('Saved');
    expect(libraryRequests).toBe(0);
    await page.getByRole('button', { name: 'Sign in to save to Google Drive', exact: true }).click();
    await expect(page).toHaveURL(/\/api\/v1\/auth\/google$/);
});

test('signing out immediately clears private history even if the next history request fails', async ({ page }) => {
    await setup(page, { items: [item({ transcript: 'Private lesson', study_notes: 'Private notes' })] });
    let signedOut = false;
    await page.route('**/api/v1/account', route => route.fulfill({ json: account({ user: signedOut ? null : account().user }) }));
    await page.route('**/api/v1/auth/logout', route => { signedOut = true; return route.fulfill({ json: { success: true } }); });
    await page.goto('/');
    await expect(page.locator('#libraryList')).toContainText('Private lesson');
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.locator('#libraryList')).toBeEmpty();
    await expect(page.locator('#libraryHint')).toContainText('Sign in');
});

test('Drive saves the selected carousel item once with CSRF and adds it to history', async ({ page }) => {
    await setup(page);
    await page.route('**/api/v1/extract', route => route.fulfill({ json: { platform: 'instagram', metadata: { title: 'Rain album' }, media_urls: ['/fixtures/first.jpg', '/fixtures/second.jpg'] } }));
    await page.route('**/fixtures/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>' }));
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    let requests = 0;
    await page.route('**/api/v1/drive', async route => {
        requests += 1;
        expect(route.request().headers()['x-csrf-token']).toBe('test-csrf');
        expect(route.request().postDataJSON()).toMatchObject({ source_url: source, title: 'Rain album', media_index: 1, audio_only: false, request_id: expect.any(String) });
        await pending;
        return route.fulfill({ json: { item: item({ title: 'Rain album', destination: 'drive', drive_url: 'https://drive.google.com/file/d/rain/view' }) } });
    });
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save item 2 to Drive', exact: true }).click();
    try { await expect(page.getByRole('button', { name: 'Saving to Google Drive…', exact: true })).toBeDisabled(); }
    finally { finish(); }
    await expect(page.locator('#driveStatus')).toContainText('Saved to Google Drive');
    await expect(page.getByRole('link', { name: 'Open in Google Drive', exact: true })).toHaveAttribute('href', 'https://drive.google.com/file/d/rain/view');
    expect(requests).toBe(1);
});

test('history failures retain successful downloads and refresh can recover', async ({ page }) => {
    await setup(page);
    let refreshFails = true;
    await page.route('**/api/v1/library', route => {
        if (route.request().method() === 'POST') return route.fulfill({ status: 500, json: { detail: 'History unavailable' } });
        return refreshFails ? route.fulfill({ status: 503, json: { detail: 'History is temporarily unavailable. Refresh to try again.' } }) : route.fulfill({ json: { items: [item()] } });
    });
    await page.route('**/api/v1/download', route => route.fulfill({ contentType: 'video/mp4', body: 'media' }));
    await page.goto('/');
    await expect(page.locator('#libraryStatus')).toContainText('temporarily unavailable');
    await extract(page);
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save video', exact: true }).click();
    await downloaded;
    await expect(page.locator('#libraryStatus')).toContainText('Your file was saved');
    await expect(page.locator('.toast')).toContainText('Saved');
    refreshFails = false;
    await page.getByRole('button', { name: 'Refresh history', exact: true }).click();
    await expect(page.locator('#libraryList')).toContainText('Learning in the rain');
});

test('learning request errors show recovery, transcript exports and expired artifact permission reconnects', async ({ page }) => {
    await setup(page, { items: [item({ transcript: 'Practice recalling key ideas.', study_notes: 'Review your notes.', learning_status: 'transcribed', drive_url: 'javascript:alert(1)' })] });
    await page.route('**/api/v1/library/find-1/notes', route => route.fulfill({ status: 502, json: { detail: 'AI provider unavailable. Try again.' } }));
    await page.route('**/api/v1/library/find-1/drive', route => route.fulfill({ status: 409, json: { detail: 'Google Drive needs to be reconnected.' } }));
    await page.goto('/');
    await expect(page.getByRole('link', { name: 'Open in Google Drive', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Write AI study notes', exact: true }).click();
    await expect(page.locator('#libraryList')).toContainText('AI provider unavailable');
    await expect(page.getByRole('button', { name: 'Write AI study notes', exact: true })).toBeEnabled();
    await page.getByText('Transcript', { exact: true }).click();
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download transcript (.txt)', exact: true }).click();
    expect((await downloaded).suggestedFilename()).toMatch(/_transcript\.txt$/);
    await page.getByRole('button', { name: 'Save transcript to Drive', exact: true }).click();
    await expect(page.locator('#libraryList')).toContainText('needs to be reconnected');
    await expect(page.locator('#libraryList').getByRole('link', { name: 'Reconnect Google Drive', exact: true })).toHaveAttribute('href', '/api/v1/auth/google?drive=true');
});

test('account lookup and sign-out failures keep errors readable and downloading available', async ({ page }) => {
    await setup(page);
    await page.route('**/api/v1/auth/logout', route => route.fulfill({ status: 503, contentType: 'text/plain', body: 'offline' }));
    await page.goto('/');
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.locator('#accountStatus')).toContainText('Something went wrong');
    await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeEnabled();
    await page.route('**/api/v1/account', route => route.abort());
    await page.goto('/?auth=error');
    await expect(page.locator('#accountStatus')).toContainText('Downloads still work');
    await expect(page.locator('#libraryStatus')).toContainText('Google sign-in could not be completed');
    await extract(page);
    await expect(page.getByRole('button', { name: 'Save video', exact: true })).toBeEnabled();
});

test('a Drive response arriving after logout cannot restore private history or status', async ({ page }) => {
    await setup(page);
    let signedOut = false;
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    await page.route('**/api/v1/account', route => route.fulfill({ json: account({ user: signedOut ? null : account().user }) }));
    await page.route('**/api/v1/auth/logout', route => { signedOut = true; return route.fulfill({ json: { success: true } }); });
    await page.route('**/api/v1/drive', async route => { await pending; return route.fulfill({ json: { item: item({ title: 'Private delayed result', destination: 'drive' }) } }); });
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect(page.locator('#saveDriveBtn')).toHaveAttribute('aria-busy', 'true');
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.locator('#libraryHint')).toContainText('Sign in');
    finish();
    await expect(page.locator('#saveDriveBtn')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#libraryList')).toBeEmpty();
    await expect(page.locator('#driveStatus')).toBeEmpty();
});

test('retrying a failed Drive request reuses its request ID until a successful save', async ({ page }) => {
    await setup(page);
    let attempts = [];
    await page.route('**/api/v1/drive', route => {
        attempts = [...attempts, route.request().postDataJSON()];
        return attempts.length === 1
            ? route.fulfill({ status: 504, json: { detail: 'Drive save timed out. Try again.' } })
            : route.fulfill({ json: { item: item({ destination: 'drive' }) } });
    });
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect(page.locator('#driveStatus')).toContainText('timed out');
    expect(attempts).toHaveLength(1);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect(page.locator('#driveStatus')).toContainText('Saved to Google Drive');
    expect(attempts[0].request_id).toMatch(/^[a-f\d-]{36}$/);
    expect(attempts[1].request_id).toBe(attempts[0].request_id);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect.poll(() => attempts.length).toBe(3);
    expect(attempts[2].request_id).not.toBe(attempts[0].request_id);
});

test('photo history explains why speech transcription is unavailable', async ({ page }) => {
    await setup(page, { items: [item({ filename: 'rain.jpg', platform: 'instagram' })] });
    await page.goto('/');
    await expect(page.getByRole('button', { name: 'Transcribe', exact: true })).toBeDisabled();
    await expect(page.locator('#libraryList')).toContainText('Photos do not contain audio to transcribe.');
    await expect(page.getByRole('button', { name: 'Find again', exact: true })).toBeEnabled();
});

test('an in-progress Drive retry keeps its request ID without offering account reconnect', async ({ page }) => {
    await setup(page);
    let ids = [];
    await page.route('**/api/v1/drive', route => {
        ids = [...ids, route.request().postDataJSON().request_id];
        return route.fulfill({ status: 409, json: { detail: 'This Drive save is still running. Retry shortly to check its result.' } });
    });
    await page.goto('/');
    await extract(page);
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect(page.locator('#driveStatus')).toContainText('still running');
    await expect(page.locator('#driveReconnect')).toBeHidden();
    await page.getByRole('button', { name: 'Save to Google Drive', exact: true }).click();
    await expect.poll(() => ids.length).toBe(2);
    expect(ids[0]).toBe(ids[1]);
});

test('polling another learning job preserves reading focus and artifact scroll', async ({ page }) => {
    const ready = item({ id: 'ready-find', title: 'The lesson being read', study_notes: Array.from({ length: 70 }, (_, index) => `Study point ${index + 1}: recall this idea in your own words.`).join('\n'), transcript: 'Transcript to study.', learning_status: 'ready' });
    let currentReady = ready;
    let busy = item({ id: 'busy-find', title: 'Another lesson', learning_status: 'transcribing' });
    await setup(page);
    await page.route('**/api/v1/library', route => route.fulfill({ json: route.request().method() === 'POST'
        ? { item: item({ id: 'new-find', title: 'A newly saved lesson' }) }
        : { items: [currentReady, busy] } }));
    await page.clock.install();
    await page.goto('/');
    const readyRow = page.locator('.library-item').filter({ hasText: 'The lesson being read' });
    const summary = readyRow.locator('summary').filter({ hasText: 'Study notes' });
    await summary.click();
    const notes = readyRow.locator('.artifact-text').last();
    await notes.evaluate(node => { node.scrollTop = 250; });
    await expect(summary).toBeFocused();
    busy = { ...busy, transcript: 'Finished transcript.', learning_status: 'transcribed' };
    await page.clock.runFor(5000);
    await expect(page.locator('.library-item').filter({ hasText: 'Another lesson' }).getByRole('button', { name: 'Write AI study notes', exact: true })).toBeEnabled();
    await expect(summary).toBeFocused();
    expect(await notes.evaluate(node => node.scrollTop)).toBe(250);
    await page.evaluate(source_url => document.dispatchEvent(new CustomEvent('anygrab:saved', { detail: { source_url, title: 'A newly saved lesson', filename: 'new.mp4', platform: 'youtube', destination: 'browser' } })), source);
    await expect(page.locator('#libraryList')).toContainText('A newly saved lesson');
    await expect(summary).toBeFocused();
    expect(await notes.evaluate(node => node.scrollTop)).toBe(250);
    const exportButton = readyRow.getByRole('button', { name: 'Download notes (.md)', exact: true });
    await exportButton.focus();
    currentReady = { ...ready, drive_url: 'https://drive.google.com/file/d/updated/view' };
    await page.evaluate(() => document.getElementById('refreshLibraryBtn').click());
    await expect(readyRow.getByRole('link', { name: 'Open in Google Drive', exact: true })).toBeVisible();
    await expect(exportButton).toBeFocused();
    expect(await notes.evaluate(node => node.scrollTop)).toBe(250);
});
