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
        with patch.object(sys, 'argv', ['photonctl.py', 'file', 'list', '--page', '1', '--limit', '20',
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
        with patch.object(sys, 'argv', ['photonctl.py', 'file', 'list']), patch.object(p, 'files') as listing:
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

    def test_printer_discovery_uses_only_evidenced_gets(self):
        record = {'id': 42, 'machine_type': 130, 'device_status': 2,
                  'name': 'Mono', 'serial': 'private-serial', 'user_id': 999,
                  'mqtt': {'password': 'private-password'}, 'unknown_numeric': 888}
        with patch.object(p, 'credentials', return_value=('private-token', 'https://api.example')), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            client = session.return_value.__enter__.return_value
            client.get.return_value = Response({'code': 1, 'data': [record]})
            p.printers()
            args, kw = client.get.call_args
            self.assertEqual(args, ('https://api.example/work/printer/getPrinters',))
            self.assertIsNone(kw['params'])
            self.assertEqual(kw['headers']['XX-Token'], 'private-token')
            self.assertFalse(kw['allow_redirects'])
            p.printers('42')
            args, kw = client.get.call_args
            self.assertEqual(args, ('https://api.example/v2/printer/info',))
            self.assertEqual(kw['params'], {'id': '42'})
            self.assertEqual(client.get.call_count, 2)
            client.post.assert_not_called()
            client.put.assert_not_called()
        for secret in ('private-token', 'private-serial', 'private-password', '999', '888'):
            self.assertNotIn(secret, out.getvalue())
        self.assertIn('130', out.getvalue())

    def test_print_preview_offline_and_dispatch_disabled(self):
        with patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('HTTP')), \
                patch.object(p.requests, 'Session', side_effect=AssertionError('network')), \
                patch.object(p, 'credentials', side_effect=AssertionError('credentials')), \
                patch.object(sys, 'argv', ['photonctl.py', 'printer', 'print', '123', '--printer', '42', '--dry-run']), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p.main(), 0)
        preview = json.loads(out.getvalue())
        self.assertFalse(preview['ready_to_send'])
        self.assertFalse(preview['dispatch_enabled'])
        self.assertEqual(preview['content_type'], 'application/json; charset=utf-8')
        self.assertEqual(preview['print_order'], p.android_print_request(123, 42))
        self.assertNotIn('model_130_settings_order', preview)
        self.assertNotIn('1225', out.getvalue())
        self.assertNotIn('taskid', out.getvalue())
        self.assertNotIn('task_mode', out.getvalue())
        self.assertNotIn('filesize', out.getvalue())
        with patch.object(sys, 'argv', ['photonctl.py', 'printer', 'print', '123', '--printer', '42']), \
                patch.object(p.requests, 'Session', side_effect=AssertionError('network')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(p.main(), 1)

    def test_android_print_exact_schema_and_types(self):
        body = p.android_print_request('123', '42')
        self.assertEqual(body, {
            'printer_id': 42, 'order_id': 1, 'project_id': 0,
            'data': {'file_id': 123, 'filename': '', 'filepath': '/',
                     'project_type': 1, 'template_id': 0, 'plate_id': 0,
                     'is_delete_file': 0, 'matrix': '', 'slice_size': None,
                     'slice_param': None, 'hollow_param': None, 'punching_param': None,
                     'task_settings': None, 'filetype': 0, 'file_key': '',
                     'file_name': '', 'official_file_id': '', 'official_file_type': -1},
            'settings': None, 'ams_info': None,
        })
        for container, fields in ((body, ('printer_id', 'order_id', 'project_id')),
                                  (body['data'], ('file_id', 'project_type', 'template_id',
                                                  'plate_id', 'is_delete_file', 'filetype',
                                                  'official_file_type'))):
            for field in fields:
                self.assertIs(type(container[field]), int)
        decoded = json.loads(json.dumps(body))
        for field in ('slice_size', 'slice_param', 'hollow_param', 'punching_param', 'task_settings'):
            self.assertIn(field, decoded['data'])
            self.assertIsNone(decoded['data'][field])
        self.assertIn('settings', decoded)
        self.assertIsNone(decoded['settings'])
        self.assertIn('ams_info', decoded)
        self.assertIsNone(decoded['ams_info'])
        for invalid in (True, 0, -1, 1.5, '1.5', None, 2147483648, '2147483648'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(p.Error):
                    p.android_print_request(invalid, 42)
                with self.assertRaises(p.Error):
                    p.android_print_request(123, invalid)

    def test_android_settings_preserve_explicit_values_without_defaults(self):
        # Synthetic server fixtures, not claims about actual model-130 options.
        options = {'fixture_option', 'another_fixture_option'}
        settings = {'fixture_option': 7}
        body = p.android_print_request(123, 42, settings, options)
        self.assertEqual(body['data']['task_settings'], settings)
        self.assertNotIn('another_fixture_option', body['data']['task_settings'])
        self.assertIsNot(settings, body['data']['task_settings'])
        self.assertEqual(p.android_task_settings({'fixture_option': 0}, options), {'fixture_option': 0})
        self.assertIsNone(p.android_task_settings(None))
        self.assertIsNone(p.android_task_settings({}))

    def test_android_settings_validation(self):
        for settings, supported in [([], None), ('{}', None),
                                    ({'unknown': 1}, None), ({'unknown': 1}, {'known'}),
                                    ({'known': True}, {'known'}), ({'known': '1'}, {'known'}),
                                    ({'known': 1.0}, {'known'}), ({'known': None}, {'known'}),
                                    ({'known': []}, {'known'}),
                                    ({'known': 2147483648}, {'known'}),
                                    ({'known': 1}, {'bad name'}),
                                    ({'stirBeforePrint': 1}, {'stirBeforePrint'}),
                                    ({'stirBeforePrint': 2}, {'stirBeforePrint'})]:
            with self.subTest(settings=settings):
                with self.assertRaises(p.Error):
                    p.android_print_request(123, 42, settings, supported)
        names = {'stirBeforePrint', 'stir_settings'}
        settings = {'stirBeforePrint': 1, 'stir_settings': {'stirTime': 3}}
        self.assertEqual(p.android_task_settings(settings, names), settings)
        self.assertEqual(p.android_task_settings({'stirBeforePrint': 0}, names), {'stirBeforePrint': 0})
        for nested in ({'stir_time': 3}, {'stirTime': True}, {'stirTime': 0},
                       {'stirTime': 3, 'unknown': 1}):
            with self.assertRaises(p.Error):
                p.android_task_settings({'stirBeforePrint': 1, 'stir_settings': nested}, names)
        with self.assertRaises(p.Error):
            p.android_task_settings({'stirBeforePrint': 0, 'stir_settings': {'stirTime': 3}}, names)

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


class PrinterStatusTests(unittest.TestCase):
    def run_status(self, data=None, code=1, http=200, error=None, invalid_json=False):
        with patch.object(p, 'credentials', return_value=('private-token', 'https://api.example/prefix')), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                patch.object(sys, 'argv', ['photonctl.py', 'printer', 'status', '797953']):
            client = session.return_value.__enter__.return_value
            if error:
                client.get.side_effect = error
            else:
                response = Response({'code': code, 'data': data, 'msg': 'private-token https://private/?secret=x'})
                response.status_code = http
                client.get.return_value = response
                if invalid_json:
                    client.get.return_value.json = unittest.mock.Mock(side_effect=ValueError('private-token'))
            result = p.main()
            client.get.assert_called_once()
            args, kwargs = client.get.call_args
            self.assertEqual(args, ('https://api.example/prefix/v2/printer/info',))
            self.assertEqual(kwargs['params'], {'id': '797953'})
            self.assertEqual(kwargs['headers']['XX-Token'], 'private-token')
            self.assertEqual(kwargs['headers']['XX-Signature'],
                             p.signature(kwargs['headers']['XX-Timestamp'], kwargs['headers']['XX-Nonce']))
            self.assertFalse(kwargs['allow_redirects'])
            client.post.assert_not_called()
            client.put.assert_not_called()
        output = out.getvalue() + err.getvalue()
        self.assertNotIn('private-token', output)
        self.assertNotIn('https://private', output)
        return result, output

    def test_online_and_offline_use_device_status_not_printing(self):
        for device_status, state in ((1, 'ONLINE'), (2, 'OFFLINE')):
            with self.subTest(state=state):
                result, output = self.run_status({'id': 797953, 'machine_type': 130,
                                                  'device_status': device_status, 'is_printing': 1,
                                                  'key': 'private-device-key', 'user_id': 765432,
                                                  'version': {'firmware_version': '4.0.7.9'}})
                self.assertEqual(result, 0)
                self.assertIn('Connectivity: ' + state + ' (cloud-reported)', output)
                self.assertIn('"device_status": %d' % device_status, output)
                self.assertIn('"is_printing": 1', output)
                self.assertNotIn('private-device-key', output)
                self.assertNotIn('765432', output)
                self.assertIn('freshness is not guaranteed', output)

    def test_unknown_status_never_uses_cached_printing(self):
        for status in (None, 0, 3, '1', True, [], {}):
            with self.subTest(status=status):
                result, output = self.run_status({'id': 797953, 'machine_type': 130,
                                                  'device_status': status, 'is_printing': 1})
                self.assertEqual(result, 0)
                self.assertIn('Connectivity: UNKNOWN', output)
                self.assertIn('device_status is missing, malformed or unrecognized', output)
        result, output = self.run_status({'id': 797953, 'machine_type': 130, 'is_printing': 1})
        self.assertEqual(result, 0)
        self.assertIn('Connectivity: UNKNOWN', output)

    def test_missing_and_malformed_printers(self):
        for data in (None, {}, [], [{'id': 797953}], 'bad', 1,
                     {'device_status': 1}, {'id': True}, {'id': 'bad'}, {'id': 123}):
            with self.subTest(data=data):
                result, output = self.run_status(data)
                self.assertEqual(result, 1)
                self.assertIn('Connectivity: UNKNOWN', output)
        result, output = self.run_status({'id': 797953, 'device_status': 1, 'machine_type': 10001})
        self.assertEqual(result, 0)
        self.assertIn('Connectivity: UNKNOWN', output)
        self.assertIn('outside the APK LCD semantics', output)

    def test_expired_session_http_and_api_failures(self):
        for options in ({'http': 401}, {'http': 404}, {'http': 503},
                        {'code': 10001}, {'code': 0}, {'code': '1'}, {'code': True},
                        {'invalid_json': True}):
            with self.subTest(options=options):
                result, output = self.run_status(**options)
                self.assertEqual(result, 1)
                self.assertIn('Connectivity: UNKNOWN', output)
                self.assertIn('reimport it with login', output)

    def test_timeout_and_transport_error(self):
        for error in (p.requests.Timeout('https://private/?token=private-token'),
                      p.requests.ConnectionError('private-token')):
            result, output = self.run_status(error=error)
            self.assertEqual(result, 1)
            self.assertIn('Connectivity: UNKNOWN', output)
            self.assertIn('Printer read request failed', output)

    def test_missing_session_and_dry_run_do_not_contact_network(self):
        for argv in (['photonctl.py', 'printer', 'status', '797953'],
                     ['photonctl.py', '--dry-run', 'printer', 'status', '797953']):
            with patch.object(sys, 'argv', argv), \
                    patch.object(p, 'credentials', side_effect=p.Error('No usable session.')), \
                    patch.object(p.requests, 'Session') as session, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(p.main(), 1)
                session.assert_not_called()


class CliHierarchyTests(unittest.TestCase):
    # Each legacy/canonical pair must reach the same existing operation and
    # preserve parsed types, defaults and handler arguments.
    CASES = (
        (['upload', '/tmp/example.pm4u'], ['file', 'upload', '/tmp/example.pm4u'],
         'upload', ('/tmp/example.pm4u', False, False)),
        (['upload', '/tmp/example.pm4u', '--dry-run', '--verbose'],
         ['file', 'upload', '/tmp/example.pm4u', '--dry-run', '--verbose'],
         'upload', ('/tmp/example.pm4u', True, True)),
        (['files'], ['file', 'list'], 'files', ({'page': 1, 'limit': 10},)),
        (['files', '--page', '2', '--limit', '5', '--machine-type', '130', '--file-type', 'pm4u', '--orderby', 'desc'],
         ['file', 'list', '--page', '2', '--limit', '5', '--machine-type', '130', '--file-type', 'pm4u', '--orderby', 'desc'],
         'files', ({'page': 2, 'limit': 5, 'machine_type': 130, 'file_type': 'pm4u', 'orderby': 'desc'},)),
        (['printers'], ['printer', 'list'], 'printers', (None,)),
        (['printer-info', '797953'], ['printer', 'info', '797953'], 'printers', ('797953',)),
        (['printer-status', '797953'], ['printer', 'status', '797953'], 'printer_status', ('797953',)),
        (['print', '123', '--printer', '797953', '--dry-run'],
         ['printer', 'print', '123', '--printer', '797953', '--dry-run'],
         'print_dry_run', ('123', '797953')),
        (['--dry-run', 'upload', '/tmp/example.pm4u'],
         ['--dry-run', 'file', 'upload', '/tmp/example.pm4u'], 'upload', ('/tmp/example.pm4u', True, False)),
        (['--dry-run', 'print', '123', '--printer', '797953'],
         ['--dry-run', 'printer', 'print', '123', '--printer', '797953'],
         'print_dry_run', ('123', '797953')),
    )

    def setUp(self):
        guard = patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No HTTP in CLI tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_alias_and_canonical_parser_namespaces_match(self):
        parser = p.build_parser()
        for legacy, canonical, _, _ in self.CASES:
            with self.subTest(canonical=canonical):
                old = vars(parser.parse_args(legacy))
                new = vars(parser.parse_args(canonical))
                for namespace in (old, new):
                    for key in ('resource', 'action', 'deprecated_alias'):
                        namespace.pop(key, None)
                self.assertEqual(old, new)

    def test_alias_and_canonical_dispatch_identically(self):
        handlers = ('login', 'upload', 'files', 'printers', 'printer_status', 'print_dry_run')
        for legacy, canonical, handler, expected in self.CASES:
            for argv, deprecated in ((legacy, True), (canonical, False)):
                with self.subTest(argv=argv), contextlib.ExitStack() as stack:
                    mocks = {name: stack.enter_context(patch.object(p, name, return_value=0)) for name in handlers}
                    stack.enter_context(patch.object(sys, 'argv', ['photonctl.py'] + argv))
                    stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    self.assertEqual(p.main(), 0)
                    mocks[handler].assert_called_once_with(*expected)
                    for name in handlers:
                        if name != handler:
                            mocks[name].assert_not_called()
                    self.assertEqual('is deprecated' in stderr.getvalue(), deprecated)

    def test_login_stays_at_root(self):
        with patch.object(sys, 'argv', ['photonctl.py', 'login']), patch.object(p, 'login') as login:
            self.assertEqual(p.main(), 0)
            login.assert_called_once_with()

    def test_help_at_every_level_and_hidden_aliases(self):
        for argv in ([], ['file'], ['printer'], ['login'], ['file', 'upload'], ['file', 'list'],
                     ['printer', 'list'], ['printer', 'info'], ['printer', 'status'], ['printer', 'print']):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as exc:
                    p.build_parser().parse_args(argv + ['--help'])
                self.assertEqual(exc.exception.code, 0)
                self.assertIn('usage:', out.getvalue())
                self.assertIn('--help', out.getvalue())
                if not argv:
                    self.assertIn('{login,file,printer}', out.getvalue())
                    for alias in ('printer-info', 'printer-status', 'printers', 'files'):
                        # Match command rows, not words inside descriptions.
                        self.assertNotIn('    ' + alias + ' ', out.getvalue())
                if argv == ['printer', 'print']:
                    self.assertIn('--printer', out.getvalue())
                    self.assertIn('--dry-run', out.getvalue())

    def test_invalid_and_missing_subcommands_or_arguments(self):
        for argv in ([], ['file'], ['printer'], ['file', 'delete'], ['printer', 'stop'],
                     ['file', 'upload'], ['printer', 'info'], ['printer', 'status', 'bad'],
                     ['printer', 'info', '0'], ['printer', 'print', '123', '--dry-run'],
                     ['printer', 'print', 'bad', '--printer', '42', '--dry-run']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    p.build_parser().parse_args(argv)
                self.assertEqual(exc.exception.code, 2)

    def test_print_without_dry_run_remains_disabled(self):
        for argv in (['print', '123', '--printer', '42'], ['printer', 'print', '123', '--printer', '42']):
            with patch.object(sys, 'argv', ['photonctl.py'] + argv), \
                    patch.object(p, 'print_dry_run') as preview, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(p.main(), 1)
                preview.assert_not_called()


class AuthenticationFallbackTests(unittest.TestCase):
    def test_unestablished_native_login_preserves_existing_session_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            session_file = Path(folder) / 'session.json'
            saved = b'{"token":"cached-test-session","api_base":"https://api.example"}'
            session_file.write_bytes(saved)
            session_file.chmod(0o600)
            with patch.object(p, 'SESSION_FILE', session_file), patch.dict(os.environ, {}, clear=True), \
                    patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No HTTP')), \
                    patch('builtins.input', side_effect=AssertionError('Do not solicit unsupported credentials')), \
                    patch.object(sys, 'argv', ['photonctl.py', 'login']), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(p.main(), 1)
            self.assertIn('Email/password login is not verified', err.getvalue())
            self.assertNotIn('cached-test-session', out.getvalue() + err.getvalue())
            self.assertEqual(session_file.read_bytes(), saved)
            self.assertEqual(session_file.stat().st_mode & 0o777, 0o600)

    def test_invalid_manual_import_preserves_existing_session_without_exposure(self):
        for env in ({'PHOTONCTL_TOKEN': 'private-token'},
                    {'PHOTONCTL_TOKEN': 'private-token', 'PHOTONCTL_API_BASE': 'http://invalid.example'},
                    {'PHOTONCTL_TOKEN': 'private-token\ninvalid', 'PHOTONCTL_API_BASE': 'https://api.example'}):
            with self.subTest(env_keys=list(env)), tempfile.TemporaryDirectory() as folder:
                session_file = Path(folder) / 'session.json'
                saved = b'{"token":"cached-test-session","api_base":"https://api.example"}'
                session_file.write_bytes(saved)
                session_file.chmod(0o600)
                with patch.object(p, 'SESSION_FILE', session_file), patch.dict(os.environ, env, clear=True), \
                        patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No HTTP')), \
                        patch.object(sys, 'argv', ['photonctl.py', 'login']), \
                        contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(p.main(), 1)
                self.assertEqual(session_file.read_bytes(), saved)
                self.assertNotIn('private-token', out.getvalue() + err.getvalue())
                self.assertNotIn('cached-test-session', out.getvalue() + err.getvalue())


if __name__ == '__main__':
    unittest.main()
