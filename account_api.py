"""Google account, download library and least-privilege Drive upload routes."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, Field, field_validator

from account_store import AccountStore
from media_source import MAX_MEDIA_BYTES, downloaded_source, source_platform, validate_source_url

log = logging.getLogger('anygrab.accounts')
router = APIRouter(prefix='/api/v1')
CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '').strip()
CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '').strip()
REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', '').strip()
_key = os.getenv('ACCOUNT_ENCRYPTION_KEY', '').strip()
_parts = urlsplit(REDIRECT_URI)
APP_ORIGIN = os.getenv('ACCOUNT_ORIGIN', '').strip().rstrip('/') or f'{_parts.scheme}://{_parts.netloc}'
CONFIGURED = bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI and _key)
SESSION_COOKIE = 'anygrab_session'
STATE_COOKIE = 'anygrab_oauth_state'
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
try:
    store = AccountStore(os.getenv('ACCOUNT_DB_PATH', 'data/accounts.db'), _key or None)
except (ValueError, TypeError):
    CONFIGURED = False
    store = AccountStore(os.getenv('ACCOUNT_DB_PATH', 'data/accounts.db'))
    log.error('Account encryption key is invalid; Google sign-in disabled')
_download_slots = threading.BoundedSemaphore(2)
_artifact_slots = asyncio.Semaphore(2)
_account_tasks = set()


def require_user(request):
    user = store.get_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, 'Sign in with Google to use your library.')
    return user


def check_csrf(request, user):
    origin = request.headers.get('origin', '')
    token = request.headers.get('x-csrf-token', '')
    if origin != APP_ORIGIN or not secrets.compare_digest(token, user['csrf_token']):
        raise HTTPException(403, 'Refresh the page and try again.')


def _cookie_options():
    return {'httponly': True, 'secure': _parts.scheme == 'https', 'samesite': 'lax', 'path': '/'}


class HistoryRequest(BaseModel):
    source_url: str = Field(max_length=2048)
    title: str = Field(default='Untitled', max_length=300)
    platform: str = Field(default='', max_length=50)
    filename: str = Field(min_length=1, max_length=240)
    destination: str = Field(pattern='^(browser|folder)$')
    media_index: int = Field(default=0, ge=0, le=99)
    audio_only: bool = False

    _source = field_validator('source_url')(validate_source_url)

    @field_validator('filename')
    @classmethod
    def safe_filename(cls, value):
        if value in ('.', '..') or any(char in value for char in ('/', '\\', '\x00', '\r', '\n')):
            raise ValueError('Invalid filename.')
        return value


class DriveRequest(BaseModel):
    request_id: UUID | None = None
    source_url: str = Field(max_length=2048)
    title: str = Field(default='Untitled', max_length=300)
    media_index: int = Field(default=0, ge=0, le=99)
    audio_only: bool = False
    _source = field_validator('source_url')(validate_source_url)


class ArtifactRequest(BaseModel):
    request_id: UUID | None = None
    artifact: str = Field(pattern='^(transcript|study_notes)$')


@router.get('/account')
def account(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    user = store.get_session(request.cookies.get(SESSION_COOKIE))
    tokens = store.get_tokens(user['id']) if user else {}
    return {'configured': CONFIGURED, 'user': {key: user[key] for key in ('name', 'email')} if user else None,
        'csrf_token': user['csrf_token'] if user else None,
        'drive_connected': DRIVE_SCOPE in tokens.get('scope', '').split(),
        'transcription_available': os.getenv('TRANSCRIPTION_ENABLED', '').strip().lower() in ('1', 'true', 'yes') and importlib.util.find_spec('faster_whisper') is not None,
        'notes_available': bool(os.getenv('GEMINI_API_KEY', '').strip())}


@router.get('/auth/google')
def google_login(request: Request, drive: bool = False):
    if not CONFIGURED:
        raise HTTPException(503, 'Google sign-in has not been configured.')
    user = require_user(request) if drive else None
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Open sign-in from this app.')
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    state = store.create_oauth_state({'verifier': verifier, 'nonce': nonce, 'user_id': user['id'] if user else None})
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = {'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI, 'response_type': 'code',
        'scope': 'openid email profile' + (' ' + DRIVE_SCOPE if drive else ''),
        'state': state, 'nonce': nonce, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'include_granted_scopes': 'true', 'access_type': 'offline'}
    if drive:
        params = {**params, 'prompt': 'consent', 'login_hint': user['email']}
    response = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params), status_code=302)
    response.set_cookie(STATE_COOKIE, state, max_age=600, **_cookie_options())
    return response


async def _exchange_code(code, verifier):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post('https://oauth2.googleapis.com/token', data={
            'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'redirect_uri': REDIRECT_URI,
            'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier})
        response.raise_for_status()
        return response.json()


def _verify_identity(token):
    return id_token.verify_oauth2_token(token, GoogleRequest(), audience=CLIENT_ID)


@router.get('/auth/google/callback')
async def google_callback(request: Request, state: str = '', code: str = ''):
    response = RedirectResponse('/?auth=error', status_code=302)
    response.delete_cookie(STATE_COOKIE, **_cookie_options())
    if not CONFIGURED or not state or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE, '')):
        return response
    payload = store.pop_oauth_state(state)
    if not payload or not code:
        return response
    try:
        tokens = await _exchange_code(code, payload['verifier'])
        identity = await asyncio.to_thread(_verify_identity, tokens['id_token'])
        if not secrets.compare_digest(identity.get('nonce', ''), payload['nonce']):
            raise ValueError('Identity nonce mismatch')
        if identity.get('email_verified') is not True or not identity.get('sub') or not identity.get('email'):
            raise ValueError('Identity unavailable')
        current = store.get_session(request.cookies.get(SESSION_COOKIE))
        if payload.get('user_id') and (not current or current['id'] != payload['user_id'] or current['google_sub'] != identity['sub']):
            raise ValueError('Drive account changed')
        safe_tokens = {key: tokens[key] for key in ('access_token', 'refresh_token', 'scope', 'token_type') if key in tokens}
        safe_tokens = {**safe_tokens, 'expires_at': time.time() + int(tokens.get('expires_in', 3600))}
        user = store.upsert_user(identity['sub'], identity['email'], identity.get('name', identity['email'])[:300], safe_tokens)
        if current:
            store.delete_session(request.cookies[SESSION_COOKIE])
        session = store.create_session(user['id'])
        response = RedirectResponse('/?auth=success', status_code=302)
        response.delete_cookie(STATE_COOKIE, **_cookie_options())
        response.set_cookie(SESSION_COOKIE, session, max_age=7 * 86400, **_cookie_options())
    except Exception:
        log.warning('Google sign-in failed')
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.post('/auth/logout')
def logout(request: Request):
    user = require_user(request)
    check_csrf(request, user)
    store.delete_session(request.cookies[SESSION_COOKIE])
    from fastapi.responses import JSONResponse
    response = JSONResponse({'ok': True})
    response.delete_cookie(SESSION_COOKIE, **_cookie_options())
    return response


@router.get('/library')
def library(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'items': store.list_items(require_user(request)['id'])}


@router.post('/library')
def save_history(request: Request, data: HistoryRequest):
    user = require_user(request)
    check_csrf(request, user)
    return {'item': store.create_item(user['id'], data.model_dump())}


async def _drive_access_token(user):
    tokens = store.get_tokens(user['id'])
    if DRIVE_SCOPE not in tokens.get('scope', '').split():
        raise HTTPException(409, 'Connect Google Drive first.')
    if tokens.get('expires_at', 0) > time.time() + 60:
        return tokens['access_token']
    if not tokens.get('refresh_token'):
        raise HTTPException(409, 'Reconnect Google Drive to continue.')
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post('https://oauth2.googleapis.com/token', data={
            'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
            'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'})
        if response.status_code in (400, 401):
            store.set_tokens(user['id'], {**tokens, 'scope': '', 'refresh_token': ''})
            raise HTTPException(409, 'Reconnect Google Drive to continue.')
        response.raise_for_status()
        refreshed = response.json()
    updated = {**tokens, **{key: refreshed[key] for key in ('access_token', 'scope') if key in refreshed},
        'expires_at': time.time() + int(refreshed.get('expires_in', 3600))}
    store.set_tokens(user['id'], updated)
    return updated['access_token']


async def _upload_to_drive(user, path, filename, mime):
    path = Path(path)
    if not 0 < path.stat().st_size <= MAX_MEDIA_BYTES:
        raise ValueError('This file exceeds the upload limit.')
    token = await _drive_access_token(user)
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=30), follow_redirects=False) as client:
        response = await client.post('https://www.googleapis.com/upload/drive/v3/files',
            params={'uploadType': 'resumable', 'fields': 'id,webViewLink'},
            headers={'Authorization': 'Bearer ' + token, 'X-Upload-Content-Type': mime,
                'X-Upload-Content-Length': str(path.stat().st_size)}, json={'name': filename[:240]})
        response.raise_for_status()
        location = response.headers.get('location', '')
        parsed = urlsplit(location)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com' or parsed.port not in (None, 443):
            raise ValueError('Invalid Drive upload response.')
        async def chunks():
            with path.open('rb') as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
        uploaded = await client.put(location, content=chunks(), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': mime, 'Content-Length': str(path.stat().st_size)})
        uploaded.raise_for_status()
        result = uploaded.json()
        if not result.get('id'):
            raise ValueError('Drive did not confirm the upload.')
        return 'https://drive.google.com/file/d/' + str(result['id']) + '/view'


async def upload_to_drive(user, path, filename, mime):
    # Inactivity timeouts alone permit slow uploads to outlive the idempotency lease.
    async with asyncio.timeout(180):
        return await _upload_to_drive(user, path, filename, mime)


def _retain_account_task(task):
    _account_tasks.add(task)
    def finished(done):
        _account_tasks.discard(done)
        if not done.cancelled() and done.exception():
            log.warning('A background Google Drive save failed')
    task.add_done_callback(finished)
    return task


async def shutdown_accounts():
    if _account_tasks:
        await asyncio.gather(*list(_account_tasks), return_exceptions=True)


def _download_and_save(user, data, request_id=None, claim_token=None):
    if not _download_slots.acquire(timeout=1):
        if request_id:
            store.abandon_drive_request(user['id'], request_id, claim_token)
        raise HTTPException(429, 'Downloads are busy. Try again shortly.')
    try:
        with downloaded_source(data.source_url, data.media_index, data.audio_only) as path:
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            drive_url = asyncio.run(upload_to_drive(user, path, path.name, mime))
            item = store.create_item(user['id'], {**data.model_dump(exclude={'request_id'}), 'platform': source_platform(data.source_url),
                'filename': path.name, 'destination': 'drive', 'drive_url': drive_url}, request_id=request_id, claim_token=claim_token)
            return item
    except Exception:
        if request_id:
            store.abandon_drive_request(user['id'], request_id, claim_token)
        raise
    finally:
        _download_slots.release()


@router.post('/drive')
async def save_drive(request: Request, data: DriveRequest):
    user = require_user(request)
    check_csrf(request, user)
    if DRIVE_SCOPE not in store.get_tokens(user['id']).get('scope', '').split():
        raise HTTPException(409, 'Connect Google Drive first.')
    claim = _claim_request(user, data, 'media')
    if claim.get('status') == 'done':
        return claim['result']
    try:
        task = _retain_account_task(asyncio.create_task(asyncio.to_thread(
            _download_and_save, user, data, str(data.request_id) if data.request_id else None, claim.get('claim_token'))))
        return {'item': await asyncio.shield(task)}
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(422, 'This source could not be downloaded within the media limits.') from error
    except Exception as error:
        log.warning('Google Drive save failed')
        raise HTTPException(502, 'Google Drive could not save this file. Try again.') from error


def _claim_request(user, data, operation):
    if not data.request_id:
        return {'status': 'claimed'}
    payload = {'operation': operation, **data.model_dump(mode='json', exclude={'request_id'})}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    claim = store.claim_drive_request(user['id'], str(data.request_id), digest)
    if claim['status'] == 'busy':
        raise HTTPException(409, 'This Drive save is still running. Retry shortly to check its result.')
    if claim['status'] == 'mismatch':
        raise HTTPException(422, 'This save request was already used for a different file.')
    return claim


async def _upload_artifact_unlimited(user, content, data, claim):
    request_id = str(data.request_id) if data.request_id else None
    try:
        with tempfile.TemporaryDirectory(prefix='anygrab-artifact-') as directory:
            filename = data.artifact + '.md' if data.artifact == 'study_notes' else 'transcript.txt'
            path = Path(directory) / filename
            path.write_text(content, encoding='utf-8')
            url = await upload_to_drive(user, path, filename, 'text/markdown' if data.artifact == 'study_notes' else 'text/plain')
            result = {'drive_url': url}
            if request_id:
                store.finish_drive_request(user['id'], request_id, claim['claim_token'], result)
            return result
    except Exception:
        if request_id:
            store.abandon_drive_request(user['id'], request_id, claim['claim_token'])
        raise


async def _upload_artifact(user, content, data, claim):
    try:
        async with asyncio.timeout(15):
            await _artifact_slots.acquire()
    except TimeoutError:
        if data.request_id:
            store.abandon_drive_request(user['id'], str(data.request_id), claim['claim_token'])
        raise HTTPException(429, 'Drive saves are busy. Try again shortly.')
    try:
        return await _upload_artifact_unlimited(user, content, data, claim)
    finally:
        _artifact_slots.release()


@router.post('/library/{item_id}/drive')
async def save_artifact(request: Request, item_id: str, data: ArtifactRequest):
    user = require_user(request)
    check_csrf(request, user)
    item = store.get_item(user['id'], item_id)
    if not item:
        raise HTTPException(404, 'Library item not found.')
    content = item.get(data.artifact)
    if not content:
        raise HTTPException(409, 'Generate this study material first.')
    operation = 'artifact:' + item_id + ':' + hashlib.sha256(content.encode()).hexdigest()
    claim = _claim_request(user, data, operation)
    if claim.get('status') == 'done':
        return claim['result']
    try:
        task = _retain_account_task(asyncio.create_task(_upload_artifact(user, content, data, claim)))
        return await asyncio.shield(task)
    except HTTPException:
        raise
    except Exception as error:
        log.warning('Google Drive artifact save failed')
        raise HTTPException(502, 'Google Drive could not save this material. Try again.') from error
