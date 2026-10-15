import asyncio
from contextlib import contextmanager
import sys
import time
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import media_source

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import account_api
from account_store import AccountStore


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(str(Path(self.temp.name) / 'accounts.db'), Fernet.generate_key())
        self.user = self.store.upsert_user('google-a', 'a@example.com', 'Alice', {'access_token': 'secret', 'scope': account_api.DRIVE_SCOPE})
        self.token = self.store.create_session(self.user['id'])
        self.patch = patch.object(account_api, 'store', self.store)
        self.patch.start()
        self.origin = patch.object(account_api, 'APP_ORIGIN', 'http://testserver')
        self.origin.start()
        app = FastAPI()
        app.include_router(account_api.router)
        self.client = TestClient(app)
        self.client.cookies.set(account_api.SESSION_COOKIE, self.token)
        self.csrf = self.store.get_session(self.token)['csrf_token']
        self.headers = {'Origin': 'http://testserver', 'X-CSRF-Token': self.csrf}

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.origin.stop()
        self.temp.cleanup()

    def data(self):
        return {'source_url': 'https://www.youtube.com/watch?v=abc123', 'title': 'Lesson', 'platform': 'YouTube', 'filename': 'lesson.mp4', 'destination': 'browser'}

    def test_history_requires_session_and_csrf(self):
        self.assertEqual(self.client.post('/api/v1/library', json=self.data()).status_code, 403)
        for headers in [{'Origin': 'https://evil.com', 'X-CSRF-Token': self.csrf}, {'Origin': 'http://testserver', 'X-CSRF-Token': 'wrong'}, {'X-CSRF-Token': self.csrf}]:
            self.assertEqual(self.client.post('/api/v1/library', json=self.data(), headers=headers).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/v1/library').status_code, 401)

    def test_history_and_owner_isolation(self):
        response = self.client.post('/api/v1/library', json=self.data(), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.json()['item']
        self.assertEqual(self.client.get('/api/v1/library').json()['items'][0]['id'], item['id'])
        other = self.store.upsert_user('google-b', 'b@example.com', 'Bob', {})
        self.assertEqual(self.store.list_items(other['id']), [])
        self.assertIsNone(self.store.get_item(other['id'], item['id']))
        self.store.update_learning(other['id'], item['id'], 'complete', transcript='stolen')
        self.assertIsNone(self.store.get_item(self.user['id'], item['id'])['transcript'])

    def test_history_validation(self):
        for url in ['http://127.0.0.1/a', 'https://evil.com/x', 'https://youtube.com.evil.com/a', 'https://user:pass@youtube.com/a']:
            response = self.client.post('/api/v1/library', json={**self.data(), 'source_url': url}, headers=self.headers)
            self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/v1/library', json={**self.data(), 'filename': '../secret'}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_session_expiry_and_logout(self):
        expired = self.store.create_session(self.user['id'], lifetime=-1)
        self.assertIsNone(self.store.get_session(expired))
        self.assertEqual(self.client.post('/api/v1/auth/logout', headers=self.headers).status_code, 200)
        self.assertIsNone(self.store.get_session(self.token))

    def test_tokens_encrypted_and_account_private(self):
        self.assertNotIn(b'secret', Path(self.store.path).read_bytes())
        response = self.client.get('/api/v1/account').json()
        self.assertEqual(response['user'], {'name': 'Alice', 'email': 'a@example.com'})
        self.assertNotIn('access_token', str(response))
        self.assertTrue(response['drive_connected'])

    def test_oauth_invalid_state_rejected(self):
        with patch.object(account_api, 'CONFIGURED', True):
            response = self.client.get('/api/v1/auth/google/callback?state=bad&code=bad', follow_redirects=False)
        self.assertEqual(response.headers['location'], '/?auth=error')

    def test_drive_failure_does_not_record(self):
        with patch.object(account_api, '_download_and_save', side_effect=RuntimeError('provider secret')):
            response = self.client.post('/api/v1/drive', json={'source_url': self.data()['source_url'], 'title': 'Lesson'}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('provider secret', response.text)
        self.assertEqual(self.store.list_items(self.user['id']), [])

    def oauth_start(self, drive=False):
        response = self.client.get('/api/v1/auth/google' + ('?drive=true' if drive else ''), follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        values = parse_qs(urlsplit(response.headers['location']).query)
        return values['state'][0], values['nonce'][0]

    def test_oauth_success_pkce_and_replay(self):
        with patch.object(account_api, 'CONFIGURED', True):
            state, nonce = self.oauth_start()
            with patch.object(account_api, '_exchange_code', new=AsyncMock(return_value={'id_token': 'verified', 'access_token': 'new-token', 'scope': 'openid email profile'})) as exchange:
                with patch.object(account_api, '_verify_identity', return_value={'sub': 'google-a', 'email': 'a@example.com', 'email_verified': True, 'name': 'Alice', 'nonce': nonce}):
                    response = self.client.get('/api/v1/auth/google/callback', params={'state': state, 'code': 'code'}, follow_redirects=False)
            self.assertEqual(response.headers['location'], '/?auth=success')
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            self.assertGreater(len(exchange.call_args.args[1]), 40)
            self.assertIsNone(self.store.get_session(self.token))
            replay = self.client.get('/api/v1/auth/google/callback', params={'state': state, 'code': 'code'}, follow_redirects=False)
            self.assertEqual(replay.headers['location'], '/?auth=error')

    def test_oauth_nonce_and_linked_identity_rejection(self):
        for bad_nonce, subject in [(True, 'google-a'), (False, 'google-b')]:
            with patch.object(account_api, 'CONFIGURED', True):
                state, nonce = self.oauth_start(drive=True)
                with patch.object(account_api, '_exchange_code', new=AsyncMock(return_value={'id_token': 'verified'})):
                    with patch.object(account_api, '_verify_identity', return_value={'sub': subject, 'email': 'a@example.com', 'email_verified': True, 'nonce': 'wrong' if bad_nonce else nonce}):
                        response = self.client.get('/api/v1/auth/google/callback', params={'state': state, 'code': 'code'}, follow_redirects=False)
            self.assertEqual(response.headers['location'], '/?auth=error')
            self.assertIsNotNone(self.store.get_session(self.token))

    def test_oauth_disabled_and_expired_state(self):
        with patch.object(account_api, 'CONFIGURED', False):
            self.assertEqual(self.client.get('/api/v1/auth/google').status_code, 503)
        state = self.store.create_oauth_state({'nonce': 'n'})
        with self.store.connect() as db:
            db.execute('UPDATE oauth_states SET expires=0')
        self.assertIsNone(self.store.pop_oauth_state(state))
        self.assertIsNone(self.store.pop_oauth_state(state))

    def test_learning_claim_expiry_fencing_and_artifact_preservation(self):
        item = self.store.create_item(self.user['id'], self.data())
        first = self.store.claim_learning(self.user['id'], item['id'], 'queued', lease_seconds=-1)
        second = self.store.claim_learning(self.user['id'], item['id'], 'queued')
        self.assertIsNone(self.store.claim_learning(self.user['id'], item['id'], 'queued'))
        self.store.update_learning(self.user['id'], item['id'], 'complete', transcript='old', claim_token=first['learning_claim'])
        self.assertIsNone(self.store.get_item(self.user['id'], item['id'])['transcript'])
        self.store.update_learning(self.user['id'], item['id'], 'complete', transcript='fresh', error='old', claim_token=second['learning_claim'])
        self.store.update_learning(self.user['id'], item['id'], 'complete', study_notes='notes')
        updated = self.store.get_item(self.user['id'], item['id'])
        self.assertEqual(updated['transcript'], 'fresh')
        self.assertIsNone(updated['learning_error'])
        self.store.claim_learning(self.user['id'], item['id'], 'queued', lease_seconds=-1)
        self.assertEqual(self.store.list_items(self.user['id'])[0]['learning_status'], 'error')

    def test_artifact_upload_owner_missing_and_success(self):
        item = self.store.create_item(self.user['id'], self.data())
        endpoint = '/api/v1/library/' + item['id'] + '/drive'
        self.assertEqual(self.client.post(endpoint, json={'artifact': 'transcript'}, headers=self.headers).status_code, 409)
        self.store.update_learning(self.user['id'], item['id'], 'complete', transcript='hello')
        with patch.object(account_api, 'upload_to_drive', new=AsyncMock(return_value='https://drive.google.com/file/d/id/view')) as upload:
            response = self.client.post(endpoint, json={'artifact': 'transcript'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(upload.call_args.args[1].exists())
        self.assertEqual(self.client.post('/api/v1/library/missing/drive', json={'artifact': 'transcript'}, headers=self.headers).status_code, 404)

    def test_drive_success_and_not_connected(self):
        path = Path(self.temp.name) / 'video.mp4'
        path.write_bytes(b'video')
        @contextmanager
        def source(*args):
            yield path
        with patch.object(account_api, 'downloaded_source', source):
            with patch.object(account_api, 'upload_to_drive', new=AsyncMock(return_value='https://drive.google.com/file/d/id/view')):
                response = self.client.post('/api/v1/drive', json={'source_url': self.data()['source_url'], 'title': 'Lesson', 'media_index': 2, 'audio_only': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['item']['media_index'], 2)
        self.assertTrue(response.json()['item']['audio_only'])
        self.store.set_tokens(self.user['id'], {'scope': ''})
        self.assertEqual(self.client.post('/api/v1/drive', json={'source_url': self.data()['source_url']}, headers=self.headers).status_code, 409)

    def test_drive_provider_refresh_and_resumable_stream(self):
        self.store.set_tokens(self.user['id'], {'access_token': 'old', 'refresh_token': 'refresh', 'scope': account_api.DRIVE_SCOPE, 'expires_at': 0})
        path = Path(self.temp.name) / 'video.mp4'
        path.write_bytes(b'video')
        requests = []
        def handle(request):
            requests.append(request)
            if request.url.host == 'oauth2.googleapis.com':
                return httpx.Response(200, json={'access_token': 'fresh', 'expires_in': 3600})
            if request.method == 'POST':
                return httpx.Response(200, headers={'Location': 'https://www.googleapis.com/upload/drive/v3/files?upload_id=one'})
            self.assertEqual(request.content, b'video')
            self.assertEqual(request.headers['authorization'], 'Bearer fresh')
            return httpx.Response(200, json={'id': 'file-one'})
        client_class = httpx.AsyncClient
        with patch.object(account_api.httpx, 'AsyncClient', side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs)):
            url = asyncio.run(account_api.upload_to_drive(self.user, path, path.name, 'video/mp4'))
        self.assertEqual(url, 'https://drive.google.com/file/d/file-one/view')
        self.assertEqual(self.store.get_tokens(self.user['id'])['access_token'], 'fresh')
        self.assertEqual(len(requests), 3)

    def test_drive_refresh_revoked(self):
        self.store.set_tokens(self.user['id'], {'refresh_token': 'revoked', 'scope': account_api.DRIVE_SCOPE})
        client_class = httpx.AsyncClient
        with patch.object(account_api.httpx, 'AsyncClient', side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={'error': 'invalid_grant'})), **kwargs)):
            with self.assertRaises(account_api.HTTPException) as error:
                asyncio.run(account_api._drive_access_token(self.user))
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(self.store.get_tokens(self.user['id'])['scope'])

    def test_source_download_bounds_cleanup_and_index(self):
        class FakeDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=False):
                return {'entries': [{'id': 'one', 'duration': 20}, {'id': 'two', 'duration': 30}]}
            def process_info(self, selected):
                output = Path(self.options['outtmpl']).parent / (selected['id'] + '.mp4')
                output.write_bytes(b'video')
        with patch.object(media_source.yt_dlp, 'YoutubeDL', FakeDownloader):
            with media_source.downloaded_source(self.data()['source_url'], 1) as path:
                self.assertEqual(path.name, 'two.mp4')
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())
            with self.assertRaises(ValueError):
                with media_source.downloaded_source(self.data()['source_url'], 2):
                    pass
        for metadata in [{'duration': media_source.MAX_MEDIA_DURATION + 1}, {'is_live': True}, {'filesize': media_source.MAX_MEDIA_BYTES + 1}]:
            with self.assertRaises(ValueError):
                media_source._check_metadata(metadata)
        with self.assertRaises(ValueError):
            media_source._bounded_progress({'downloaded_bytes': media_source.MAX_MEDIA_BYTES + 1})

    def test_learning_heartbeat_is_fenced(self):
        item = self.store.create_item(self.user['id'], self.data())
        claim = self.store.claim_learning(self.user['id'], item['id'], 'transcribing')
        self.assertTrue(self.store.renew_learning(self.user['id'], item['id'], claim['learning_claim']))
        self.assertFalse(self.store.renew_learning(self.user['id'], item['id'], 'stale'))
        self.store.update_learning(self.user['id'], item['id'], 'complete', claim_token=claim['learning_claim'])
        self.assertFalse(self.store.renew_learning(self.user['id'], item['id'], claim['learning_claim']))

    def test_cdn_private_unknown_and_photo_fallback(self):
        public_dns = [(None, None, None, None, ('8.8.8.8', 443))]
        with patch.object(media_source.socket, 'getaddrinfo', return_value=public_dns):
            self.assertEqual(media_source._validate_cdn_url('https://cdninstagram.com/photo'), 'https://cdninstagram.com/photo')
        for url in ['https://evil.com/photo', 'http://cdninstagram.com/photo', 'https://user@cdninstagram.com/photo']:
            with self.assertRaises(ValueError):
                media_source._validate_cdn_url(url)
        with patch.object(media_source.socket, 'getaddrinfo', return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaises(ValueError):
                media_source._validate_cdn_url('https://cdninstagram.com/photo')
        result = SimpleNamespace(metadata={'is_video': False}, media_urls=['https://cdninstagram.com/photo'])
        fake_main = SimpleNamespace(extract_instagram=lambda url: result, extract_tiktok_fallback=lambda url: result)
        class BrokenExtractor:
            def __init__(self, options):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, *args, **kwargs):
                raise RuntimeError('unsupported photo')
        client_class = httpx.Client
        with patch.dict(sys.modules, {'main': fake_main}):
            with patch.object(media_source.yt_dlp, 'YoutubeDL', BrokenExtractor):
                with patch.object(media_source.socket, 'getaddrinfo', return_value=public_dns):
                    with patch.object(media_source.httpx, 'Client', side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Type': 'image/jpeg'}, content=b'photo')), **kwargs)):
                        with media_source.downloaded_source('https://www.instagram.com/p/abc') as path:
                            self.assertEqual(path.read_bytes(), b'photo')
                            self.assertEqual(path.suffix, '.jpg')
                        self.assertFalse(path.exists())
                        with self.assertRaises(ValueError):
                            with media_source.downloaded_source('https://www.instagram.com/p/abc', audio_only=True):
                                pass

    def test_idempotent_drive_and_artifact_retries(self):
        path = Path(self.temp.name) / 'video.mp4'
        path.write_bytes(b'video')
        @contextmanager
        def source(*args):
            yield path
        payload = {'source_url': self.data()['source_url'], 'title': 'Lesson', 'request_id': str(uuid4())}
        with patch.object(account_api, 'downloaded_source', source):
            with patch.object(account_api, 'upload_to_drive', new=AsyncMock(return_value='https://drive.google.com/file/d/id/view')) as upload:
                first = self.client.post('/api/v1/drive', json=payload, headers=self.headers)
                second = self.client.post('/api/v1/drive', json=payload, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(upload.await_count, 1)
        self.assertEqual(len(self.store.list_items(self.user['id'])), 1)
        mismatch = self.client.post('/api/v1/drive', json={**payload, 'title': 'Different'}, headers=self.headers)
        self.assertEqual(mismatch.status_code, 422)
        item = first.json()['item']
        self.store.update_learning(self.user['id'], item['id'], 'complete', transcript='lesson transcript')
        artifact_payload = {'artifact': 'transcript', 'request_id': str(uuid4())}
        with patch.object(account_api, 'upload_to_drive', new=AsyncMock(return_value='https://drive.google.com/file/d/artifact/view')) as upload:
            first = self.client.post('/api/v1/library/' + item['id'] + '/drive', json=artifact_payload, headers=self.headers)
            second = self.client.post('/api/v1/library/' + item['id'] + '/drive', json=artifact_payload, headers=self.headers)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(upload.await_count, 1)

    def test_idempotency_pending_expiry_owner_and_failure_release(self):
        request_id = str(uuid4())
        first = self.store.claim_drive_request(self.user['id'], request_id, 'payload', lease_seconds=-1)
        second = self.store.claim_drive_request(self.user['id'], request_id, 'payload')
        self.assertNotEqual(first['claim_token'], second['claim_token'])
        self.store.finish_drive_request(self.user['id'], request_id, first['claim_token'], {'old': True})
        self.assertEqual(self.store.claim_drive_request(self.user['id'], request_id, 'payload')['status'], 'busy')
        self.store.finish_drive_request(self.user['id'], request_id, second['claim_token'], {'saved': True})
        self.assertEqual(self.store.claim_drive_request(self.user['id'], request_id, 'payload')['result'], {'saved': True})
        self.assertEqual(self.store.claim_drive_request('other-owner', request_id, 'payload')['status'], 'claimed')
        self.store.abandon_drive_request('other-owner', request_id, self.store.claim_drive_request('other-owner', request_id, 'payload').get('claim_token', 'wrong'))
        self.assertEqual(self.store.claim_drive_request('other-owner', request_id, 'payload')['status'], 'busy')
        payload = account_api.DriveRequest(source_url=self.data()['source_url'], request_id=uuid4())
        claim = account_api._claim_request(self.user, payload, 'media')
        response = self.client.post('/api/v1/drive', json=payload.model_dump(mode='json'), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        with patch.object(account_api, 'downloaded_source', side_effect=RuntimeError('download failed')):
            with self.assertRaises(RuntimeError):
                account_api._download_and_save(self.user, payload, str(payload.request_id), claim['claim_token'])
        self.assertEqual(account_api._claim_request(self.user, payload, 'media')['status'], 'claimed')

    def test_channel_playlists_rejected_without_consuming_generator(self):
        with self.assertRaises(ValueError):
            media_source.validate_source_url('https://www.youtube.com/@channel')
        self.assertTrue(media_source.validate_source_url('http://www.youtube.com/watch?v=123').startswith('https:'))
        consumed = []
        def unbounded():
            while True:
                consumed.append(1)
                yield {'id': 'x'}
        class PlaylistExtractor:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, *args, **kwargs):
                return {'_type': 'playlist', 'entries': unbounded()}
        with patch.object(media_source.yt_dlp, 'YoutubeDL', PlaylistExtractor):
            with self.assertRaises(ValueError):
                with media_source.downloaded_source(self.data()['source_url']):
                    pass
            self.assertEqual(consumed, [])
            with self.assertRaises(ValueError):
                with media_source.downloaded_source('https://instagram.com/p/carousel'):
                    pass
            self.assertEqual(len(consumed), 101)

    def test_upload_total_timeout_and_failure_cleanup(self):
        async def stalled(*args):
            await asyncio.sleep(0.05)
        timeout = asyncio.timeout
        with patch.object(account_api, '_upload_to_drive', side_effect=stalled):
            with patch.object(account_api.asyncio, 'timeout', side_effect=lambda seconds: timeout(0.001)):
                with self.assertRaises(TimeoutError):
                    asyncio.run(account_api.upload_to_drive(self.user, Path('unused'), 'unused', 'text/plain'))
        item = self.store.create_item(self.user['id'], self.data())
        self.store.update_learning(self.user['id'], item['id'], 'ready', study_notes='notes')
        request_id = str(uuid4())
        payload = {'artifact': 'study_notes', 'request_id': request_id}
        with patch.object(account_api, 'upload_to_drive', new=AsyncMock(side_effect=TimeoutError)):
            response = self.client.post('/api/v1/library/' + item['id'] + '/drive', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT * FROM drive_requests WHERE request_id=?', (request_id,)).fetchone())

    def test_drive_capacity_failure_abandons_claim(self):
        payload = account_api.DriveRequest(source_url=self.data()['source_url'], request_id=uuid4())
        claim = account_api._claim_request(self.user, payload, 'media')
        with patch.object(account_api, '_download_slots') as slots:
            slots.acquire.return_value = False
            with self.assertRaises(account_api.HTTPException) as error:
                account_api._download_and_save(self.user, payload, str(payload.request_id), claim['claim_token'])
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(account_api._claim_request(self.user, payload, 'media')['status'], 'claimed')


if __name__ == '__main__':
    unittest.main()
