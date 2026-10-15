document.addEventListener('DOMContentLoaded', () => {
    const byId = id => document.getElementById(id);
    const list = byId('libraryList');
    const driveButton = byId('saveDriveBtn');
    let account = null;
    let items = [];
    let media = null;
    let pollTimer = null;
    let refreshPending = null;
    let historyVersion = 0;
    let busyKeys = new Set();
    let operationErrors = {};
    let artifactLinks = {};
    let driveAttempts = {};
    let renderedRows = new Map();
    let reconnectDrive = false;
    const isLearning = item => ['transcribing', 'summarizing'].includes(item.learning_status);

    function element(tag, className, content) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (content != null) node.textContent = content;
        return node;
    }

    function currentSession(version, owner) {
        return version === historyVersion && Boolean(account?.user) && account.user.email === owner;
    }

    function attemptId(key) {
        if (!driveAttempts[key]) driveAttempts = { ...driveAttempts, [key]: crypto.randomUUID() };
        return driveAttempts[key];
    }

    function finishAttempt(key) {
        driveAttempts = Object.fromEntries(Object.entries(driveAttempts).filter(([existing]) => existing !== key));
    }

    function status(id, message, error = false) {
        const node = byId(id);
        node.textContent = message;
        node.classList.toggle('is-error', error);
    }

    async function request(path, body) {
        const response = await fetch(`/api/v1${path}`, {
            credentials: 'same-origin',
            ...(body === undefined ? {} : {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': account?.csrf_token || '' },
                body: JSON.stringify(body),
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong. Try again.');
            error.status = response.status;
            throw error;
        }
        return data;
    }

    function renderAccount() {
        const signedIn = Boolean(account?.user);
        byId('accountName').textContent = account?.user?.name || account?.user?.email || '';
        byId('accountName').classList.toggle('hide', !signedIn);
        byId('signOutBtn').classList.toggle('hide', !signedIn);
        byId('googleSignIn').classList.toggle('hide', signedIn || !account?.configured);
        status('accountStatus', account ? (account.configured ? '' : 'Google sign-in is not set up yet.') : 'Account unavailable. Downloads still work.');
        byId('libraryHint').textContent = signedIn
            ? 'Your downloads, transcripts, and study notes, saved to this account.'
            : 'Sign in with Google to save download history, transcripts, and study notes with your account.';
        const notices = [];
        if (account && !account.transcription_available) notices.push('Transcription is not set up yet.');
        if (account && !account.notes_available) notices.push('AI study notes are not set up yet.');
        if (account?.transcription_available && account?.notes_available) notices.push('Transcribe a saved video or audio, then turn the transcript into AI study notes.');
        byId('learningHint').textContent = notices.join(' ');
        renderDrive();
    }

    async function loadAccount() {
        try { account = await request('/account'); }
        catch { account = null; }
        renderAccount();
    }

    function needsDriveConnection(error) {
        return [403, 409].includes(error.status) && /reconnect|connect.*Google Drive|Google Drive.*(?:permission|connection|connected|expired)/i.test(error.message);
    }

    async function checkExpiredSession(error) {
        if (error.status !== 401) return;
        await loadAccount();
        if (!account?.user) {
            historyVersion += 1;
            items = [];
            renderLibrary();
        }
    }

    function driveLabel() {
        if (!account?.configured) return 'Google Drive unavailable';
        if (!account.user) return 'Sign in to save to Google Drive';
        if (!account.drive_connected || reconnectDrive) return 'Connect Google Drive';
        return media?.media_count > 1 ? 'Save first item to Google Drive' : 'Save to Google Drive';
    }

    function renderDrive() {
        driveButton.textContent = busyKeys.has('drive') ? 'Saving to Google Drive…' : driveLabel();
        driveButton.disabled = !media || !account?.configured || busyKeys.has('drive');
        driveButton.setAttribute('aria-busy', String(busyKeys.has('drive')));
        byId('driveReconnect').classList.toggle('hide', !reconnectDrive);
        byId('driveReconnect').href = account?.user ? '/api/v1/auth/google?drive=true' : '/api/v1/auth/google';
        document.querySelectorAll('.carousel-drive-btn').forEach(button => {
            button.disabled = !media || !account?.configured || busyKeys.has('drive');
        });
    }

    function connectDrive() { window.location.assign(account?.user ? '/api/v1/auth/google?drive=true' : '/api/v1/auth/google'); }

    async function saveDrive(index) {
        if (!media || !account?.configured || busyKeys.has('drive')) return;
        if (!account.user || !account.drive_connected || reconnectDrive) return connectDrive();
        const version = historyVersion;
        const owner = account.user.email;
        const payload = { source_url: media.source_url, title: media.title.slice(0, 300), media_index: index, audio_only: byId('audioOnlyToggle').checked };
        const attempt = JSON.stringify(payload);
        const requestId = attemptId(attempt);
        busyKeys = new Set([...busyKeys, 'drive']);
        renderDrive();
        status('driveStatus', 'Saving to Google Drive…');
        try {
            const data = await request('/drive', { ...payload, request_id: requestId });
            if (!currentSession(version, owner)) return;
            finishAttempt(attempt);
            if (data.item) upsert(data.item, version, owner);
            status('driveStatus', 'Saved to Google Drive. Find it in your saved finds below.');
        } catch (error) {
            if (!currentSession(version, owner)) return;
            status('driveStatus', error.message, true);
            reconnectDrive = needsDriveConnection(error);
            await checkExpiredSession(error);
        } finally {
            busyKeys = new Set([...busyKeys].filter(key => key !== 'drive'));
            renderDrive();
        }
    }

    function upsert(item, version, owner) {
        if (!currentSession(version, owner)) return;
        items = [item, ...items.filter(existing => existing.id !== item.id)];
        renderLibrary();
        schedulePoll();
    }

    function schedulePoll() {
        clearTimeout(pollTimer);
        if (items.some(isLearning)) pollTimer = setTimeout(refreshLibrary, document.hidden ? 30_000 : 5000);
    }

    async function refreshLibrary() {
        if (!account?.user) {
            items = [];
            renderLibrary();
            return;
        }
        if (refreshPending) return refreshPending;
        const version = historyVersion;
        refreshPending = (async () => {
            try {
                const data = await request('/library');
                if (!Array.isArray(data.items)) throw new Error('History could not be loaded. Try refreshing.');
                if (version !== historyVersion || !account?.user) return;
                items = data.items;
                renderLibrary();
                status('libraryStatus', '');
            } catch (error) {
                if (version === historyVersion && account?.user) status('libraryStatus', error.message, true);
            } finally {
                refreshPending = null;
                schedulePoll();
            }
        })();
        return refreshPending;
    }

    function action(label, callback, disabled = false, className = 'secondary-button') {
        const button = element('button', className, label);
        button.type = 'button';
        button.disabled = disabled;
        button.addEventListener('click', callback);
        return button;
    }

    function driveLink(url, label) {
        try {
            const parsed = new URL(url);
            if (parsed.protocol !== 'https:' || parsed.hostname !== 'drive.google.com') return null;
            const link = element('a', '', label);
            link.href = parsed.href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            return link;
        } catch { return null; }
    }

    async function learn(item, kind) {
        const version = historyVersion;
        const owner = account?.user?.email;
        if (busyKeys.has(item.id) || isLearning(item)) return;
        busyKeys = new Set([...busyKeys, item.id]);
        operationErrors = { ...operationErrors, [item.id]: '' };
        renderLibrary();
        try {
            const data = await request(`/library/${encodeURIComponent(item.id)}/${kind}`, {});
            if (data.item) upsert(data.item, version, owner);
        } catch (error) {
            if (currentSession(version, owner)) operationErrors = { ...operationErrors, [item.id]: error.message };
        }
        finally {
            busyKeys = new Set([...busyKeys].filter(key => key !== item.id));
            renderLibrary();
        }
    }

    function exportArtifact(item, kind) {
        const notes = kind === 'study_notes';
        const blob = new Blob([item[kind]], { type: notes ? 'text/markdown;charset=utf-8' : 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const anchor = element('a');
        anchor.href = url;
        const name = (item.title || 'AnyGrab').replace(/[^\p{L}\p{N}_-]+/gu, '_').slice(0, 80) || 'AnyGrab';
        anchor.download = `${name}_${notes ? 'notes.md' : 'transcript.txt'}`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
    }

    async function saveArtifact(item, kind) {
        const version = historyVersion;
        const owner = account?.user?.email;
        if (!account?.user || !account.drive_connected || reconnectDrive) return connectDrive();
        const key = `${item.id}:${kind}`;
        if (busyKeys.has(key)) return;
        busyKeys = new Set([...busyKeys, key]);
        operationErrors = { ...operationErrors, [item.id]: '' };
        renderLibrary();
        try {
            const data = await request(`/library/${encodeURIComponent(item.id)}/drive`, { artifact: kind, request_id: attemptId(key) });
            if (!currentSession(version, owner)) return;
            finishAttempt(key);
            artifactLinks = { ...artifactLinks, [key]: data.drive_url };
        } catch (error) {
            if (!currentSession(version, owner)) return;
            operationErrors = { ...operationErrors, [item.id]: error.message };
            reconnectDrive = needsDriveConnection(error);
            await checkExpiredSession(error);
            renderDrive();
        } finally {
            busyKeys = new Set([...busyKeys].filter(value => value !== key));
            renderLibrary();
        }
    }

    function artifact(item, kind, openStates) {
        const notes = kind === 'study_notes';
        const key = `${item.id}:${kind}`;
        const details = element('details', 'artifact-details');
        details.dataset.artifactKey = key;
        details.open = openStates.has(key);
        const summary = element('summary', '', notes ? 'Study notes' : 'Transcript');
        summary.dataset.focusKey = `summary-${kind}`;
        details.append(summary);
        details.append(element('pre', 'artifact-text', item[kind]));
        const actions = element('div', 'artifact-actions');
        const download = action(notes ? 'Download notes (.md)' : 'Download transcript (.txt)', () => exportArtifact(item, kind), false, 'text-button');
        download.dataset.focusKey = `export-${kind}`;
        actions.append(download);
        const label = busyKeys.has(key) ? 'Saving to Drive…' : (reconnectDrive ? 'Reconnect Google Drive' : `Save ${notes ? 'notes' : 'transcript'} to Drive`);
        const save = action(label, () => saveArtifact(item, kind), !account?.configured || busyKeys.has(key), 'text-button');
        save.dataset.focusKey = `drive-${kind}`;
        actions.append(save);
        const link = driveLink(artifactLinks[key], 'Open saved artifact in Drive');
        if (link) {
            link.dataset.focusKey = `open-${kind}`;
            actions.append(link);
        }
        details.append(actions);
        return details;
    }

    function libraryRow(item, openStates) {
        const row = element('article', 'library-item');
        row.dataset.itemId = item.id;
        row.append(element('h3', '', item.title || 'Untitled media'));
        const meta = element('p', 'library-meta');
        const date = new Date(item.created_at);
        const time = element('time', '', Number.isNaN(date.getTime()) ? 'Saved' : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }));
        if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
        meta.append(element('span', '', item.platform || 'Media'), time, element('span', '', ({ browser: 'Browser download', folder: 'Chosen folder', drive: 'Google Drive' })[item.destination] || 'Saved'));
        row.append(meta);
        const actions = element('div', 'library-actions');
        actions.append(action('Find again', () => {
            byId('urlInput').value = item.source_url;
            byId('extractForm').requestSubmit();
        }, byId('extractBtn').disabled, 'text-button'));
        const link = driveLink(item.drive_url, 'Open in Google Drive');
        if (link) actions.append(link);
        const busy = isLearning(item) || busyKeys.has(item.id);
        const photo = /\.(jpe?g|png|webp|gif|avif|heic|svg)$/i.test(item.filename || '');
        actions.append(action('Transcribe', () => learn(item, 'transcribe'), !account?.transcription_available || busy || photo));
        actions.append(action('Write AI study notes', () => learn(item, 'notes'), !account?.notes_available || !item.transcript || busy));
        row.append(actions);
        if (photo) row.append(element('p', 'action-status', 'Photos do not contain audio to transcribe.'));
        if (isLearning(item)) row.append(element('p', 'learning-state', item.learning_status === 'transcribing' ? 'Transcribing… You can keep using AnyGrab.' : 'Writing study notes… You can keep using AnyGrab.'));
        const error = operationErrors[item.id] || item.learning_error;
        if (error) {
            const message = element('p', 'learning-error', error);
            message.setAttribute('role', 'alert');
            row.append(message);
            if (reconnectDrive) {
                const reconnect = element('a', 'recovery-link', 'Reconnect Google Drive');
                reconnect.href = '/api/v1/auth/google?drive=true';
                reconnect.dataset.focusKey = 'reconnect';
                row.append(reconnect);
            }
        }
        if (item.transcript) row.append(artifact(item, 'transcript', openStates));
        if (item.study_notes) row.append(artifact(item, 'study_notes', openStates));
        actions.querySelectorAll('button, a').forEach(node => { node.dataset.focusKey = node.tagName === 'A' ? 'open-drive' : node.textContent; });
        return row;
    }

    function rowSignature(item) {
        return JSON.stringify([
            item, operationErrors[item.id], reconnectDrive,
            [item.id, `${item.id}:transcript`, `${item.id}:study_notes`].map(key => busyKeys.has(key)),
            artifactLinks[`${item.id}:transcript`], artifactLinks[`${item.id}:study_notes`],
            account?.configured, account?.transcription_available, account?.notes_available,
            byId('extractBtn').disabled,
        ]);
    }

    function renderLibrary() {
        const active = document.activeElement;
        const focusedRow = active?.closest('.library-item');
        const focusKey = active?.dataset.focusKey;
        const openStates = new Set([...list.querySelectorAll('details[open]')].map(node => node.dataset.artifactKey));
        const scrollStates = new Map([...list.querySelectorAll('.artifact-text')].map(node => [node.parentElement.dataset.artifactKey, node.scrollTop]));
        const nextRows = new Map(items.map(item => {
            const signature = rowSignature(item);
            const previous = renderedRows.get(item.id);
            const row = previous?.signature === signature ? previous.row : libraryRow(item, openStates);
            return [item.id, { row, signature }];
        }));
        const retained = new Set([...nextRows.values()].map(value => value.row));
        [...list.children].filter(row => !retained.has(row)).forEach(row => row.remove());
        [...nextRows.values()].forEach(({ row }, index) => {
            if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
        });
        renderedRows = nextRows;
        list.querySelectorAll('.artifact-text').forEach(node => {
            const scroll = scrollStates.get(node.parentElement.dataset.artifactKey);
            if (scroll !== undefined) node.scrollTop = scroll;
        });
        if (focusedRow && focusKey && document.activeElement !== active) {
            const row = nextRows.get(focusedRow.dataset.itemId)?.row;
            const target = [...(row?.querySelectorAll('[data-focus-key]') || [])].find(node => node.dataset.focusKey === focusKey);
            if (target && !target.disabled) target.focus({ preventScroll: true });
        }
        byId('libraryEmpty').classList.toggle('hide', items.length > 0);
        byId('libraryEmpty').textContent = account?.user
            ? 'Your next good find starts with a link. Saved downloads will appear here.'
            : 'Your downloads are ready to keep. Sign in to start your saved history and make study notes.';
    }

    document.addEventListener('anygrab:media', event => {
        media = event.detail;
        status('driveStatus', '');
        renderDrive();
        document.querySelectorAll('.carousel-drive-btn').forEach(button => button.remove());
        if (media?.media_count > 1) document.querySelectorAll('.carousel-item').forEach((row, index) => {
            row.append(action(`Save item ${index + 1} to Drive`, () => saveDrive(index), !account?.configured, 'secondary-button carousel-drive-btn'));
        });
    });
    document.addEventListener('anygrab:saved', async event => {
        const version = historyVersion;
        try {
            if (!account) await loadAccount();
            if (!account?.user) return;
            const owner = account.user.email;
            const data = await request('/library', { ...event.detail, title: event.detail.title.slice(0, 300) });
            if (data.item) upsert(data.item, version, owner);
        } catch {
            if (version === historyVersion && account?.user) status('libraryStatus', 'Your file was saved, but history could not be updated. Refresh history to check.', true);
        }
    });
    driveButton.addEventListener('click', () => saveDrive(0));
    byId('refreshLibraryBtn').addEventListener('click', refreshLibrary);
    byId('signOutBtn').addEventListener('click', async () => {
        byId('signOutBtn').disabled = true;
        try {
            await request('/auth/logout', {});
            historyVersion += 1;
            account = { ...account, user: null, drive_connected: false, csrf_token: null };
            renderAccount();
            items = [];
            clearTimeout(pollTimer);
            renderLibrary();
            artifactLinks = {};
            driveAttempts = {};
            status('driveStatus', '');
            operationErrors = {};
            await loadAccount();
            await refreshLibrary();
        } catch (error) { status('accountStatus', error.message, true); }
        finally { byId('signOutBtn').disabled = false; }
    });
    const parameters = new URLSearchParams(window.location.search);
    (async () => {
        await loadAccount();
        await refreshLibrary();
        if (parameters.get('auth') === 'error') status('libraryStatus', 'Google sign-in could not be completed. Try signing in again.', true);
    })();
});
