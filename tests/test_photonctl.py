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
                    self.assertIn('Cloud file ID: 456', out.getvalue())
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
            with patch.object(p, 'output', return_value=p.Output(verbose=True)):
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
            with patch.object(p, 'output', return_value=p.Output(verbose=True)):
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
    def run_status(self, data=None, code=1, http=200, error=None, invalid_json=False, argv=None):
        with patch.object(p, 'credentials', return_value=('private-token', 'https://api.example/prefix')), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                patch.object(sys, 'argv', (argv or ['photonctl.py', 'printer', 'status', '797953']) + ['--verbose']):
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

    def test_observed_activity_states_with_one_get(self):
        for status, printing, connectivity, activity in (
                (1, 1, 'ONLINE', 'IDLE'), (1, 2, 'ONLINE', 'PRINTING'),
                (2, 1, 'OFFLINE', 'UNKNOWN'), (2, 2, 'OFFLINE', 'UNKNOWN')):
            with self.subTest(status=status, printing=printing):
                result, output = self.run_status({'id': 797953, 'machine_type': 130,
                                                  'device_status': status, 'is_printing': printing})
                self.assertEqual(result, 0)
                self.assertIn('Connectivity: ' + connectivity, output)
                self.assertIn('Activity: ' + activity, output)
                if status == 2:
                    self.assertIn('last activity may be stale', output)

    def test_interpreter_unknown_activity_and_models(self):
        for value in (None, 0, 3, '1', True, 1.0, [], {}):
            with self.subTest(value=value):
                record = {'machine_type': 130, 'device_status': 1, 'is_printing': value}
                self.assertEqual(p.interpret_printer_status(record)[:2], ('ONLINE', 'UNKNOWN'))
        self.assertEqual(p.interpret_printer_status({'machine_type': 130, 'device_status': 1})[:2],
                         ('ONLINE', 'UNKNOWN'))
        for model, connectivity in ((131, 'ONLINE'), (None, 'UNKNOWN'), ('130', 'UNKNOWN'),
                                    (True, 'UNKNOWN'), (10001, 'UNKNOWN')):
            with self.subTest(model=model):
                record = {'machine_type': model, 'device_status': 1, 'is_printing': 2}
                self.assertEqual(p.interpret_printer_status(record)[:2], (connectivity, 'UNKNOWN'))

    def test_activity_with_default_and_explicit_override(self):
        record = {'id': 797953, 'machine_type': 130, 'device_status': 1, 'is_printing': 2}
        with patch.object(p, 'load_config', return_value={'default_printer': {'id': '797953'}}):
            result, output = self.run_status(record, argv=['photonctl.py', 'printer', 'status'])
            self.assertEqual(result, 0)
            self.assertIn('Activity: PRINTING', output)
        with patch.object(p, 'load_config', side_effect=AssertionError('Explicit ID must bypass config')):
            result, output = self.run_status(record)
            self.assertEqual(result, 0)
            self.assertIn('Activity: PRINTING', output)

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
        (['--dry-run', 'upload', '/tmp/example.pm4u'],
         ['--dry-run', 'file', 'upload', '/tmp/example.pm4u'], 'upload', ('/tmp/example.pm4u', True, False)),
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
                    self.assertIn('{login,file,printer,print,help}', out.getvalue())
                    for alias in ('printer-info', 'printer-status', 'printers', 'files'):
                        # Match command rows, not words inside descriptions.
                        self.assertNotIn('    ' + alias + ' ', out.getvalue())
                if argv == ['printer', 'print']:
                    self.assertIn('--printer', out.getvalue())
                    self.assertIn('--dry-run', out.getvalue())

    def test_invalid_and_missing_subcommands_or_arguments(self):
        for argv in ([], ['file'], ['printer'], ['file', 'delete'], ['printer', 'stop'],
                     ['file', 'upload'], ['printer', 'status', 'bad'],
                     ['printer', 'info', '0'],
                     ['printer', 'print', 'bad', '--printer', '42', '--dry-run']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    p.build_parser().parse_args(argv)
                self.assertEqual(exc.exception.code, 2)

    def test_live_print_without_interactive_terminal_sends_nothing(self):
        for argv in (['printer', 'print', '123', '--printer', '42'],):
            with patch.object(sys, 'argv', ['photonctl.py'] + argv), \
                    patch.object(p, 'print_dry_run') as preview, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(p.main(), 1)
                preview.assert_not_called()


class HelpCommandTests(unittest.TestCase):
    PATHS = ([], ['login'], ['print'], ['file'], ['file', 'upload'], ['file', 'list'],
             ['printer'], ['printer', 'list'], ['printer', 'set-default'],
             ['printer', 'default'], ['printer', 'info'], ['printer', 'status'],
             ['printer', 'print'])

    def setUp(self):
        for target, attribute in ((p, 'credentials'), (p, 'load_config'),
                                  (Path, 'open'), (p.requests.sessions.Session, 'request')):
            guard = patch.object(target, attribute, side_effect=AssertionError('Help must stay offline'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_version_is_offline(self):
        for flag in ('-v', '--version'):
            with self.subTest(flag=flag), patch.object(sys, 'argv', ['photonctl.py', flag]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as exc:
                    p.main()
            self.assertEqual(exc.exception.code, 0)
            self.assertEqual(out.getvalue(), '0.0.1\n')

    def test_help_paths_match_native_help_exactly(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                with patch.object(sys, 'argv', ['photonctl.py', 'help'] + path), \
                        contextlib.redirect_stdout(io.StringIO()) as custom:
                    self.assertEqual(p.main(), 0)
                with patch.object(sys, 'argv', ['photonctl.py'] + path + ['--help']), \
                        contextlib.redirect_stdout(io.StringIO()) as native:
                    with self.assertRaises(SystemExit) as exc:
                        p.main()
                self.assertEqual(exc.exception.code, 0)
                self.assertEqual(custom.getvalue(), native.getvalue())

    def test_resolves_actual_parser_and_arbitrary_depth(self):
        root = p.build_parser()
        children = next(a.choices for a in root._actions if isinstance(a, p.argparse._SubParsersAction))
        printer = children['printer']
        leaves = next(a.choices for a in printer._actions if isinstance(a, p.argparse._SubParsersAction))
        self.assertIs(p.resolve_help_parser(root, ['printer', 'status']), leaves['status'])
        root = p.argparse.ArgumentParser()
        current = root
        path = ['level' + str(n) for n in range(8)]
        for name in path:
            current = current.add_subparsers().add_parser(name)
        self.assertIs(p.resolve_help_parser(root, path), current)

    def test_unknown_paths_fail_with_clear_errors_and_suggestions(self):
        for path, expected in ((['prnter'], "Did you mean 'printer'?"),
                               (['printer', 'statsu'], "Did you mean 'status'?"),
                               (['file', 'delete'], 'Available commands: upload, list'),
                               (['printer', 'status', '123'], 'has no subcommands')):
            with self.subTest(path=path), patch.object(sys, 'argv', ['photonctl.py', 'help'] + path), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as exc:
                    p.main()
                self.assertEqual(exc.exception.code, 2)
                self.assertIn('Unknown help target', err.getvalue())
                self.assertIn(expected, err.getvalue())
                self.assertNotIn('Traceback', err.getvalue())
                self.assertNotIn('printer-info', err.getvalue())

    def test_help_preserves_hidden_alias_help_and_safety_descriptions(self):
        parser = p.build_parser()
        root_help = parser.format_help()
        self.assertNotIn('    printer-info ', root_help)
        self.assertIn('Deprecated alias', p.resolve_help_parser(parser, ['printer-info']).format_help())
        preview = p.resolve_help_parser(parser, ['printer', 'print']).format_help()
        self.assertIn('interactive confirmation', preview)
        self.assertIn('saved default', preview)
        self.assertIn('--dry-run is strictly offline', preview)
        status = p.resolve_help_parser(parser, ['printer', 'status']).format_help()
        self.assertIn('explicit ID overrides', status)


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


class DefaultPrinterTests(unittest.TestCase):
    RECORDS = [{'id': 101, 'name': 'Desk printer', 'model': 'Mono test', 'machine_type': 130},
               {'id': 202, 'name': 'Other printer', 'model': 'Other test', 'machine_type': 130}]

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / 'photonctl/config.json'
        for patcher in (patch.object(p, 'CONFIG_FILE', self.config),
                        patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No live HTTP'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, args, records=None, answers=(), interactive=True, api_error=None):
        with patch.object(sys, 'argv', ['photonctl.py'] + args), \
                patch.object(sys.stdin, 'isatty', return_value=interactive), \
                patch('builtins.input', side_effect=answers) as prompt, \
                patch.object(p, 'api_get_printers', return_value=(self.RECORDS if records is None else records, 'test-token'),
                             side_effect=api_error) as api, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = p.main()
        return code, out.getvalue(), err.getvalue(), api, prompt

    def test_zero_printers_and_api_errors_do_not_save(self):
        code, out, err, api, prompt = self.run_command(['printer', 'set-default'], records=[])
        self.assertEqual(code, 1)
        self.assertIn('No registered printers', err)
        api.assert_called_once_with('/work/printer/getPrinters')
        prompt.assert_not_called()
        self.assertFalse(self.config.exists())
        for records, failure in ((None, p.Error('Printer read request failed.')), ('bad', None),
                                 ([{'id': 'bad'}], None), ([{'id': 101}, {'id': 101}], None)):
            result = self.run_command(['printer', 'set-default'], records=records, api_error=failure)
            self.assertEqual(result[0], 1)
            self.assertFalse(self.config.exists())

    def test_one_printer_saves_immediately_after_selection(self):
        result = self.run_command(['printer', 'set-default'], records=self.RECORDS[:1], answers=['1'])
        self.assertEqual(result[0], 0)
        self.assertEqual(result[4].call_count, 1)
        self.assertIn('Default printer set to Desk printer.', result[1])
        self.assertEqual(p.load_config()['default_printer']['id'], '101')
        self.assertIn('Desk printer', result[1])
        self.assertIn(' 1) Desk printer (101)', result[1])
        self.assertNotIn('{', result[1])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)

    def test_multiple_printers_invalid_input_then_selection(self):
        result = self.run_command(['printer', 'set-default'], answers=['bad', '0', '3', '2'])
        self.assertEqual(result[0], 0)
        self.assertIn('Enter a number from 1 to 2', result[1])
        self.assertEqual(p.load_config()['default_printer'], {'id': '202', 'name': 'Other printer', 'model': 'Other test'})
        result[3].assert_called_once_with('/work/printer/getPrinters')
        self.assertNotIn('test-token', result[1])

    def test_cancel_eof_interrupt_keep_config(self):
        p.save_default_printer(self.RECORDS[0])
        original = self.config.read_bytes()
        for answers, expected in (([''], 0), (['q'], 0), (['cancel'], 0),
                                  ([EOFError()], 0), ([KeyboardInterrupt()], 130),
                                  (['bad', EOFError()], 0), (['bad', KeyboardInterrupt()], 130)):
            with self.subTest(answers=answers):
                result = self.run_command(['printer', 'set-default'], answers=answers)
                self.assertEqual(result[0], expected)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertIn('*1) Desk printer (101)', result[1])
                self.assertIn('Cancelled', result[1])

    def test_noninteractive_fails_before_api_or_prompt(self):
        result = self.run_command(['printer', 'set-default'], interactive=False)
        self.assertEqual(result[0], 1)
        self.assertIn('requires interactive stdin', result[2])
        result[3].assert_not_called()
        result[4].assert_not_called()
        self.assertFalse(self.config.exists())

    def test_local_default_and_missing_default(self):
        self.assertEqual(p.load_config(), {})
        result = self.run_command(['printer', 'default'])
        self.assertEqual(result[0], 1)
        self.assertIn('printer set-default', result[2])
        result[3].assert_not_called()
        p.save_default_printer(self.RECORDS[1])
        result = self.run_command(['printer', 'default'])
        self.assertEqual(result[0], 0)
        self.assertIn('(202)', result[1])
        self.assertIn('Other printer', result[1])
        verbose = self.run_command(['printer', 'default', '--verbose'])
        self.assertIn('not validated online', verbose[1])
        result[3].assert_not_called()

    def test_save_atomic_separate_from_session_and_preserves_other_config(self):
        self.config.parent.mkdir()
        session = self.config.parent / 'session.json'
        session.write_bytes(b'untouched-session')
        self.config.write_text(json.dumps({'other_setting': 123}))
        self.config.chmod(0o600)
        original_replace = os.replace
        def replace(source, target):
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(self.config.read_text()), {'other_setting': 123})
            original_replace(source, target)
        with patch.object(p.os, 'replace', side_effect=replace):
            p.save_default_printer(self.RECORDS[0])
        self.assertEqual(p.load_config()['other_setting'], 123)
        self.assertEqual(session.read_bytes(), b'untouched-session')
        original = self.config.read_bytes()
        with patch.object(p.os, 'replace', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                p.save_default_printer(self.RECORDS[1])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.glob('.config-*')), [])

    def test_invalid_config_and_permissions(self):
        self.config.parent.mkdir()
        for text in ('not JSON', '[]', '{"default_printer": "bad"}', '{"default_printer":{"id":true}}'):
            self.config.write_text(text)
            self.config.chmod(0o600)
            with self.assertRaises(p.Error):
                p.load_config()
        self.config.write_text('{}')
        self.config.chmod(0o644)
        with self.assertRaisesRegex(p.Error, '0600'):
            p.load_config()

    def test_fallback_and_explicit_override_no_extra_requests(self):
        p.save_default_printer(self.RECORDS[0])
        for args, handler, expected in ((['printer', 'info'], 'printers', ('101',)),
                                        (['printer', 'status'], 'printer_status', ('101',)),
                                        (['printer', 'print', '303', '--dry-run'], 'print_dry_run', ('303', '101')),
                                        (['printer-info'], 'printers', ('101',)),
                                        (['printer-status'], 'printer_status', ('101',)),
                                        (['--dry-run', 'printer', 'print', '303'], 'print_dry_run', ('303', '101'))):
            with self.subTest(args=args), patch.object(p, handler, return_value=0) as callback:
                result = self.run_command(args)
                self.assertEqual(result[0], 0)
                callback.assert_called_once_with(*expected)
                result[3].assert_not_called()
        for args, handler, expected in ((['printer', 'info', '202'], 'printers', ('202',)),
                                        (['printer', 'status', '202'], 'printer_status', ('202',)),
                                        (['printer', 'print', '303', '--printer', '202', '--dry-run'],
                                         'print_dry_run', ('303', '202'))):
            with patch.object(p, 'load_config', side_effect=AssertionError('Explicit overrides config')), \
                    patch.object(p, handler, return_value=0) as callback:
                result = self.run_command(args)
                self.assertEqual(result[0], 0)
                callback.assert_called_once_with(*expected)
                result[3].assert_not_called()

    def test_missing_default_never_selects_implicitly(self):
        for args in (['printer', 'info'], ['printer', 'status'], ['printer', 'print', '303', '--dry-run']):
            result = self.run_command(args)
            self.assertEqual(result[0], 1)
            self.assertIn('printer set-default', result[2])
            result[3].assert_not_called()
            result[4].assert_not_called()
            self.assertFalse(self.config.exists())

    def test_list_default_marker_and_stale_default_reselection(self):
        baseline = self.run_command(['printer', 'list'])
        p.save_default_printer(self.RECORDS[0])
        marked = self.run_command(['printer', 'list'])
        self.assertNotIn('*', baseline[1])
        self.assertIn('101 *', marked[1])
        baseline_json = self.run_command(['printer', 'list', '--json'])
        self.assertEqual(json.loads(baseline_json[1]), self.RECORDS)
        original = self.config.read_bytes()
        stale = self.run_command(['printer', 'list'], records=self.RECORDS[1:])
        self.assertIn('not in the returned list', stale[2])
        self.assertIn('printer set-default', stale[2])
        self.assertEqual(self.config.read_bytes(), original)
        result = self.run_command(['printer', 'set-default'], records=self.RECORDS[1:], answers=['q'])
        self.assertIn('not in the returned list', result[1])
        self.assertEqual(self.config.read_bytes(), original)
        result = self.run_command(['printer', 'set-default'], records=self.RECORDS[1:], answers=['1'])
        self.assertEqual(result[0], 0)
        self.assertEqual(p.load_config()['default_printer']['id'], '202')

    def test_default_dry_run_stays_offline_and_live_requires_terminal(self):
        p.save_default_printer(self.RECORDS[0])
        result = self.run_command(['printer', 'print', '303', '--dry-run'])
        self.assertEqual(result[0], 0)
        self.assertEqual(json.loads(result[1])['print_order']['printer_id'], 101)
        self.assertFalse(json.loads(result[1])['dispatch_enabled'])
        result[3].assert_not_called()
        result = self.run_command(['printer', 'print', '303'])
        self.assertEqual(result[0], 1)
        self.assertIn('interactive', result[2])
        result[3].assert_not_called()

    def test_selection_only_gets_printers_with_existing_signing(self):
        with patch.object(p, 'credentials', return_value=('test-token', 'https://api.example')), \
                patch.object(p.requests, 'Session') as session, patch.object(sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['2']), contextlib.redirect_stdout(io.StringIO()):
            client = session.return_value.__enter__.return_value
            client.get.return_value = Response({'code': 1, 'data': self.RECORDS})
            self.assertEqual(p.set_default_printer(), 0)
            client.get.assert_called_once()
            self.assertEqual(client.get.call_args.args, ('https://api.example/work/printer/getPrinters',))
            self.assertEqual(client.get.call_args.kwargs['headers']['XX-Token'], 'test-token')
            client.post.assert_not_called()
            client.put.assert_not_called()



class PresentationTests(unittest.TestCase):
    PRINTER = {'id': 797953, 'name': 'Anycubic Photon Mono 4 Ultra',
               'machine_type': 130, 'device_status': 1, 'is_printing': 1,
               'version': {'firmware_version': '4.0.7.9'}}

    def setUp(self):
        guard = patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No live HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def run_cli(self, argv, data, config=None, code=1):
        with patch.object(sys, 'argv', ['photonctl.py'] + argv), \
                patch.object(p, 'credentials', return_value=('private-token', 'https://api.example')), \
                patch.object(p, 'load_config', return_value=config or {}), \
                patch.object(p.requests, 'Session') as session, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            client = session.return_value.__enter__.return_value
            client.get.return_value = client.post.return_value = Response({'code': code, 'data': data})
            result = p.main()
        return result, out.getvalue(), err.getvalue(), client

    def test_human_status_all_states_one_read(self):
        for device, printing, connectivity, activity in (
                (1, 1, 'ONLINE', 'IDLE'), (1, 2, 'ONLINE', 'PRINTING'),
                (2, 1, 'OFFLINE', 'UNKNOWN'), (2, 2, 'OFFLINE', 'UNKNOWN'),
                (None, 2, 'UNKNOWN', 'UNKNOWN'), (1, None, 'ONLINE', 'UNKNOWN')):
            with self.subTest(device=device, printing=printing):
                record = {**self.PRINTER, 'device_status': device, 'is_printing': printing}
                result, text, err, client = self.run_cli(['printer', 'status', '797953'], record)
                self.assertEqual(result, 0)
                self.assertEqual(err, '')
                self.assertIn('Anycubic Photon Mono 4 Ultra', text)
                self.assertIn('Status: ' + connectivity, text)
                self.assertIn('Activity: ' + activity, text)
                for technical in ('/v2/', 'freshness', 'device_status', '{', '\033', 'READY'):
                    self.assertNotIn(technical, text)
                client.get.assert_called_once()
                client.post.assert_not_called()
                client.put.assert_not_called()
                self.assertEqual(client.get.call_args.kwargs['params'], {'id': '797953'})

    def test_multiple_printers_marker_without_interpreting_list_flags(self):
        records = [self.PRINTER, {**self.PRINTER, 'id': '123', 'name': 'Second printer'}]
        result, text, _, client = self.run_cli(['printer', 'list'], {'list': records},
                                              {'default_printer': {'id': '797953'}})
        self.assertEqual(result, 0)
        self.assertIn('797953 *', text)
        self.assertIn('Second printer', text)
        self.assertIn('ACTIVITY', text)
        self.assertNotIn('ONLINE', text)
        self.assertEqual(text.count('UNKNOWN'), 4)
        client.get.assert_called_once()
        client.post.assert_not_called()

    def test_info_and_local_default(self):
        result, text, _, client = self.run_cli(['printer', 'info', '797953'], self.PRINTER)
        self.assertEqual(result, 0)
        self.assertIn('Firmware: 4.0.7.9', text)
        self.assertIn('Status: ONLINE', text)
        client.get.assert_called_once()
        result, text, _, client = self.run_cli(['printer', 'default'], None,
                                              {'default_printer': {'id': '797953', 'name': 'Mono'}})
        self.assertEqual(text, 'Default printer: Mono (797953)\n')
        client.get.assert_not_called()
        client.post.assert_not_called()

    def test_files_human_long_missing_and_raw_status(self):
        records = [{'id': 123, 'old_filename': 'a' * 200 + '.pm4u', 'file_extension': 'pm4u',
                    'file_size': 1536, 'status': 2}, {'id': 456}]
        result, text, _, client = self.run_cli(['file', 'list'], {'list': records})
        self.assertEqual(result, 0)
        self.assertIn('1.5 KiB', text)
        self.assertIn('…', text)
        self.assertIn('UNKNOWN', text)
        self.assertIn('123', text)
        self.assertNotIn('Ready', text)
        self.assertNotIn('Processing', text)
        self.assertLess(max(map(len, text.splitlines())), 101)
        client.post.assert_called_once()
        self.assertEqual(client.post.call_args.kwargs['json'], {'page': 1, 'limit': 10})
        client.get.assert_not_called()
        client.put.assert_not_called()

    def test_empty_and_unrecognized_lists(self):
        for path, message in ((['file', 'list'], 'No cloud files'), (['printer', 'list'], 'No registered printers')):
            result, text, _, client = self.run_cli(path, [])
            self.assertEqual(result, 0)
            self.assertIn(message, text)
            self.assertNotIn('[]', text)
        for path in (['file', 'list'], ['printer', 'list']):
            result, text, _, _ = self.run_cli(path, {'unexpected': 4})
            self.assertEqual(result, 0)
            self.assertIn('Unrecognized', text)

    def test_file_sizes(self):
        for size, expected in ((0, '0 B'), (1023, '1023 B'), (1024, '1.0 KiB'),
                               (1536, '1.5 KiB'), (1048576, '1.0 MiB'),
                               (None, '-'), ('1024', '-'), (True, '-'), (-1, '-')):
            self.assertEqual(p.human_size(size), expected)

    def test_json_preserves_raw_types_and_structure_at_all_option_levels(self):
        record = {**self.PRINTER, 'secret': 'hidden', 'user_id': 99}
        for argv in (['--json', 'printer', 'status', '797953'],
                     ['printer', '--json', 'status', '797953'],
                     ['printer', 'status', '797953', '--json'],
                     ['--verbose', 'printer', 'status', '797953', '--json']):
            result, text, _, client = self.run_cli(argv, record)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(text), self.PRINTER)
            self.assertNotIn('\033', text)
            client.get.assert_called_once()
        files = {'list': [{'id': 123, 'file_size': 1024, 'status': 2,
                           'old_filename': 'model.pm4u'}], 'total': 1}
        result, text, _, client = self.run_cli(['file', 'list', '--json'], files)
        self.assertEqual(json.loads(text), files)
        client.post.assert_called_once()
        result, text, _, _ = self.run_cli(['printer', 'list', '--json'], [self.PRINTER])
        self.assertEqual(json.loads(text), [self.PRINTER])
        result, text, _, client = self.run_cli(['printer', 'default', '--json'], None,
                                              {'default_printer': {'id': '797953'}})
        self.assertEqual(json.loads(text), {'id': '797953'})
        client.get.assert_not_called()

    def test_verbose_sources_raw_flags_and_sanitization(self):
        record = {**self.PRINTER, 'key': 'private-key', 'serial': 'private-serial',
                  'user_id': 999, 'name': 'Mono private-token https://x/?signature=private-query'}
        for argv in (['--verbose', 'printer', 'status', '797953'],
                     ['printer', 'status', '797953', '--verbose']):
            result, text, _, _ = self.run_cli(argv, record)
            self.assertIn('GET /v2/printer/info', text)
            self.assertIn('"device_status": 1', text)
            self.assertIn('freshness is not guaranteed', text)
            for secret in ('private-token', 'private-key', 'private-serial', '999', 'private-query'):
                self.assertNotIn(secret, text)
        record = {'list': [{'id': 1, 'old_filename': 'model.pm4u', 'user_id': 999,
                            'preSignUrl': 'https://x/?signature=hidden', 'Cookie': 'hidden-cookie'}]}
        result, text, _, _ = self.run_cli(['file', 'list', '--verbose'], record)
        self.assertIn('/work/index/files response:', text)
        self.assertIn('"code": 1', text)
        self.assertNotIn('999', text)
        self.assertNotIn('hidden', text)

    def test_color_tty_environment_and_overrides(self):
        for tty, env, options, colored in (
                (True, {}, {}, True), (False, {}, {}, False),
                (True, {'NO_COLOR': ''}, {}, False),
                (True, {}, {'no_color': True}, False),
                (True, {}, {'json_mode': True}, False)):
            with patch.object(sys.stdout, 'isatty', return_value=tty), patch.dict(os.environ, env, clear=True):
                for label in ('ONLINE', 'OFFLINE', 'PRINTING', 'UNKNOWN', 'IDLE'):
                    self.assertEqual('\033[' in p.Output(**options).color(label), colored)
        with patch.object(p.Output, 'color', autospec=True, side_effect=lambda self, text: text) as color:
            self.run_cli(['printer', 'status', '797953', '--no-color'], self.PRINTER)
            self.assertTrue(all(call.args[0].no_color for call in color.call_args_list))

    def test_terminal_control_injection_and_json_errors(self):
        record = {**self.PRINTER, 'name': 'Mono\033[31m\nforged row'}
        result, text, _, _ = self.run_cli(['printer', 'status', '797953', '--json'], record)
        self.assertNotIn('\033', json.loads(text)['name'])
        result, text, err, _ = self.run_cli(['printer', 'status', '797953', '--json'], None, code=0)
        self.assertEqual(result, 1)
        self.assertEqual(text, '')
        self.assertIn('Error:', err)
        result, text, err, _ = self.run_cli(['printer', 'status', '797953', '--json'], {'id': 1})
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(text), {'id': 1})
        self.assertIn('does not match', err)

    def test_tables_respect_narrow_width_and_keep_ids(self):
        with patch.object(p.shutil, 'get_terminal_size', return_value=os.terminal_size((48, 24))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            p.Output().table(['ID', 'NAME', 'TYPE', 'SIZE', 'STATUS'],
                             [['12345678901234567890', 'x' * 200, 'pm4u', '1 KiB', '2']])
        self.assertIn('12345678901234567890', out.getvalue())
        self.assertTrue(all(len(line) <= 48 for line in out.getvalue().splitlines()))

    def test_json_file_failure_and_sensitive_containers(self):
        result, text, err, client = self.run_cli(['file', 'list', '--json'], None, code=0)
        self.assertEqual(result, 1)
        self.assertEqual(text, '')
        self.assertIn('Error:', err)
        client.post.assert_called_once()
        client.get.assert_not_called()
        safe = p.sanitized({'user': {'id': 999, 'name': 'private-name'},
                            'headers': {'Cookie': 'private-cookie'}, 'key': 12345}, '')
        self.assertEqual(set(safe.values()), {'[redacted]'})

    def test_upload_json_is_single_document_with_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.pm4u'
            content = b'exact original bytes'
            path.write_bytes(content)
            with patch.object(sys, 'argv', ['photonctl.py', '--verbose', 'file', 'upload', str(path), '--json']), \
                    patch.object(p, 'credentials', return_value=('private-token', 'https://api.example')), \
                    patch.object(p.requests, 'Session') as session, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                client = session.return_value.__enter__.return_value
                client.post.side_effect = [Response({'code': 1, 'data': {'id': 12,
                    'preSignUrl': 'https://storage.example/?secret=hidden'}}),
                    Response({'code': 1, 'data': {'id': 34}}), Response({'code': 1, 'data': {}})]
                def put(url, **kwargs):
                    self.assertEqual(kwargs['data'].read(), content)
                    response = Response(None)
                    response.headers = {'ETag': hashlib.md5(content).hexdigest()}
                    return response
                client.put.side_effect = put
                self.assertEqual(p.main(), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload, {'id': 34, 'name': 'model.pm4u', 'size': len(content),
                                           'md5': hashlib.md5(content).hexdigest().upper()})
                self.assertNotIn('hidden', out.getvalue())
                self.assertEqual(client.post.call_count, 3)
                client.put.assert_called_once()
                client.get.assert_not_called()
            result, text, _, client = self.run_cli(['file', 'upload', str(path), '--dry-run', '--json'], None)
            self.assertEqual(result, 0)
            self.assertTrue(json.loads(text)['dry_run'])
            client.post.assert_not_called()
            client.put.assert_not_called()


class UploadToPrintTests(unittest.TestCase):
    PRINTER = {'id': 101, 'name': 'Mock Mono', 'machine_type': 130,
               'device_status': 1, 'is_printing': 1}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'model.pm4u'
        self.content = b'original pm4u bytes\x00\xff'
        self.path.write_bytes(self.content)
        self.record = {'id': 456, 'old_filename': self.path.name, 'file_size': len(self.content),
                       'file_type': 1, 'status': 2, 'md5': hashlib.md5(self.content).hexdigest().upper()}
        guard = patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No live HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def run_workflow(self, flags=(), printer=None, record=None, enabled=False, verified=False,
                     answers=(), tty=True, dispatch_response=None, dispatch_error=None,
                     upload_error=False, get_records=None, defaults=True):
        calls, uploaded_bytes = [], []
        original_bytes = self.path.read_bytes()
        client = unittest.mock.Mock()
        selected = self.PRINTER if printer is None else printer
        library = self.record if record is None else record
        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith('/lockStorageSpace'):
                self.assertEqual(kwargs['json'], {'name': self.path.name, 'size': len(self.content), 'is_temp_file': 0})
                if upload_error:
                    return Response({'code': 0})
                return Response({'code': 1, 'data': {'id': 123,
                    'preSignUrl': 'https://storage.example/file?secret=hidden'}})
            if url.endswith('/newUploadFile'):
                self.assertEqual(kwargs['json'], {'user_lock_space_id': 123})
                return Response({'code': 1, 'data': {'id': 456}})
            if url.endswith('/unlockStorageSpace'):
                self.assertEqual(kwargs['json'], {'id': 123, 'is_delete_cos': 0})
                return Response({'code': 1, 'data': {}})
            if url.endswith('/work/index/files'):
                self.assertEqual(kwargs['json'], {'page': 1, 'limit': 10})
                return Response({'code': 1, 'data': {'list': library if isinstance(library, list) else [library], 'total': 1}})
            self.assertTrue(url.endswith(p.PRINT_ENDPOINT))
            if dispatch_error:
                raise dispatch_error
            return dispatch_response or Response({'code': 1, 'data': {'msgid': 'mock-msg', 'task_id': 987}})
        def put(url, **kwargs):
            uploaded_bytes.append(kwargs['data'].read())
            response = Response(None)
            response.headers = {'ETag': hashlib.md5(self.content).hexdigest()}
            return response
        client.post.side_effect = post
        client.put.side_effect = put
        if get_records is not None:
            client.get.side_effect = [Response({'code': 1, 'data': item}) for item in get_records]
        else:
            client.get.return_value = Response({'code': 1, 'data': selected})
        verified_prerequisites = {'file_ready_verified': True, 'options_verified': True,
                                 'routing_verified': True, 'task_settings': None,
                                 'supported_options': [], 'blockers': []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['photonctl.py', 'print', str(self.path)] + list(flags)))
            creds = stack.enter_context(patch.object(p, 'credentials', return_value=('private-token', 'https://api.example/api')))
            config = stack.enter_context(patch.object(p, 'load_config', return_value={'default_printer': {'id': '101'}} if defaults else {}))
            stack.enter_context(patch.object(p.requests, 'Session')).return_value.__enter__.return_value = client
            stack.enter_context(patch.object(p, 'LIVE_PRINT_DISPATCH_ENABLED', enabled))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=tty))
            stack.enter_context(patch.object(sys.stderr, 'isatty', return_value=tty))
            prompt = stack.enter_context(patch('builtins.input', side_effect=answers))
            if verified:
                stack.enter_context(patch.object(p, 'print_prerequisites', return_value=verified_prerequisites))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            # Mock isatty on the actual redirected stderr used by the command.
            stderr = io.StringIO()
            stderr.isatty = lambda: tty
            err = stack.enter_context(contextlib.redirect_stderr(stderr))
            code = p.main()
        text = out.getvalue()
        errors = err.getvalue()
        for secret in ('private-token', 'secret=hidden'):
            self.assertNotIn(secret, text + errors)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        return code, text, errors, client, calls, uploaded_bytes, prompt, creds, config

    def test_upload_then_registered_id_preview_exact_request(self):
        result = self.run_workflow(['--json'])
        self.assertEqual(result[0], 0)
        preview = json.loads(result[1])
        self.assertEqual(preview['file']['id'], 456)
        self.assertEqual(preview['print_order'], p.android_print_request(456, 101))
        self.assertIs(type(preview['print_order']['data']['file_id']), int)
        self.assertIsNone(preview['print_order']['data']['task_settings'])
        self.assertFalse(preview['dispatch_enabled'])
        self.assertEqual(preview['connectivity'], 'ONLINE')
        self.assertEqual(preview['activity'], 'IDLE')
        self.assertEqual(result[5], [self.content])
        self.assertEqual(result[3].get.call_count, 2)
        self.assertEqual(result[3].post.call_count, 4)
        result[6].assert_not_called()
        self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))

    def test_default_and_explicit_override(self):
        for flags in (['--dry-run'], ['--printer', '202', '--dry-run']):
            result = self.run_workflow(flags + ['--json'])
            self.assertEqual(result[0], 0)
            self.assertEqual(json.loads(result[1])['printer_id'], 202 if '--printer' in flags else 101)
            if '--printer' in flags:
                result[8].assert_not_called()
            result[3].get.assert_not_called()
            result[3].post.assert_not_called()
            result[7].assert_not_called()
        result = self.run_workflow(['--dry-run'], defaults=False)
        self.assertEqual(result[0], 1)
        self.assertIn('printer set-default', result[2])
        result[3].post.assert_not_called()

    def test_offline_dry_run_never_uploads_or_invents_file_id(self):
        result = self.run_workflow(['--dry-run', '--json'])
        preview = json.loads(result[1])
        self.assertTrue(preview['dry_run'])
        self.assertNotIn('id', preview['file'])
        self.assertIsNone(preview['print_order'])
        self.assertEqual(preview['connectivity'], 'UNKNOWN')
        result[3].post.assert_not_called()
        result[3].get.assert_not_called()
        result[3].put.assert_not_called()
        result[7].assert_not_called()

    def test_invalid_files_and_unsupported_printer_before_upload(self):
        self.path.write_bytes(b'')
        result = self.run_workflow(['--dry-run'])
        self.assertEqual(result[0], 1)
        result[3].post.assert_not_called()
        self.path.write_bytes(self.content)
        for printer in ({**self.PRINTER, 'machine_type': 131}, {**self.PRINTER, 'id': 202}, {}, []):
            result = self.run_workflow(printer=printer)
            self.assertEqual(result[0], 1)
            result[3].post.assert_not_called()
        for name in ('missing.pm4u', 'model.stl'):
            with patch.object(sys, 'argv', ['photonctl.py', 'print', str(self.path.parent / name), '--dry-run']), \
                    patch.object(p, 'credentials', side_effect=AssertionError('No authentication')), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(p.main(), 1)

    def test_source_disabled_opt_in_and_noninteractive_guards(self):
        result = self.run_workflow(['--experimental-live'])
        self.assertEqual(result[0], 1)
        self.assertIn('source-disabled', result[1])
        self.assertIn('Uploaded file remains', result[2])
        result[6].assert_not_called()
        self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))
        result = self.run_workflow(['--experimental-live'], tty=False)
        self.assertEqual(result[0], 1)
        self.assertIn('interactive', result[2])
        result[3].post.assert_not_called()
        result[3].get.assert_not_called()
        result = self.run_workflow(['--dry-run', '--experimental-live'])
        self.assertEqual(result[0], 1)
        result[3].post.assert_not_called()

    def test_online_idle_never_bypasses_unknown_prerequisites(self):
        result = self.run_workflow(['--experimental-live'], enabled=True)
        self.assertEqual(result[0], 1)
        self.assertIn('settings are unverified', result[1])
        self.assertIn('Print-ready', result[2] + result[1])
        result[6].assert_not_called()
        self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))

    def test_offline_busy_unknown_always_block_attempts(self):
        for device, printing in ((2, 1), (2, 2), (1, 2), (None, 1), (1, None)):
            result = self.run_workflow(['--experimental-live'], enabled=True, verified=True,
                                       printer={**self.PRINTER, 'device_status': device, 'is_printing': printing})
            self.assertEqual(result[0], 1)
            result[6].assert_not_called()
            self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))

    def test_upload_registration_and_file_metadata_failures(self):
        result = self.run_workflow(upload_error=True)
        self.assertEqual(result[0], 1)
        self.assertEqual(len(result[4]), 1)
        result[6].assert_not_called()
        for record in ([], {'id': 123}, {'id': 456, 'file_size': 999},
                       {'id': 456, 'old_filename': 'different.pm4u'}, {'id': 456, 'md5': 'wrong'}):
            result = self.run_workflow(record=record)
            self.assertEqual(result[0], 1)
            self.assertIn('Cloud file ID: 456', result[2])
            result[6].assert_not_called()
            self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))
        for status in (2, 3, None, 'unknown'):
            result = self.run_workflow(['--json'], record={**self.record, 'status': status})
            preview = json.loads(result[1])
            self.assertEqual(preview['library_record']['status'], status)
            self.assertTrue(preview['blockers'])  # No web/Android state equivalence is assumed.
            self.assertFalse(preview['dispatch_enabled'])

    def test_future_confirmation_no_eof_or_interrupt_never_dispatch(self):
        for answer, code in (('n', 0), ('', 0), (EOFError(), 0), (KeyboardInterrupt(), 130)):
            result = self.run_workflow(['--experimental-live', '--json'], enabled=True, verified=True, answers=[answer])
            self.assertEqual(result[0], code)
            if code == 0:
                self.assertTrue(json.loads(result[1])['cancelled'])
            self.assertEqual(result[6].call_count, 1)
            self.assertIn('Start printing? [y/N]:', result[6].call_args.args[0])
            self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))

    def test_future_affirmative_sends_once_but_does_not_claim_printer_started(self):
        for answer in ('y', 'yes', ' YES '):
            result = self.run_workflow(['--experimental-live', '--json'], enabled=True, verified=True, answers=[answer])
            self.assertEqual(result[0], 0)
            receipt = json.loads(result[1])['dispatch']
            self.assertTrue(receipt['cloud_accepted'])
            self.assertIsNone(receipt['printer_accepted'])
            self.assertEqual(receipt['acknowledgement']['task_id'], 987)
            starts = [(url, kwargs) for url, kwargs in result[4] if url.endswith(p.PRINT_ENDPOINT)]
            self.assertEqual(len(starts), 1)
            url, kwargs = starts[0]
            self.assertEqual(url, 'https://api.example/api/work/operation/sendOrder')
            self.assertEqual(json.loads(kwargs['data']), p.android_print_request(456, 101))
            self.assertEqual(kwargs['headers']['Content-Type'], 'application/json; charset=utf-8')
            self.assertEqual(kwargs['headers']['XX-Signature'],
                             p.signature(kwargs['headers']['XX-Timestamp'], kwargs['headers']['XX-Nonce']))
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(result[3].get.call_count, 3)
            self.assertIn('physical', result[2])
            self.assertIn('Printer: Mock Mono (101)', result[2])
            self.assertIn('File: model.pm4u', result[2])

    def test_future_state_change_after_confirmation_blocks_dispatch(self):
        result = self.run_workflow(['--experimental-live'], enabled=True, verified=True, answers=['y'],
            get_records=[self.PRINTER, self.PRINTER, {**self.PRINTER, 'is_printing': 2}])
        self.assertEqual(result[0], 1)
        self.assertFalse(any(url.endswith(p.PRINT_ENDPOINT) for url, _ in result[4]))

    def test_file_scan_pagination_is_bounded_and_uses_only_supported_fields(self):
        uploaded = {'id': 456, 'name': self.path.name, 'size': len(self.content), 'md5': self.record['md5']}
        with patch.object(p, 'cloud_files', side_effect=[({'list': [{'id': 999}], 'total': 20}, 'token'),
                                                        ({'list': [self.record], 'total': 20}, 'token')]) as listing:
            record, _ = p.find_uploaded_file(uploaded)
            self.assertEqual(record, self.record)
            self.assertEqual(listing.call_args_list,
                             [unittest.mock.call({'page': 1, 'limit': 10}), unittest.mock.call({'page': 2, 'limit': 10})])
        with patch.object(p, 'cloud_files', return_value=({'list': [{'id': 999}]}, 'token')) as listing:
            with self.assertRaises(p.Error):
                p.find_uploaded_file(uploaded, max_pages=2)
            self.assertEqual(listing.call_count, 2)
        with patch.object(p, 'cloud_files', return_value=({'list': [None]}, 'token')):
            with self.assertRaises(p.Error):
                p.find_uploaded_file(uploaded)


class PrintDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.printer = dict(UploadToPrintTests.PRINTER)
        self.prerequisites = {'registered': True, 'file_id': 456, 'file_ready_verified': True,
                              'options_verified': True, 'routing_verified': True,
                              'supported_options': [], 'task_settings': None}

    def dispatch(self, response=None, error=None, enabled=True, experimental=True, confirmed=True,
                 physical_ready=True, tty=True):
        with patch.object(p, 'LIVE_PRINT_DISPATCH_ENABLED', enabled), \
                patch.object(sys.stdin, 'isatty', return_value=tty), patch.object(sys.stderr, 'isatty', return_value=tty), \
                patch.object(p, 'credentials', return_value=('private-token', 'https://api.example/api')) as creds, \
                patch.object(p.requests, 'Session') as session:
            client = session.return_value.__enter__.return_value
            client.post.return_value = response or Response({'code': 1, 'data': {'msgid': 'message', 'task_id': 55}})
            client.post.side_effect = error
            try:
                result = p.dispatch_android_print(456, 101, self.printer, self.prerequisites,
                    experimental=experimental, confirmed=confirmed, physical_ready=physical_ready)
            except p.Error as exc:
                result = exc
        return result, client, creds

    def test_all_gates_prevent_http(self):
        for kwargs in ({'enabled': False}, {'experimental': False}, {'confirmed': False},
                       {'physical_ready': False}, {'tty': False}):
            result, client, creds = self.dispatch(**kwargs)
            self.assertIsInstance(result, p.Error)
            client.post.assert_not_called()
            creds.assert_not_called()
        for key in ('registered', 'file_ready_verified', 'options_verified', 'routing_verified'):
            with patch.dict(self.prerequisites, {key: False}):
                result, client, _ = self.dispatch()
                self.assertIsInstance(result, p.Error)
                client.post.assert_not_called()
        with patch.dict(self.prerequisites, {'file_id': 123}):
            result, client, _ = self.dispatch()
            self.assertIsInstance(result, p.Error)
            client.post.assert_not_called()

    def test_null_or_device_derived_complete_settings(self):
        cases = (([], None), (['mockOption'], {'mockOption': 0}),
                 (['stirBeforePrint', 'stir_settings'], {'stirBeforePrint': 1, 'stir_settings': {'stirTime': 10}}))
        for options, settings in cases:
            self.prerequisites.update(supported_options=options, task_settings=settings)
            result, client, _ = self.dispatch()
            self.assertTrue(result['cloud_accepted'])
            payload = json.loads(client.post.call_args.kwargs['data'])
            self.assertEqual(payload['data']['task_settings'], settings)
            self.assertEqual(payload, p.android_print_request(456, 101, settings, options))
            for forbidden in ('taskid', 'task_mode', 'filesize'):
                self.assertNotIn(forbidden, payload['data'])
            client.post.assert_called_once()

    def test_missing_or_unsupported_settings_never_default_to_zero(self):
        for options, settings in ((None, None), (['mockOption'], None),
                                   (['one', 'two'], {'one': 1}), ([], {'unknown': 0}),
                                   (['mockOption'], {'mockOption': True}), (['mockOption'], [{}]),
                                   ([{}], None), ([True], None)):
            self.prerequisites.update(supported_options=options, task_settings=settings)
            result, client, _ = self.dispatch()
            self.assertIsInstance(result, p.Error)
            client.post.assert_not_called()

    def test_api_failures_and_ambiguous_outcomes_never_retry(self):
        bad_json = Response(None)
        bad_json.json = unittest.mock.Mock(side_effect=ValueError('private-token'))
        http_error = Response(None)
        http_error.status_code = 503
        for response in (bad_json, http_error, Response({'code': 0, 'msg': 'private-token'}),
                         Response([]), Response({'code': True}), Response({'code': 1, 'data': None}),
                         Response({'code': 1, 'data': {'msgid': 123}}),
                         Response({'code': 1, 'data': {'task_id': True}})):
            result, client, _ = self.dispatch(response=response)
            self.assertIsInstance(result, p.Error)
            self.assertNotIn('private-token', str(result))
            client.post.assert_called_once()
        for error in (p.requests.Timeout('private-token'), p.requests.ConnectionError('private-token')):
            result, client, _ = self.dispatch(error=error)
            self.assertIsInstance(result, p.Error)
            self.assertIn('UNKNOWN', str(result))
            self.assertNotIn('private-token', str(result))
            client.post.assert_called_once()

    def test_android_api_success_codes_and_acknowledgement_are_not_execution(self):
        for code in (1, 200):
            result, client, _ = self.dispatch(response=Response({'code': code, 'data': {'task_id': 0,
                'msgid': 'private-token https://x/?secret=hidden', 'token': 'do-not-display'}}))
            self.assertTrue(result['submitted'])
            self.assertTrue(result['cloud_accepted'])
            self.assertIsNone(result['printer_accepted'])
            self.assertNotIn('private-token', json.dumps(result))
            self.assertNotIn('secret=hidden', json.dumps(result))
            self.assertNotIn('do-not-display', json.dumps(result))
            client.post.assert_called_once()


class ExistingCloudPrintTests(unittest.TestCase):
    PRINTER = {'id': 101, 'name': 'Mock Mono', 'machine_type': 130,
               'device_status': 1, 'is_printing': 1}
    FILE = {'id': 98988601, 'old_filename': 'mock.pm4u', 'file_extension': 'pm4u',
            'file_type': 1, 'status': 1, 'machine_type': 130}

    def setUp(self):
        guard = patch.object(p.requests.sessions.Session, 'request', side_effect=AssertionError('No real HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def run_cloud(self, flags=(), answer='y', printer=None, file=None, options=None, tty=True,
                  receipt=None, send_error=None, after_confirmation=None, observed=None,
                  list_data=None, list_code=1, options_code=1, poll_error=False):
        printer = dict(self.PRINTER) if printer is None else printer
        file = dict(self.FILE) if file is None else file
        options = {'list': []} if options is None else options
        events = []
        info_count = 0
        sends = []
        def get(url, **kwargs):
            nonlocal info_count
            events.append(('GET', url, kwargs))
            if url.endswith('/work/index/files'):
                self.assertEqual(kwargs['params']['printable'], 1)
                self.assertEqual(kwargs['params']['machine_type'], 130)
                data = ([file] if kwargs['params']['page'] == 1 else []) if list_data is None else list_data
                return Response({'code': list_code, 'data': data})
            self.assertTrue(url.endswith('/v2/printer/info'))
            self.assertEqual(kwargs['params'], {'id': '101'})
            info_count += 1
            if info_count >= 3 and poll_error:
                raise p.requests.Timeout('private-token https://secret.example/?hidden')
            data = (after_confirmation if info_count == 2 and after_confirmation is not None else
                    (observed if info_count >= 3 and observed is not None else printer))
            return Response({'code': 1, 'data': data})
        def post(url, **kwargs):
            events.append(('POST', url, kwargs))
            if url.endswith('/v2/device/getPrinterOptions'):
                self.assertEqual(kwargs['data'], {'model_id': 130, 'printer_id': 101})
                self.assertNotIn('json', kwargs)
                return Response({'code': options_code, 'data': options})
            self.assertTrue(url.endswith(p.PRINT_ENDPOINT))
            sends.append((url, kwargs))
            if send_error:
                raise send_error
            return receipt or Response({'code': 1, 'data': {'msgid': 'mock-msg', 'task_id': 55}})
        with patch.object(sys, 'argv', ['photonctl.py', 'printer', 'print', '98988601'] + list(flags)), \
                patch.object(p, 'credentials', return_value=('private-token', 'https://api.example/p/p/workbench/api')) as credentials, \
                patch.object(p, 'load_config', return_value={'default_printer': {'id': '101'}}) as config, \
                patch.object(sys.stdin, 'isatty', return_value=tty), \
                patch.object(p.requests, 'Session') as session, patch.object(p.time, 'sleep') as sleep, \
                patch('builtins.input', side_effect=[answer]) as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            error_stream = io.StringIO()
            error_stream.isatty = lambda: tty
            with contextlib.redirect_stderr(error_stream):
                client = session.return_value.__enter__.return_value
                client.get.side_effect = get
                client.post.side_effect = post
                result = p.main()
        for secret in ('private-token', 'hidden', 'https://secret.example'):
            self.assertNotIn(secret, out.getvalue() + error_stream.getvalue())
        client.put.assert_not_called()
        return result, out.getvalue(), error_stream.getvalue(), sends, events, prompt, credentials, config, sleep

    def test_confirmed_default_print_exact_body_and_url(self):
        result = self.run_cloud(['--json'], observed={**self.PRINTER, 'is_printing': 2})
        self.assertEqual(result[0], 0)
        data = json.loads(result[1])
        self.assertEqual(data['file_id'], 98988601)
        self.assertEqual(data['printer_id'], 101)
        self.assertTrue(data['submitted'])
        self.assertTrue(data['cloud_accepted'])
        self.assertIsNone(data['printer_accepted'])
        self.assertTrue(data['activity_observation']['idle_to_printing_observed'])
        self.assertFalse(data['activity_observation']['file_correlated'])
        self.assertEqual(data['acknowledgement'], {'msgid': 'mock-msg', 'task_id': 55})
        self.assertEqual(len(result[3]), 1)
        url, kwargs = result[3][0]
        self.assertEqual(url, 'https://api.example/p/p/workbench/api/work/operation/sendOrder')
        self.assertEqual(kwargs['headers']['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(kwargs['headers']['XX-Token'], 'private-token')
        self.assertEqual(kwargs['headers']['XX-Signature'],
                         p.signature(kwargs['headers']['XX-Timestamp'], kwargs['headers']['XX-Nonce']))
        self.assertEqual(json.loads(kwargs['data']), p.android_print_request(98988601, 101))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (15, 120))
        self.assertEqual(result[5].call_args.args, ('Start this print? [y/N]: ',))
        self.assertIn('File ID: 98988601', result[2])
        self.assertIn('cover', result[2])
        self.assertNotIn('\033', result[1])

    def test_explicit_override_and_dry_run_strictly_offline(self):
        result = self.run_cloud(['--printer', '101', '--json'])
        self.assertEqual(result[0], 0)
        result[7].assert_not_called()
        for flags in (['--dry-run'], ['--dry-run', '--printer', '101', '--json']):
            result = self.run_cloud(flags)
            self.assertEqual(result[0], 0)
            self.assertEqual(result[4], [])
            result[6].assert_not_called()
            result[5].assert_not_called()
            self.assertEqual(json.loads(result[1])['print_order'], p.android_print_request(98988601, 101))
            self.assertFalse(json.loads(result[1])['dispatch_enabled'])

    def test_default_no_and_cancel_never_send_control(self):
        for answer in ('', 'n', 'no', 'true', EOFError(), KeyboardInterrupt()):
            result = self.run_cloud(['--json'], answer=answer)
            self.assertEqual(result[0], 130 if isinstance(answer, KeyboardInterrupt) else 0)
            self.assertEqual(result[3], [])
            if not isinstance(answer, KeyboardInterrupt):
                data = json.loads(result[1])
                self.assertTrue(data['cancelled'])
                self.assertFalse(data['submitted'])
            self.assertTrue(all(p.PRINT_ENDPOINT not in event[1] for event in result[4]))

    def test_offline_busy_unknown_and_wrong_model_block(self):
        for record in ({**self.PRINTER, 'device_status': 2}, {**self.PRINTER, 'is_printing': 2},
                       {**self.PRINTER, 'is_printing': None}, {**self.PRINTER, 'device_status': True},
                       {**self.PRINTER, 'machine_type': 131}, {**self.PRINTER, 'id': 102}, {}):
            result = self.run_cloud(printer=record)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[3], [])
            result[5].assert_not_called()
            self.assertEqual(len(result[4]), 1)

    def test_file_readiness_type_extension_model_and_ownership(self):
        for field, value in (('status', 2), ('status', 3), ('status', None), ('status', True),
                             ('status', '1'), ('status', 0), ('file_type', 2),
                             ('file_extension', 'stl'), ('file_extension', None),
                             ('machine_type', 131), ('machine_type', '130'),
                             ('old_filename', 'wrong.stl'), ('id', 999)):
            result = self.run_cloud(file={**self.FILE, field: value})
            self.assertEqual(result[0], 1)
            self.assertEqual(result[3], [])
            result[5].assert_not_called()
        for invalid in ({'list': [self.FILE]}, [None], 'bad'):
            result = self.run_cloud(list_data=invalid)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[3], [])
        # A generic model marker is allowed only in the model-filtered printable result.
        result = self.run_cloud(file={**self.FILE, 'machine_type': 0}, answer='n')
        self.assertEqual(result[0], 0)
        result[5].assert_called_once()

    def test_options_nonempty_missing_malformed_block_without_control_readback(self):
        for options in ({}, {'list': None}, [], {'list': {}},
                        {'list': [{'name': 'mockOption', 'default_value': 0}]}, {'list': [None]}):
            result = self.run_cloud(options=options)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[3], [])
            result[5].assert_not_called()
            self.assertEqual([url for method, url, _ in result[4] if method == 'POST'],
                             ['https://api.example/p/p/workbench/api/v2/device/getPrinterOptions'])
        for kwargs in ({'list_code': 0}, {'options_code': 0}):
            result = self.run_cloud(**kwargs)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[3], [])
            result[5].assert_not_called()

    def test_noninteractive_stops_before_authentication_or_network(self):
        result = self.run_cloud(tty=False)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[4], [])
        result[6].assert_not_called()
        result[5].assert_not_called()

    def test_state_change_during_confirmation_prevents_start(self):
        result = self.run_cloud(after_confirmation={**self.PRINTER, 'is_printing': 2})
        self.assertEqual(result[0], 1)
        result[5].assert_called_once()
        self.assertEqual(result[3], [])

    def test_cloud_acknowledgement_not_actual_job_and_bounded_observation(self):
        result = self.run_cloud(['--json'])
        self.assertEqual(result[0], 0)
        data = json.loads(result[1])
        self.assertIsNone(data['printer_accepted'])
        self.assertEqual(data['activity_observation']['samples'], 3)
        self.assertFalse(data['activity_observation']['idle_to_printing_observed'])
        self.assertEqual(result[8].call_count, 2)
        self.assertEqual(len(result[3]), 1)
        result = self.run_cloud(['--json'], poll_error=True)
        self.assertEqual(result[0], 0)  # Accepted submission is not repeated on a failed read.
        self.assertIn('error', json.loads(result[1])['activity_observation'])
        self.assertEqual(len(result[3]), 1)

    def test_submit_errors_timeouts_and_malformed_replies_are_never_retried(self):
        malformed = Response(None)
        malformed.json = unittest.mock.Mock(side_effect=ValueError('private-token'))
        bad_http = Response(None)
        bad_http.status_code = 503
        for receipt in (malformed, bad_http, Response({'code': 0, 'msg': 'private-token'}),
                        Response({'code': 1, 'data': None}), Response({'code': '1'})):
            result = self.run_cloud(['--json'], receipt=receipt)
            self.assertEqual(result[0], 1)
            self.assertEqual(result[1], '')
            self.assertEqual(len(result[3]), 1)
        for error in (p.requests.Timeout('private-token'), p.requests.ConnectionError('private-token')):
            result = self.run_cloud(send_error=error)
            self.assertEqual(result[0], 1)
            self.assertIn('UNKNOWN', result[2])
            self.assertEqual(len(result[3]), 1)

    def test_verbose_is_sanitized_and_android_codes_supported(self):
        result = self.run_cloud(['--verbose'], list_code=200, options_code=200,
                                receipt=Response({'code': 200, 'data': {'msgid': 'private-token', 'task_id': 0}}))
        self.assertEqual(result[0], 0)
        self.assertIn('POST /work/operation/sendOrder', result[1])
        self.assertIn('"task_settings": null', result[1])
        self.assertIn('UNVERIFIED', result[1])
        self.assertEqual(len(result[3]), 1)

if __name__ == '__main__':
    unittest.main()
