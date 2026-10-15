"""Authenticated, CSRF-protected learning jobs for saved media."""

import asyncio
import logging
import weakref
from functools import partial

from fastapi import APIRouter, HTTPException, Request

from account_api import check_csrf, require_user, store
from media_source import downloaded_source
from study_notes import (
    LearningConfigurationError, configured_int, generate_study_notes,
    notes_configured, transcribe_audio, transcription_configured, validate_notes_input,
)

router = APIRouter(prefix='/api/v1/library')
log = logging.getLogger('anygrab.learning')
_jobs = set()
_gates = weakref.WeakKeyDictionary()


def _owned_item(request: Request, item_id: str):
    user = require_user(request)
    check_csrf(request, user)
    item = store.get_item(user['id'], item_id)
    if item is None:
        raise HTTPException(404, 'Saved download not found.')
    return user, item


def _transcribe(item: dict) -> str:
    with downloaded_source(item['source_url'], media_index=item.get('media_index', 0), audio_only=True) as path:
        return transcribe_audio(path)


async def _finish_thread(callback):
    worker = asyncio.create_task(asyncio.to_thread(callback))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling to_thread cannot stop its native work. Finish before publishing
        # the final state so a retry cannot race a still-running thread.
        return await worker


async def _renew_lease(user_id: str, item: dict):
    while True:
        try:
            if not store.renew_learning(user_id, item['id'], item['learning_claim']):
                return
        except Exception as error:
            log.warning('Learning lease renewal failed (%s)', type(error).__name__)
        await asyncio.sleep(60)


async def _run_job(user_id: str, item: dict, operation: str, gate):
    token = item['learning_claim']
    heartbeat = asyncio.create_task(_renew_lease(user_id, item))
    try:
        async with gate:
            if not store.renew_learning(user_id, item['id'], token):
                raise RuntimeError('Learning job lease was revoked')
            callback = partial(_transcribe, item) if operation == 'transcribe' else partial(
                generate_study_notes, item['transcript'], item.get('title', ''),
            )
            result = await _finish_thread(callback)
        status = 'transcribed' if operation == 'transcribe' else 'ready'
        field = 'transcript' if operation == 'transcribe' else 'study_notes'
        # A regenerated transcript invalidates notes derived from its old version.
        artifacts = {field: result, **({'study_notes': ''} if operation == 'transcribe' else {})}
        store.update_learning(user_id, item['id'], status, claim_token=token, **artifacts)
    except asyncio.CancelledError:
        store.update_learning(user_id, item['id'], 'error', claim_token=token,
            error='This learning job was interrupted. Please retry.')
        raise
    except Exception as error:
        # Provider error bodies may include sensitive URLs or credentials.
        log.warning('Learning job %s failed (%s)', operation, type(error).__name__)
        message = 'Transcription failed. Please retry.' if operation == 'transcribe' else 'Study notes could not be generated. Please retry.'
        store.update_learning(user_id, item['id'], 'error', claim_token=token, error=message)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def _job_done(task):
    _jobs.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        log.warning('Learning job state could not be saved (%s)', type(error).__name__)


def _start_job(user: dict, item: dict, operation: str):
    try:
        workers = configured_int('LEARNING_WORKERS', 2, 8)
        pending = configured_int('LEARNING_MAX_PENDING', 8, 64)
    except LearningConfigurationError as error:
        raise HTTPException(503, str(error)) from error
    if len(_jobs) >= pending:
        raise HTTPException(429, 'Learning jobs are busy. Please retry shortly.', headers={'Retry-After': '30'})
    status = 'transcribing' if operation == 'transcribe' else 'summarizing'
    claimed = store.claim_learning(user['id'], item['id'], status)
    if claimed is None:
        raise HTTPException(409, 'This download already has a learning job in progress.')
    loop = asyncio.get_running_loop()
    if loop not in _gates:
        _gates[loop] = asyncio.BoundedSemaphore(workers)
    task = asyncio.create_task(_run_job(user['id'], claimed, operation, _gates[loop]))
    _jobs.add(task)
    task.add_done_callback(_job_done)
    return {'item': {key: value for key, value in claimed.items() if key != 'learning_claim'}}


@router.post('/{item_id}/transcribe', status_code=202)
async def transcribe(request: Request, item_id: str):
    user, item = _owned_item(request, item_id)
    if not transcription_configured():
        raise HTTPException(503, 'Transcription is not configured on this server.')
    return _start_job(user, item, 'transcribe')


@router.post('/{item_id}/notes', status_code=202)
async def notes(request: Request, item_id: str):
    user, item = _owned_item(request, item_id)
    if not notes_configured():
        raise HTTPException(503, 'AI study notes require GEMINI_API_KEY on the server.')
    if not item.get('transcript'):
        raise HTTPException(409, 'Create a transcript before generating study notes.')
    try:
        validate_notes_input(item['transcript'])
    except LearningConfigurationError as error:
        raise HTTPException(503, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return _start_job(user, item, 'notes')


async def shutdown_learning():
    """Drain accepted jobs during the application's graceful shutdown."""
    if _jobs:
        await asyncio.gather(*tuple(_jobs), return_exceptions=True)
