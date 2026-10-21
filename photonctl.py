#!/usr/bin/env python3
"""Upload an already-sliced .pm4u unchanged to Anycubic Cloud."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import requests

A = 'f9b3528877c94d5c9c5af32245db46ef'
B = '0cf75926606049a3937f56b0373b99fb'
VERSION = '4.2.1'
SESSION_FILE = Path.home() / '.config/photonctl/session.json'
# Observed in the official site's app.982cbff9.js, not a guessed API host.
WEB_API_BASE = 'https://cloud-universe.anycubic.com/p/p/workbench/api'


class Error(Exception):
    pass


def signature(timestamp, nonce):
    return hashlib.md5((A + str(timestamp) + VERSION + B + nonce + A).encode('ascii')).hexdigest().upper()


def headers(token):
    timestamp = str(time.time_ns() // 1_000_000)
    nonce = str(uuid.uuid4())
    return {'XX-Nonce': nonce, 'XX-Timestamp': timestamp,
            'XX-Signature': signature(timestamp, nonce), 'XX-Token': token,
            'XX-Device-Type': 'pc', 'XX-LANGUAGE': 'US',
            'XX-Version': VERSION, 'XX-IS-CN': '0'}


def https_url(value, base=False):
    try:
        u = urlsplit(value)
        valid = (u.scheme == 'https' and u.hostname and not u.username
                 and not u.password and not u.fragment and (not base or not u.query))
        u.port  # Validate malformed ports too.
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise Error('Expected an HTTPS URL without embedded credentials' +
                    (' or query parameters.' if base else '.'))
    return value.rstrip('/') if base else value


def credentials():
    token, base = os.getenv('PHOTONCTL_TOKEN'), os.getenv('PHOTONCTL_API_BASE')
    if token is not None or base is not None:
        if not token or not base:
            raise Error('Set both PHOTONCTL_TOKEN and PHOTONCTL_API_BASE.')
    else:
        try:
            with SESSION_FILE.open() as f:
                if stat.S_IMODE(os.fstat(f.fileno()).st_mode) != 0o600:
                    raise Error('Session file must have permissions 0600; run chmod 600 on it.')
                saved = json.load(f)
            token, base = saved['token'], saved['api_base']
        except (OSError, ValueError, KeyError, TypeError):
            raise Error('No usable session. Set PHOTONCTL_TOKEN and PHOTONCTL_API_BASE, then run login.') from None
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise Error('Invalid session token format.')
    return token, https_url(base, base=True)


def login():
    # Import only. Password/OAuth automation is deliberately unavailable until verified.
    if not os.getenv('PHOTONCTL_TOKEN') or not os.getenv('PHOTONCTL_API_BASE'):
        raise Error('Email/password login is not verified. Sign in at https://cloud-universe.anycubic.com/file; '
                    'use its actual XX-Token session header (not a Makeronline JWT), and the API base '
                    'observed in Network. Set PHOTONCTL_TOKEN and PHOTONCTL_API_BASE, then rerun login.')
    token, base = credentials()
    SESSION_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.session-', dir=SESSION_FILE.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump({'token': token, 'api_base': base}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, SESSION_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print('Session imported with permissions 0600. Not validated with the server.')


def sanitized(value, token, key=''):
    """Keep diagnostic structure and metadata; omit credentials and URL fields."""
    normalized = re.sub(r'[^a-z0-9]', '', key.lower())
    if any(word in normalized for word in
           ('token', 'password', 'secret', 'signature', 'authorization', 'cookie',
            'url', 'uri', 'filekey', 'credential', 'accesskey')):
        return '[redacted]'
    if isinstance(value, dict):
        return {k: sanitized(v, token, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitized(v, token, key) for v in value]
    if isinstance(value, str):
        # Only recognized metadata strings are printable. Unknown fields retain
        # their keys for investigating an unfamiliar response schema.
        allowed = {'code', 'msg', 'message', 'id', 'name', 'filename', 'originalfilename',
                   'originfilename', 'originalname', 'originname', 'filetype', 'type',
                   'status', 'filestatus', 'size', 'filesize', 'page', 'limit',
                   'total', 'count', 'machinetype', 'machineid', 'orderby',
                   'fileid', 'cloudfileid', 'userlockspaceid', 'officialfileid',
                   'created', 'updated', 'modified', 'date', 'fileext', 'extension',
                   'oldfilename', 'fileextension', 'machinename', 'md5'}
        if normalized not in allowed and not normalized.endswith(('time', 'timestamp', 'at')):
            return '[redacted: unrecognized text field]'
        value = value.replace(token, '[redacted]') if token else value
        value = re.sub(r'(?i)(?:https?://|//)[^\s<>"\']+', '[redacted URL]', value)
        value = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?',
                       '[redacted JWT]', value)
        value = re.sub(r'(?i)\b(token|password|secret|authorization|signature)\s*[:=]\s*\S+',
                       r'\1=[redacted]', value)
        return value
    return value


def api_post(client, base, token, endpoint, payload, verbose=False):
    try:
        response = client.post(base + endpoint, headers=headers(token), json=payload,
                               timeout=(15, 120), allow_redirects=False)
    except requests.RequestException:
        raise Error('API transport failure; response outcome may be unknown.') from None
    if not 200 <= response.status_code < 300:
        raise Error('API returned HTTP %d.' % response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise Error('API returned invalid JSON.') from None
    if verbose:
        print(endpoint + ' response:')
        print(json.dumps(sanitized(body, token), indent=2, ensure_ascii=True))
    if not isinstance(body, dict) or type(body.get('code')) is not int or body['code'] != 1:
        # Never echo a server message: it may include a token or a signed URL.
        code = body.get('code') if isinstance(body, dict) else None
        safe_code = str(code) if type(code) is int else 'unknown'
        raise Error('API rejected request (code %s). Check session, region and API base.' % safe_code)
    return body.get('data')


def files(filters):
    token, base = credentials()
    payload = {'page': 1, 'limit': 10, **filters}
    print('Listing request: ' + json.dumps(payload, sort_keys=True))
    print('This is one response page, not necessarily the entire library. '
          'Optional filter semantics remain unverified.')
    with requests.Session() as client:
        # POST is the documented read-only listing method. Never probe write routes.
        api_post(client, base, token, '/work/index/files', payload, verbose=True)
    print('Metadata is shown under the server\'s original field names. Missing fields '
          'are not inferred; unrecognized text and URL/credential fields are redacted. '
          'An empty page alone does not prove the upload was not registered.')


def fingerprint(f):
    f.seek(0)
    digest = hashlib.md5()
    size = 0
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
        digest.update(chunk)
        size += len(chunk)
    f.seek(0)
    return size, digest.hexdigest().upper()


def upload(path, dry_run=False, verbose=False):
    path = Path(path)
    if path.suffix.lower() != '.pm4u':
        raise Error('Input must be an already-sliced .pm4u file.')
    with path.open('rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Error('Input must be a regular file.')
        size, md5 = fingerprint(f)
        if size == 0:
            raise Error('Input file is empty.')
        print('Size: %d bytes; MD5: %s' % (size, md5))
        if dry_run:
            print('Dry run: extension, nonempty regular file and checksum checked; no network traffic. '
                  'Slice format internals are not validated.')
            return
        token, base = credentials()
        with requests.Session() as api:
            reservation = api_post(api, base, token, '/v2/cloud_storage/lockStorageSpace',
                                   {'is_temp_file': 0, 'size': size, 'name': path.name}, verbose=verbose)
            if not isinstance(reservation, dict):
                raise Error('Invalid reservation response.')
            rid = reservation.get('id')
            if not ((type(rid) is int and rid > 0) or
                    (isinstance(rid, str) and rid.isdecimal() and int(rid) > 0)):
                raise Error('Missing or invalid reservation ID.')
            url = https_url(reservation.get('preSignUrl'))
            print('Cloud space reserved. Uploading original bytes.')
            # Separate client prevents API cookies/headers reaching storage. No retries.
            try:
                with requests.Session() as storage:
                    response = storage.put(url, data=f, headers={'Content-Length': str(size), 'Expect': ''},
                                           timeout=(15, 600), allow_redirects=False)
            except requests.RequestException:
                raise Error('Storage upload failed; reservation remains unconfirmed. No automatic retry.') from None
            if not 200 <= response.status_code < 300:
                raise Error('Storage returned HTTP %d; reservation remains unconfirmed.' % response.status_code)
            etag = response.headers.get('ETag', '').strip().strip('"')
            if re.fullmatch(r'[0-9a-fA-F]{32}', etag):
                if etag.upper() != md5:
                    raise Error('Storage ETag MD5 mismatch; not confirming upload.')
                print('Storage ETag matches MD5.')
            else:
                print('Storage supplied no single-part MD5 ETag; checksum comparison unavailable.')
            if fingerprint(f) != (size, md5):
                raise Error('Local file changed during upload; not confirming.')
            try:
                registered = api_post(api, base, token, '/v2/profile/newUploadFile',
                                      {'user_lock_space_id': rid}, verbose=verbose)
            except Error as exc:
                raise Error(str(exc) + ' Confirmation was attempted once. Check the Android app before retrying; '
                            'it may already have succeeded.') from None
            file_id = registered.get('id') if isinstance(registered, dict) else None
            valid_id = ((type(file_id) is int and file_id > 0) or
                        (isinstance(file_id, str) and file_id.isdecimal() and int(file_id) > 0))
            if valid_id:
                print('Registered cloud file ID: %s' % file_id)
            # Release the reservation only after successful confirmation, even
            # if its response omitted the new ID. Never delete the storage object.
            try:
                api_post(api, base, token, '/v2/cloud_storage/unlockStorageSpace',
                         {'id': rid, 'is_delete_cos': 0}, verbose=verbose)
            except Error as exc:
                raise Error(str(exc) + ' Confirmation succeeded, but reservation release failed. '
                            'Do not repeat confirmation or upload blindly; check the cloud library.') from None
            if not valid_id:
                raise Error('Confirmation succeeded and reservation was released, but data.id was missing or invalid. '
                            'Check the cloud library before uploading again.')
    print('Upload registered and reservation released. Check cloud files; print readiness is not verified. '
          'No print was started.')


def api_get_printers(endpoint, params=None):
    """Only the two source-backed read endpoints; no control dispatch here."""
    if endpoint not in ('/work/printer/getPrinters', '/v2/printer/info'):
        raise Error('Unsupported printer read endpoint.')
    token, base = credentials()
    try:
        with requests.Session() as client:
            response = client.get(base + endpoint, params=params, headers=headers(token),
                                  timeout=(15, 120), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise Error('Printer API returned HTTP %d.' % response.status_code)
        body = response.json()
    except requests.RequestException:
        raise Error('Printer read request failed.') from None
    except ValueError:
        raise Error('Printer API returned invalid JSON.') from None
    if not isinstance(body, dict) or type(body.get('code')) is not int or body['code'] != 1:
        raise Error('Printer API rejected the read request; check session and API base.')
    return body.get('data'), token


def printer_metadata(value, token):
    """Restrict output to discovery/state fields, including numeric values."""
    fields = {'id', 'printer_id', 'name', 'printer_name', 'model', 'model_id',
              'machine_type', 'machine_name', 'status', 'device_status', 'online',
              'is_online', 'is_printing', 'ready_status', 'firmware_version',
              'last_update_time'}
    containers = {'list', 'printers', 'printer', 'info', 'data', 'version', 'items'}
    if isinstance(value, list):
        return [printer_metadata(item, token) for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return None
    result = {}
    for key, item in value.items():
        if key in fields and (item is None or type(item) in (str, int, float, bool)):
            # Reuse existing string scrubbing under its recognized 'name' key.
            result[key] = sanitized(item, token, 'name')
        elif key in containers and isinstance(item, (dict, list)):
            result[key] = printer_metadata(item, token)
    return result


def printers(printer_id=None):
    endpoint = '/work/printer/getPrinters' if printer_id is None else '/v2/printer/info'
    data, token = api_get_printers(endpoint, None if printer_id is None else {'id': printer_id})
    print(json.dumps(printer_metadata(data, token), indent=2, ensure_ascii=True))
    print('Read-only cloud snapshot. Raw status values are not interpreted as print readiness. '
          'Fields absent from the response are not inferred.')


def printer_status(printer_id):
    """Report a single GET snapshot; never probe the printer with control orders."""
    endpoint = '/v2/printer/info'
    print('Status source: GET ' + endpoint)
    try:
        data, token = api_get_printers(endpoint, {'id': printer_id})
    except Error as exc:
        print('Connectivity: UNKNOWN')
        print('Reason: ' + str(exc) + ' No usable cloud status was obtained. '
              'If the session expired, reimport it with login; no automatic retry or refresh was attempted.')
        return 1
    print('Raw discovery/status metadata (credentials and unrelated fields omitted):')
    print(json.dumps(printer_metadata(data, token), indent=2, ensure_ascii=True))
    if data is None or data == {} or data == []:
        print('Connectivity: UNKNOWN\nReason: No printer record was returned; absence does not establish offline.')
        return 1
    if not isinstance(data, dict):
        print('Connectivity: UNKNOWN\nReason: Malformed printer-info data; expected a printer object.')
        return 1
    returned_id = data.get('id')
    if not ((type(returned_id) is int and returned_id > 0) or
            (type(returned_id) is str and re.fullmatch(r'[0-9]{1,10}', returned_id))):
        print('Connectivity: UNKNOWN\nReason: Printer identity is missing or malformed.')
        return 1
    if int(returned_id) != int(printer_id):
        print('Connectivity: UNKNOWN\nReason: Returned printer ID does not match the requested printer.')
        return 1
    device_status = data.get('device_status')
    machine_type = data.get('machine_type')
    # LCDPrinterDetailsActivity.p2/j2 use this endpoint's device_status for
    # offline UI. Restrict interpretation to known numeric 1/2, not future codes.
    if type(machine_type) is not int or not 0 < machine_type < 10000:
        print('Connectivity: UNKNOWN\nReason: The returned model is missing or outside the APK LCD semantics verified here.')
    elif type(device_status) is int and device_status in (1, 2):
        state = 'ONLINE' if device_status == 1 else 'OFFLINE'
        print('Connectivity: ' + state + ' (cloud-reported)')
        print('Reason: /v2/printer/info device_status=%d; based on the APK LCD connectivity UI.' % device_status)
    else:
        print('Connectivity: UNKNOWN\nReason: device_status is missing, malformed or unrecognized for this endpoint.')
    print('Cloud snapshot freshness is not guaranteed. is_printing is not used to infer connectivity; '
          'this does not verify physical power, readiness or print acceptance.')
    return 0


def android_task_settings(task_settings, supported_options=None):
    """Validate shape against explicit server-derived supported option names.

    This offline check cannot verify provenance, freshness or device ranges.
    Callers must supply names/values from the selected printer's options/readback.
    """
    if task_settings is None:
        return None
    if type(task_settings) is not dict:
        raise Error('task_settings must be null or a dictionary.')
    if not task_settings:
        return None  # Android M0 serializes an empty settings map as null.
    if (not isinstance(supported_options, (set, frozenset, list, tuple)) or
            any(type(name) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
                for name in supported_options)):
        raise Error('Nonempty task_settings require server-derived supported option names.')
    result = {}
    for name, value in task_settings.items():
        if type(name) is not str or name not in supported_options:
            raise Error('task_settings contains an option not supported by the supplied server metadata.')
        if name == 'stir_settings':
            if (type(value) is not dict or set(value) != {'stirTime'} or
                    type(value['stirTime']) is not int or not 1 <= value['stirTime'] <= 2147483647 or
                    task_settings.get('stirBeforePrint') != 1):
                raise Error('stir_settings requires integer stirTime and enabled stirBeforePrint.')
            result[name] = dict(value)
        elif type(value) is not int or not -2147483648 <= value <= 2147483647:
            raise Error('Android task option values must be integers, not booleans or strings.')
        else:
            result[name] = value
    if 'stirBeforePrint' in result:
        if result['stirBeforePrint'] not in (0, 1):
            raise Error('stirBeforePrint must be 0 or 1.')
        if result['stirBeforePrint'] == 1 and 'stir_settings' not in result:
            raise Error('Enabled stirBeforePrint requires server-derived stir_settings.')
    return result


def android_print_request(file_id, printer_id, task_settings=None, supported_options=None):
    """Pure APK-derived LCD cloud-slice constructor; no dispatch or live checks."""
    ids = []
    for value in (file_id, printer_id):
        if not (type(value) is int or (type(value) is str and re.fullmatch(r'[0-9]{1,10}', value))):
            raise Error('Android file and printer IDs must be positive 32-bit integers.')
        number = int(value)
        if not 1 <= number <= 2147483647:
            raise Error('Android file and printer IDs must be positive 32-bit integers.')
        ids.append(number)
    return {
        'printer_id': ids[1], 'order_id': 1, 'project_id': 0,
        'data': {
            'file_id': ids[0], 'filename': '', 'filepath': '/', 'project_type': 1,
            'template_id': 0, 'plate_id': 0, 'is_delete_file': 0, 'matrix': '',
            'slice_size': None, 'slice_param': None, 'hollow_param': None,
            'punching_param': None,
            'task_settings': android_task_settings(task_settings, supported_options),
            'filetype': 0, 'file_key': '', 'file_name': '', 'official_file_id': '',
            'official_file_type': -1,
        },
        'settings': None, 'ams_info': None,
    }


def print_dry_run(file_id, printer_id, task_settings=None, supported_options=None):
    preview = {
        'ready_to_send': False,
        'dispatch_enabled': False,
        'method': 'POST', 'endpoint': '/work/operation/sendOrder',
        'content_type': 'application/json; charset=utf-8',
        'note': 'Offline Android APK-derived constructor only. Nulls are explicit Android defaults. '
                'Null task_settings does not establish that this printer has no options. '
                'No IDs, capabilities or option provenance have been verified live.',
        'print_order': android_print_request(file_id, printer_id, task_settings, supported_options),
        'unverified': ['model-130 supported options, current values and valid ranges',
                       'printer model, online/idle state, readiness and selected file readiness',
                       'Android JSON acceptance and routing through the configured cloud proxy',
                       'msgid/task_id API acknowledgement vs actual matching printer/task acceptance'],
    }
    print(json.dumps(preview, indent=2, ensure_ascii=True))


def positive_id(value):
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise argparse.ArgumentTypeError('Expected a positive decimal ID.')
    return value


def add_cli_command(subparsers, name, command, help_text=None, replacement=None):
    """Configure each operation once for both canonical and legacy parsers."""
    options = {}
    if help_text is not None:
        options.update(help=help_text, description=help_text)
    if replacement:
        # Omitting help keeps the alias out of argparse's choices help table.
        options['description'] = "Deprecated alias; use '" + replacement + "'."
    parser = subparsers.add_parser(name, **options)
    parser.set_defaults(command=command)
    if replacement:
        parser.set_defaults(deprecated_alias=(name, replacement))
    if command in ('printer-info', 'printer-status'):
        parser.add_argument('printer_id', type=positive_id, help='unique cloud printer ID')
    elif command == 'print':
        parser.add_argument('file_id', type=positive_id, help='registered cloud file ID')
        parser.add_argument('--printer', required=True, type=positive_id, help='unique cloud printer ID')
        parser.add_argument('--dry-run', action='store_true', dest='print_dry_run',
                            help='preview Android JSON offline; required because dispatch is disabled')
    elif command == 'upload':
        parser.add_argument('file', metavar='path', help='already-sliced .pm4u file')
        parser.add_argument('--dry-run', action='store_true', dest='upload_dry_run',
                            help='validate and hash the local file without network traffic')
        parser.add_argument('--verbose', action='store_true',
                            help='show sanitized reservation/confirmation/release JSON')
    elif command == 'files':
        for field, default in (('page', 1), ('limit', 10)):
            parser.add_argument('--' + field, type=int, default=default,
                                help='pagination value (default: %(default)s)')
        for field in ('file_type', 'orderby', 'machine_type'):
            parser.add_argument('--' + field.replace('_', '-'), dest=field,
                                help='explicit API value; omitted by default (semantics unverified)')
    return parser


def build_parser():
    parser = argparse.ArgumentParser(description='Manage Anycubic cloud files and inspect printers.')
    parser.add_argument('--dry-run', action='store_true',
                        help='offline preview/local validation where supported (also accepted on upload/print)')
    sub = parser.add_subparsers(dest='resource', required=True, metavar='{login,file,printer}')
    add_cli_command(sub, 'login', 'login', 'securely cache an existing QCloud XX-Token from environment')
    file_parser = sub.add_parser('file', help='upload and list cloud files',
                                 description='Upload already-sliced files or inspect the cloud library.')
    file_sub = file_parser.add_subparsers(dest='action', required=True)
    add_cli_command(file_sub, 'upload', 'upload', 'upload exact .pm4u bytes; never print')
    add_cli_command(file_sub, 'list', 'files', 'read-only cloud library listing (one page)')
    printer_parser = sub.add_parser('printer', help='inspect printers and preview a print request',
                                    description='Read-only printer discovery and offline print-request preview.')
    printer_sub = printer_parser.add_subparsers(dest='action', required=True)
    add_cli_command(printer_sub, 'list', 'printers', 'read-only registered printer discovery')
    add_cli_command(printer_sub, 'info', 'printer-info', 'read-only cloud printer information')
    add_cli_command(printer_sub, 'status', 'printer-status', 'GET-only cloud connectivity snapshot')
    add_cli_command(printer_sub, 'print', 'print', 'offline Android JSON preview only; real dispatch disabled')
    for alias, replacement in (('upload', 'file upload'), ('files', 'file list'),
                               ('printers', 'printer list'), ('printer-info', 'printer info'),
                               ('printer-status', 'printer status'), ('print', 'printer print')):
        add_cli_command(sub, alias, alias, replacement=replacement)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, 'deprecated_alias', None):
        alias, replacement = args.deprecated_alias
        print("Warning: '%s' is deprecated; use '%s'." % (alias, replacement), file=sys.stderr)
    try:
        if args.command == 'print':
            if not (args.dry_run or args.print_dry_run):
                raise Error('Only print --dry-run is implemented. No printer-control requests are supported.')
            print_dry_run(args.file_id, args.printer)
        elif args.command == 'printer-status':
            if args.dry_run:
                raise Error('printer-status performs a read-only GET; --dry-run is not applicable.')
            return printer_status(args.printer_id)
        elif args.command in ('printers', 'printer-info'):
            if args.dry_run:
                raise Error('Use print --dry-run for an offline request template.')
            printers(args.printer_id if args.command == 'printer-info' else None)
        elif args.command == 'login':
            if args.dry_run:
                raise Error('--dry-run applies to upload only.')
            login()
        elif args.command == 'files':
            if args.dry_run:
                raise Error('--dry-run applies to upload only.')
            if args.page < 1 or args.limit < 1:
                raise Error('--page and --limit must be positive integers.')
            filters = {'page': args.page, 'limit': args.limit}
            for field in ('file_type', 'orderby', 'machine_type'):
                value = getattr(args, field)
                if value is not None:
                    # Numeric CLI values become JSON integers; symbolic values
                    # remain strings. No guessed defaults or automatic probes.
                    filters[field] = int(value) if re.fullmatch(r'-?\d+', value) else value
            files(filters)
        else:
            upload(args.file, args.dry_run or args.upload_dry_run, args.verbose)
    except (Error, OSError) as exc:
        # OSError filenames and request exception strings can contain sensitive URLs.
        print('Error: ' + (str(exc) if isinstance(exc, Error) else 'Local file or session I/O failed.'), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. If confirmation was in progress, check the app before retrying.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
