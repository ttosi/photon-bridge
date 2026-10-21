import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import photonctl as p


class Response:
    status_code = 200
    headers = {}

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class ProtocolTests(unittest.TestCase):
    def test_signature_vector(self):
        self.assertEqual(p.signature(1700000000123, '00112233-4455-4677-8899-aabbccddeeff'),
                         '4E0026EB77A269E0CE0027F1BC56297A')
        h = p.headers('test-token')
        self.assertEqual(str(p.uuid.UUID(h['XX-Nonce'], version=4)), h['XX-Nonce'])
        self.assertEqual(h['XX-Signature'], p.signature(h['XX-Timestamp'], h['XX-Nonce']))

    def run_upload(self, bad_etag=False, fail_confirmation=False, verbose=False, fail_unlock=False, missing_id=False):
        content = bytes(range(256)) * 513 + b'\x00exact\xffbytes'
        digest = hashlib.md5(content).hexdigest().upper()
        calls = []
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, url, **kw):
                calls.append((url, kw))
                outer.assertEqual(kw['headers']['XX-Token'], 'secret-session')
                outer.assertFalse(kw['allow_redirects'])
                if url.endswith('/lockStorageSpace'):
                    outer.assertEqual(kw['json'], {'is_temp_file': 0, 'size': len(content), 'name': 'model.pm4u'})
                    return Response({'code': 1, 'data': {'id': 123, 'url': 'unused',
                                                       'preSignUrl': 'https://storage.example/file?secret=hidden'}})
                if url.endswith('/newUploadFile'):
                    outer.assertEqual(kw['json'], {'user_lock_space_id': 123})
                    if fail_confirmation:
                        raise p.requests.Timeout('sensitive URL must not appear')
                    return Response({'code': 1, 'data': {} if missing_id else {'id': 456}})
                outer.assertTrue(url.endswith('/unlockStorageSpace'))
                outer.assertEqual(kw['json'], {'id': 123, 'is_delete_cos': 0})
                if fail_unlock:
                    raise p.requests.Timeout('sensitive URL must not appear')
                return Response({'code': 1, 'data': {}})

            def put(self, url, **kw):
                calls.append(('put', kw))
                outer.assertEqual(kw['data'].read(), content)
                outer.assertEqual(kw['headers'], {'Content-Length': str(len(content)), 'Expect': ''})
                outer.assertFalse(kw['allow_redirects'])
                response = Response(None)
                response.headers = {'ETag': '"' + ('0' * 32 if bad_etag else digest.lower()) + '"'}
                return response

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.pm4u'
            path.write_bytes(content)
            with patch.dict(os.environ, {'PHOTONCTL_TOKEN': 'secret-session',
                                         'PHOTONCTL_API_BASE': 'https://api.example/prefix'}), \
                    patch.object(p.requests, 'Session', Client), contextlib.redirect_stdout(io.StringIO()) as out:
                if bad_etag or fail_confirmation or fail_unlock or missing_id:
                    with self.assertRaises(p.Error) as err:
                        p.upload(path, verbose=verbose)
                    self.assertNotIn('sensitive URL', str(err.exception))
                    if fail_confirmation:
                        self.assertIn('Check the Android app', str(err.exception))
                else:
                    p.upload(path, verbose=verbose)
                    self.assertIn('Registered cloud file ID: 456', out.getvalue())
                if fail_unlock:
                    self.assertIn('Confirmation succeeded', str(err.exception))
                    self.assertIn('Do not repeat', str(err.exception))
                if missing_id:
                    self.assertIn('data.id was missing or invalid', str(err.exception))
                self.assertNotIn('secret-session', out.getvalue())
                self.assertNotIn('hidden', out.getvalue())
                if verbose:
                    self.assertIn('/v2/cloud_storage/lockStorageSpace response:', out.getvalue())
                    self.assertIn('/v2/profile/newUploadFile response:', out.getvalue())
                    self.assertIn('"code": 1', out.getvalue())
                    self.assertIn('"id": 123', out.getvalue())
                    self.assertIn('"id": 456', out.getvalue())
                    self.assertIn('/v2/cloud_storage/unlockStorageSpace response:', out.getvalue())
            self.assertEqual(path.read_bytes(), content)
        endpoints = [url.rsplit('/', 1)[-1] for url, _ in calls]
        expected = ['lockStorageSpace', 'put']
        if not bad_etag:
            expected.append('newUploadFile')
            if not fail_confirmation:
                expected.append('unlockStorageSpace')
        self.assertEqual(endpoints, expected)

    def test_full_workflow(self):
        self.run_upload()

    def test_bad_etag_prevents_confirmation(self):
        self.run_upload(bad_etag=True)

    def test_confirmation_not_retried(self):
        self.run_upload(fail_confirmation=True)

    def test_unlock_failure_does_not_repeat_confirmation(self):
        self.run_upload(fail_unlock=True)

    def test_missing_registered_id_still_releases_reservation(self):
        self.run_upload(missing_id=True)

    def test_dry_run_no_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.pm4u'
            path.write_bytes(b'raw bytes')
            with patch.object(p.requests, 'Session', side_effect=AssertionError('network')), \
                    patch.object(p, 'credentials', side_effect=AssertionError('credentials')), \
                    contextlib.redirect_stdout(io.StringIO()):
                p.upload(path, True)

    def test_verbose_upload(self):
        self.run_upload(verbose=True)

    def test_listing_default_payload_and_safe_metadata(self):
        body = {'code': 1, 'data': {'list': [{
            'id': '123', 'original_filename': 'model.pm4u', 'file_type': 'pm4u',
            'status': 'ready', 'file_size': 1234, 'created_at': '2026-10-20',
            'token': 'hidden-token', 'preSignUrl': 'https://storage.example/?secret=hidden',
            'nested': {'Authorization': 'hidden-auth'}, 'unknown': 'hidden-unknown'
        }], 'total': 1}}
        with patch.object(p, 'credentials', return_value=('session-secret', 'https://api.example')), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            client = session.return_value.__enter__.return_value
            client.post.return_value = Response(body)
            p.files({})
            client.post.assert_called_once()
            args, kw = client.post.call_args
            self.assertEqual(args, ('https://api.example/work/index/files',))
            self.assertEqual(kw['json'], {'page': 1, 'limit': 10})
            self.assertEqual(kw['headers']['XX-Token'], 'session-secret')
            self.assertFalse(kw['allow_redirects'])
            client.put.assert_not_called()
            for text in ('session-secret', 'hidden-token', 'hidden-auth', 'hidden-unknown', 'https://storage'):
                self.assertNotIn(text, out.getvalue())
            for text in ('123', 'model.pm4u', 'ready', '1234', '2026-10-20'):
                self.assertIn(text, out.getvalue())

    def test_listing_explicit_cli_filters(self):
        with patch.object(sys, 'argv', ['photonctl.py', 'files', '--page', '1', '--limit', '20',
                                       '--machine-type', '130', '--file-type', 'pm4u', '--orderby', 'desc']), \
                patch.object(p, 'files') as listing:
            self.assertEqual(p.main(), 0)
            listing.assert_called_once_with({'page': 1, 'limit': 20, 'machine_type': 130,
                                            'file_type': 'pm4u', 'orderby': 'desc'})

    def test_listing_pagination(self):
        with patch.object(p, 'credentials', return_value=('session-secret', 'https://api.example')), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()):
            client = session.return_value.__enter__.return_value
            client.post.return_value = Response({'code': 1, 'data': []})
            p.files({'page': 2, 'limit': 5})
            self.assertEqual(client.post.call_args.kwargs['json'], {'page': 2, 'limit': 5})
            client.post.assert_called_once()

    def test_listing_cli_defaults(self):
        with patch.object(sys, 'argv', ['photonctl.py', 'files']), patch.object(p, 'files') as listing:
            self.assertEqual(p.main(), 0)
            listing.assert_called_once_with({'page': 1, 'limit': 10})

    def test_known_metadata_allowlist(self):
        metadata = {'old_filename': 'model.pm4u', 'file_extension': 'pm4u',
                    'machine_name': 'Photon Mono 4 Ultra',
                    'md5': '0123456789ABCDEF0123456789ABCDEF'}
        self.assertEqual(p.sanitized(metadata, 'session-secret'), metadata)
        sensitive = {'XX-Token': 'session-secret', 'Authorization': 'Bearer hidden',
                     'preSignUrl': 'https://storage.example/file?signature=hidden',
                     'query_parameters': 'signature=hidden', 'storage_path': '/bucket/private/file',
                     'unknown': 'hidden'}
        output = json.dumps(p.sanitized(sensitive, 'session-secret'))
        for secret in ('session-secret', 'Bearer hidden', 'https://storage.example',
                       'signature=hidden', '/bucket/private/file', '"hidden"'):
            self.assertNotIn(secret, output)

    def test_rejected_response_is_sanitized_before_error(self):
        client = unittest.mock.Mock()
        client.post.return_value = Response({'code': 123, 'msg': 'missing page; session-secret https://x/?secret=z',
                                            'data': {'access_token': 'hidden'}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(p.Error):
                p.api_post(client, 'https://api.example', 'session-secret', '/work/index/files', {}, True)
        self.assertIn('missing page', out.getvalue())
        self.assertIn('123', out.getvalue())
        for text in ('session-secret', 'https://x', 'hidden'):
            self.assertNotIn(text, out.getvalue())

    def test_session_cache_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config/session.json'
            with patch.object(p, 'SESSION_FILE', path), \
                    patch.dict(os.environ, {'PHOTONCTL_TOKEN': 'secret-session',
                                            'PHOTONCTL_API_BASE': 'https://api.example'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                p.login()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text())['token'], 'secret-session')
            with patch.object(p, 'SESSION_FILE', path), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(p.credentials(), ('secret-session', 'https://api.example'))


if __name__ == '__main__':
    unittest.main()
