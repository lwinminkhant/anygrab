"""Exercise the full API flow with real sessions and mocked external providers."""
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import account_api
import learning_api
from account_store import AccountStore
from main import app


def wait_for(client, item_id, expected):
    for _ in range(100):
        items = client.get('/api/v1/library').json()['items']
        item = next(item for item in items if item['id'] == item_id)
        if item['learning_status'] == expected:
            return item
        time.sleep(0.01)
    raise AssertionError(f'Learning job did not reach {expected}')


def test_sign_in_save_transcribe_notes_drive_and_sign_out(tmp_path, monkeypatch):
    storage = AccountStore(tmp_path / 'accounts.db', Fernet.generate_key())
    monkeypatch.setattr(account_api, 'store', storage)
    monkeypatch.setattr(learning_api, 'store', storage)
    monkeypatch.setattr(account_api, 'CONFIGURED', True)
    monkeypatch.setattr(account_api, 'APP_ORIGIN', 'http://localhost')
    monkeypatch.setattr(learning_api, 'transcription_configured', lambda: True)
    monkeypatch.setattr(learning_api, 'notes_configured', lambda: True)
    monkeypatch.setattr(learning_api, '_transcribe', lambda item: '[00:00] Spaced practice improves recall.')
    monkeypatch.setattr(learning_api, 'generate_study_notes', lambda transcript, title: '# Overview\nPractise over time.')
    monkeypatch.setattr(account_api, 'upload_to_drive', AsyncMock(return_value='https://drive.google.com/file/d/mock-file/view'))

    with TestClient(app, base_url='http://localhost') as client:
        authorization = client.get('/api/v1/auth/google', follow_redirects=False)
        params = parse_qs(urlsplit(authorization.headers['location']).query)
        assert account_api.DRIVE_SCOPE not in params['scope'][0]
        assert params['code_challenge_method'] == ['S256']
        monkeypatch.setattr(account_api, '_exchange_code', AsyncMock(return_value={
            'id_token': 'provider-fixture', 'access_token': 'fixture-access',
            'scope': 'openid email profile', 'expires_in': 3600,
        }))
        monkeypatch.setattr(account_api, '_verify_identity', lambda token: {
            'sub': 'flow-user', 'email': 'reader@example.test', 'email_verified': True,
            'name': 'Reader', 'nonce': params['nonce'][0],
        })
        callback = client.get('/api/v1/auth/google/callback', params={
            'state': params['state'][0], 'code': 'fixture-code',
        }, follow_redirects=False)
        assert callback.headers['location'] == '/?auth=success'
        assert 'HttpOnly' in callback.headers['set-cookie']
        profile = client.get('/api/v1/account').json()
        assert profile['user']['email'] == 'reader@example.test'
        headers = {'Origin': 'http://localhost', 'X-CSRF-Token': profile['csrf_token']}
        saved = client.post('/api/v1/library', headers=headers, json={
            'source_url': 'https://www.youtube.com/watch?v=fixture', 'title': 'Practice',
            'platform': 'youtube', 'filename': 'practice.mp4', 'destination': 'browser',
        }).json()['item']
        item_id = saved['id']
        assert client.post(f'/api/v1/library/{item_id}/transcribe', headers=headers).status_code == 202
        assert 'recall' in wait_for(client, item_id, 'transcribed')['transcript']
        assert client.post(f'/api/v1/library/{item_id}/notes', headers=headers).status_code == 202
        assert 'Practise' in wait_for(client, item_id, 'ready')['study_notes']
        exported = client.post(f'/api/v1/library/{item_id}/drive', headers=headers, json={'artifact': 'study_notes'})
        assert exported.json()['drive_url'].endswith('/mock-file/view')
        account_api.upload_to_drive.assert_awaited_once()
        assert client.post('/api/v1/auth/logout', headers=headers).status_code == 200
        assert client.get('/api/v1/library').status_code == 401
        assert client.get('/api/v1/account').json()['user'] is None
