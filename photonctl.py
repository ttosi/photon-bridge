#!/usr/bin/env python3
"""Upload an already-sliced .pm4u unchanged to Anycubic Cloud."""
import argparse
import contextlib
import difflib
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
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
CONFIG_FILE = Path.home() / '.config/photonctl/config.json'
# Observed in the official site's app.982cbff9.js, not a guessed API host.
WEB_API_BASE = 'https://cloud-universe.anycubic.com/p/p/workbench/api'
# The local upload workflow remains source-disabled while its web-file prerequisites are unverified.
LIVE_PRINT_DISPATCH_ENABLED = False
# Existing-cloud-file experiments are enabled only after full preflight and interactive consent.
EXPERIMENTAL_CLOUD_PRINT_ENABLED = True
PRINT_ENDPOINT = '/work/operation/sendOrder'


class Error(Exception):
    pass


class Output:
    """Terminal-only presentation state, scoped to a single CLI invocation."""
    def __init__(self, verbose=False, json_mode=False, no_color=False):
        self.verbose, self.json_mode, self.no_color = verbose, json_mode, no_color

    def color(self, text):
        codes = {'ONLINE': '32', 'OFFLINE': '31', 'PRINTING': '33', 'UNKNOWN': '2', 'IDLE': '2', 'Error:': '31'}
        if (text in codes and not self.json_mode and not self.no_color
                and 'NO_COLOR' not in os.environ and sys.stdout.isatty()):
            return '\033[' + codes[text] + 'm' + text + '\033[0m'
        return text

    def json(self, data):
        print(json.dumps(data, indent=2, ensure_ascii=True))

    def detail(self, text):
        if self.verbose and not self.json_mode:
            print(text)

    def diagnostic(self, source, data):
        if not self.json_mode:
            print(source + ' response:')
            self.json(data)

    def table(self, headings, rows, color_columns=()):
        # Bound every column so a long filename cannot consume the terminal.
        width = max(40, shutil.get_terminal_size((100, 24)).columns)
        caps = [20] * len(headings)
        caps[0] = max(len(terminal_text(row[0])) for row in [headings] + rows)
        caps[1] = max(12, min(48, width - 10 * (len(headings) - 1)))
        def cell(value, cap):
            text = terminal_text(value)
            return text if len(text) <= cap else text[:cap - 1] + '…'
        cells = [[cell(v, caps[i]) for i, v in enumerate(row)] for row in [headings] + rows]
        widths = [max(len(row[i]) for row in cells) for i in range(len(headings))]
        name_width = max(len(headings[1]), width - sum(w for i, w in enumerate(widths) if i != 1) - 2 * (len(headings) - 1))
        if widths[1] > name_width:
            for row in cells:
                row[1] = cell(row[1], name_width)
            widths[1] = name_width
        for row_index, row in enumerate(cells):
            print('  '.join((self.color(value) if row_index and i in color_columns else value) + ' ' * (widths[i] - len(value))
                            for i, value in enumerate(row)).rstrip())


_OUTPUT = ContextVar('photonctl_output', default=None)


def output():
    return _OUTPUT.get() or Output()


def terminal_text(value):
    """Prevent API/config strings from injecting terminal controls or extra rows."""
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def human_size(value):
    if type(value) is not int or value < 0:
        return '-'
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if amount < 1024 or unit == 'TiB':
            return ('%d B' % value) if unit == 'B' else ('%.1f %s' % (amount, unit))
        amount /= 1024


def first_field(record, *keys, default='-'):
    for key in keys:
        if record.get(key) is not None and record[key] != '':
            return record[key]
    return default


def listing_records(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('list', 'items', 'printers'):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def printer_view(data, status=True):
    """Use info endpoint semantics only; discovery lists do not establish states."""
    out = output()
    if not isinstance(data, dict):
        print('No printer details returned.')
        return
    print(terminal_text(first_field(data, 'name', 'printer_name', 'machine_name', 'model', default='Printer')))
    print('ID: ' + terminal_text(data.get('id', '-')))
    if not status:
        print('Model: ' + terminal_text(first_field(data, 'machine_name', 'model')))
    connectivity, activity, _, _ = interpret_printer_status(data)
    print('Status: ' + out.color(connectivity))
    if status:
        print('Activity: ' + out.color(activity))
    version = data.get('version')
    firmware = data.get('firmware_version') or (version.get('firmware_version') if isinstance(version, dict) else None)
    if firmware is not None:
        print('Firmware: ' + terminal_text(firmware))


def render_files(data):
    records = listing_records(data)
    if records is None:
        print('Unrecognized file listing; use --verbose or --json to inspect metadata.')
        return
    if not records:
        print('No cloud files on this page.')
        return
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        rows.append([first_field(record, 'id', 'file_id'),
                     first_field(record, 'old_filename', 'original_filename', 'filename', 'name'),
                     first_field(record, 'file_extension', 'file_type', 'type'),
                     human_size(first_field(record, 'file_size', 'size', 'filesize', default=None)),
                     first_field(record, 'status', 'file_status', default='UNKNOWN')])
    output().table(['ID', 'NAME', 'TYPE', 'SIZE', 'STATUS'], rows)


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
    if normalized in {'user', 'owner', 'uid', 'ownerid', 'key', 'devicekey', 'mqtt',
                       'headers', 'requestheaders', 'responseheaders', 'query', 'params',
                       'path', 'filepath', 'storage'}:
        return '[redacted]'
    if any(word in normalized for word in
           ('token', 'password', 'secret', 'signature', 'authorization', 'cookie',
            'url', 'uri', 'filekey', 'credential', 'accesskey', 'userid', 'account',
            'email', 'phone', 'serial', 'storagepath', 'queryparameter')):
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
        value = re.sub(r'(?i)\b(token|password|secret|authorization|signature|cookie)\s*[:=]\s*\S+',
                       r'\1=[redacted]', value)
        return terminal_text(value)
    return value


def api_post(client, base, token, endpoint, payload, verbose=False, *, form=False, success_codes=(1,)):
    try:
        body_args = {'data': payload} if form else {'json': payload}
        response = client.post(base + endpoint, headers=headers(token), **body_args,
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
        output().diagnostic(endpoint, sanitized(body, token))
    if not isinstance(body, dict) or type(body.get('code')) is not int or body['code'] not in success_codes:
        # Never echo a server message: it may include a token or a signed URL.
        code = body.get('code') if isinstance(body, dict) else None
        safe_code = str(code) if type(code) is int else 'unknown'
        raise Error('API rejected request (code %s). Check session, region and API base.' % safe_code)
    return body.get('data')


def cloud_files(filters):
    """The existing web-library read, shared by listing and upload verification."""
    token, base = credentials()
    payload = {'page': 1, 'limit': 10, **filters}
    with requests.Session() as client:
        data = api_post(client, base, token, '/work/index/files', payload,
                        verbose=output().verbose and not output().json_mode)
    return data, token


def files(filters):
    out = output()
    payload = {'page': 1, 'limit': 10, **filters}
    out.detail('Listing request: ' + json.dumps(payload, sort_keys=True))
    data, token = cloud_files(payload)
    safe = sanitized(data, token)
    if out.json_mode:
        out.json(safe)
    else:
        render_files(safe)
        out.detail('One response page; filter and numeric file-status semantics remain unverified. '
                   'Missing metadata is not inferred. An empty page does not prove a file was never registered.')


def fingerprint(f):
    f.seek(0)
    digest = hashlib.md5()
    size = 0
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
        digest.update(chunk)
        size += len(chunk)
    f.seek(0)
    return size, digest.hexdigest().upper()


@contextlib.contextmanager
def open_slice(path):
    """Validate and fingerprint the exact local bytes; never slice or convert."""
    path = Path(path)
    if path.suffix.lower() != '.pm4u':
        raise Error('Input must be an already-sliced .pm4u file.')
    with path.open('rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Error('Input must be a regular file.')
        size, md5 = fingerprint(f)
        if size == 0:
            raise Error('Input file is empty.')
        yield f, size, md5


def upload(path, dry_run=False, verbose=False, *, emit=True):
    out = output()
    verbose = verbose or out.verbose
    path = Path(path)
    with open_slice(path) as (f, size, md5):
        if verbose and not out.json_mode:
            print('Size: %d bytes; MD5: %s' % (size, md5))
        if dry_run:
            if not emit:
                return {'name': path.name, 'size': size, 'md5': md5, 'dry_run': True}
            if out.json_mode:
                out.json({'name': path.name, 'size': size, 'md5': md5, 'dry_run': True})
            else:
                print('Validated: ' + terminal_text(path.name) + ' (' + human_size(size) + ')')
                print('MD5: ' + md5)
                print('Dry run: no network traffic; slice internals are not validated.')
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
            out.detail('Cloud space reserved. Uploading original bytes.')
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
                out.detail('Storage ETag matches MD5.')
            else:
                out.detail('Storage supplied no single-part MD5 ETag; checksum comparison unavailable.')
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
                if verbose and not out.json_mode:
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
    result = {'id': file_id, 'name': path.name, 'size': size, 'md5': md5}
    if not emit:
        return result
    if out.json_mode:
        out.json(result)
    else:
        print('Uploaded: ' + terminal_text(path.name))
        print('Cloud file ID: %s' % file_id)
        out.detail('Upload registered and reservation released. Print readiness is not verified. No print was started.')
    return result


def api_get_printers(endpoint, params=None, *, success_codes=(1,), timeout=(15, 120)):
    """Source-backed printer reads and Android cloud-file GET; never control dispatch."""
    if endpoint not in ('/work/printer/getPrinters', '/v2/printer/info', '/work/index/files'):
        raise Error('Unsupported printer read endpoint.')
    token, base = credentials()
    try:
        with requests.Session() as client:
            response = client.get(base + endpoint, params=params, headers=headers(token),
                                  timeout=timeout, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise Error('Printer API returned HTTP %d.' % response.status_code)
        body = response.json()
    except requests.RequestException:
        raise Error('Printer read request failed.') from None
    except ValueError:
        raise Error('Printer API returned invalid JSON.') from None
    if not isinstance(body, dict) or type(body.get('code')) is not int or body['code'] not in success_codes:
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
    out = output()
    safe = printer_metadata(data, token)
    default = None
    if printer_id is None:
        try:
            default = load_config().get('default_printer')
            if default:
                records = printer_records(data)
                if not any(record['id'] == default['id'] for record in records):
                    print('Configured default printer ' + default['id'] + ' is not in the returned list. '
                          'Run photonctl printer set-default to reselect; configuration was not changed.', file=sys.stderr)
        except (Error, OSError):
            print('Could not check the configured default; inspect photonctl printer default.', file=sys.stderr)
    if out.json_mode:
        out.json(safe)
        return
    if printer_id is not None:
        printer_view(safe, status=False)
    else:
        records = listing_records(safe)
        if records is None:
            print('Unrecognized printer listing; use --verbose or --json to inspect metadata.')
        elif not records:
            print('No registered printers.')
        else:
            rows = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                marker = ' *' if default and str(record.get('id')) == default['id'] else ''
                # getPrinters flags have no independently verified endpoint semantics.
                rows.append([str(record.get('id', '-')) + marker,
                             first_field(record, 'name', 'printer_name', 'machine_name', 'model'),
                             'UNKNOWN', 'UNKNOWN'])
            out.table(['ID', 'PRINTER', 'STATUS', 'ACTIVITY'], rows, color_columns=(2, 3))
    if out.verbose:
        print('Source: GET ' + endpoint)
        out.json(safe)
        print('Cloud snapshot; freshness/readiness not guaranteed. List flags have no verified activity semantics. '
              '* marks the locally configured default.')


def config_printer_id(value):
    if not ((type(value) is int and value > 0) or
            (type(value) is str and re.fullmatch(r'[0-9]{1,20}', value) and int(value) > 0)):
        raise Error('Invalid printer ID in configuration or printer-list response.')
    return str(int(value))


def load_config():
    try:
        with CONFIG_FILE.open() as f:
            if stat.S_IMODE(os.fstat(f.fileno()).st_mode) != 0o600:
                raise Error('Config file must have permissions 0600; run chmod 600 on ~/.config/photonctl/config.json.')
            config = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise Error('Invalid JSON in ~/.config/photonctl/config.json; repair it before selecting a default.') from None
    if not isinstance(config, dict):
        raise Error('Config must be a JSON object.')
    default = config.get('default_printer')
    if default is not None:
        if not isinstance(default, dict):
            raise Error('Invalid default_printer configuration; expected an object with an ID.')
        default['id'] = config_printer_id(default.get('id'))
    return config


def resolve_printer_id(explicit_id):
    if explicit_id is not None:
        return explicit_id
    default = load_config().get('default_printer')
    if not default:
        raise Error('No default printer configured. Run photonctl printer set-default or supply an explicit printer ID.')
    return default['id']


def show_default_printer():
    default = load_config().get('default_printer')
    if not default:
        raise Error('No default printer configured. Run photonctl printer set-default.')
    # Local configuration only; do not read credentials or validate online.
    safe = {'id': default['id']}
    for key in ('name', 'model'):
        if type(default.get(key)) is str:
            safe[key] = default[key]
    safe = printer_metadata(safe, '')
    out = output()
    if out.json_mode:
        out.json(safe)
    else:
        print('Default printer: ' + terminal_text(first_field(safe, 'name', 'model', default='Printer')) +
              ' (' + terminal_text(safe['id']) + ')')
        if out.verbose:
            out.json(safe)
            print('Configured default (not validated online). Source: ~/.config/photonctl/config.json')


def printer_records(data):
    if isinstance(data, dict):
        for key in ('list', 'printers', 'items'):
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        raise Error('Unrecognized printer-list response; no default was changed.')
    records, seen = [], set()
    for record in data:
        if not isinstance(record, dict):
            raise Error('Malformed printer-list record; no default was changed.')
        printer_id = config_printer_id(record.get('id'))
        if printer_id in seen:
            raise Error('Duplicate printer IDs in response; no default was changed.')
        seen.add(printer_id)
        records.append({**record, 'id': printer_id})
    return records


def save_default_printer(printer):
    config = load_config()
    config['default_printer'] = {'id': config_printer_id(printer.get('id'))}
    for key in ('name', 'model'):
        if type(printer.get(key)) is str:
            config['default_printer'][key] = printer[key]
    CONFIG_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_FILE.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix='.config-', dir=CONFIG_FILE.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(config, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, CONFIG_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def set_default_printer():
    if not sys.stdin.isatty():
        raise Error('printer set-default requires interactive stdin; run it in a terminal. No printer was selected.')
    default = load_config().get('default_printer')
    data, token = api_get_printers('/work/printer/getPrinters')
    records = printer_records(data)
    if default and not any(record['id'] == default['id'] for record in records):
        print('Configured default ' + default['id'] + ' is not in the returned list. Select a replacement; '
              'cancel to keep the configuration unchanged.')
    if not records:
        raise Error('No registered printers were returned. No default was changed.')
    print('Select a default printer (current default marked *):')
    choices = []
    for index, record in enumerate(records, 1):
        choice = {'id': record['id']}
        for key in ('name', 'model'):
            if type(record.get(key)) is str:
                choice[key] = sanitized(record[key], token, 'name')
        choices.append(choice)
        marker = '*' if default and record['id'] == default['id'] else ' '
        name = terminal_text(first_field(choice, 'name', 'model', default='Printer'))
        print('%s%d) %s (%s)' % (marker, index, name, choice['id']))
    try:
        while True:
            answer = input('Printer number (Enter/q to cancel): ').strip()
            if answer.lower() in ('', 'q', 'cancel'):
                print('Cancelled; default unchanged.')
                return 0
            if re.fullmatch(r'[0-9]{1,9}', answer) and 1 <= int(answer) <= len(choices):
                choice = choices[int(answer) - 1]
                break
            print('Enter a number from 1 to %d, or q to cancel.' % len(choices))
    except EOFError:
        print('\nCancelled; default unchanged.')
        return 0
    except KeyboardInterrupt:
        print('\nCancelled; default unchanged.')
        return 130
    save_default_printer(choice)
    name = terminal_text(first_field(choice, 'name', 'model', default='Printer'))
    print('Default printer set to ' + name + '.')
    return 0


def interpret_printer_status(data):
    """Interpret this endpoint's strict numeric flags; activity is verified only for model 130."""
    machine_type = data.get('machine_type')
    device_status = data.get('device_status')
    connectivity = 'UNKNOWN'
    reason = 'device_status is missing, malformed or unrecognized for this endpoint.'
    if type(machine_type) is not int or not 0 < machine_type < 10000:
        reason = 'The returned model is missing or outside the APK LCD semantics verified here.'
    elif type(device_status) is int and device_status in (1, 2):
        connectivity = 'ONLINE' if device_status == 1 else 'OFFLINE'
        reason = '/v2/printer/info device_status=%d; based on the APK LCD connectivity UI.' % device_status
    activity = 'UNKNOWN'
    activity_reason = 'activity semantics are not established for this model'
    if connectivity == 'OFFLINE':
        activity_reason = 'last activity may be stale'
    elif connectivity != 'ONLINE':
        activity_reason = 'connectivity is unknown'
    elif machine_type == 130:
        value = data.get('is_printing')
        if type(value) is int and value in (1, 2):
            activity = 'IDLE' if value == 1 else 'PRINTING'
            activity_reason = None
        else:
            activity_reason = 'is_printing is missing, malformed or unrecognized'
    return connectivity, activity, reason, activity_reason


def printer_status(printer_id):
    """Report a single GET snapshot; never probe the printer with control orders."""
    endpoint = '/v2/printer/info'
    out = output()
    out.detail('Status source: GET ' + endpoint)
    try:
        data, token = api_get_printers(endpoint, {'id': printer_id})
    except Error as exc:
        if not out.json_mode:
            print(('Connectivity: ' if out.verbose else 'Status: ') + out.color('UNKNOWN') + '\nActivity: ' + out.color('UNKNOWN'))
        print('Error: ' + str(exc) + ' If the session expired, reimport it with login.', file=sys.stderr)
        return 1
    safe = printer_metadata(data, token)
    if out.json_mode:
        out.json(safe)
    elif out.verbose:
        print('Raw discovery/status metadata (credentials and unrelated fields omitted):')
        out.json(safe)
    if data is None or data == {} or data == []:
        print('Error: No printer record was returned; absence does not establish offline.', file=sys.stderr)
        if not out.json_mode:
            print(('Connectivity: ' if out.verbose else 'Status: ') + 'UNKNOWN\nActivity: UNKNOWN')
        return 1
    if not isinstance(data, dict):
        print('Error: Malformed printer-info data; expected a printer object.', file=sys.stderr)
        if not out.json_mode:
            print(('Connectivity: ' if out.verbose else 'Status: ') + 'UNKNOWN\nActivity: UNKNOWN')
        return 1
    returned_id = data.get('id')
    if not ((type(returned_id) is int and returned_id > 0) or
            (type(returned_id) is str and re.fullmatch(r'[0-9]{1,10}', returned_id))):
        print('Error: Printer identity is missing or malformed.', file=sys.stderr)
        if not out.json_mode:
            print(('Connectivity: ' if out.verbose else 'Status: ') + 'UNKNOWN\nActivity: UNKNOWN')
        return 1
    if int(returned_id) != int(printer_id):
        print('Error: Returned printer ID does not match the requested printer.', file=sys.stderr)
        if not out.json_mode:
            print(('Connectivity: ' if out.verbose else 'Status: ') + 'UNKNOWN\nActivity: UNKNOWN')
        return 1
    connectivity, activity, reason, activity_reason = interpret_printer_status(data)
    if not out.json_mode:
        printer_view(safe)
    out.detail('Connectivity: ' + connectivity + (' (cloud-reported)' if connectivity != 'UNKNOWN' else ''))
    out.detail('Activity: ' + activity + (' (' + activity_reason + ')' if activity_reason else ''))
    out.detail('Reason: ' + reason)
    out.detail('Cloud snapshot freshness is not guaranteed. Activity is not readiness or proof of a specific job; '
          'this does not verify physical power or print acceptance.')
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


def workflow_printer_info(printer_id):
    data, token = api_get_printers('/v2/printer/info', {'id': printer_id})
    if not isinstance(data, dict):
        raise Error('No usable printer-info object; no print can be prepared.')
    if config_printer_id(data.get('id')) != config_printer_id(printer_id):
        raise Error('Returned printer ID does not match the selected printer.')
    if type(data.get('machine_type')) is not int or data['machine_type'] != 130:
        raise Error('This workflow currently supports only verified machine_type 130 (Photon Mono 4 Ultra).')
    return data, token


def find_uploaded_file(uploaded, max_pages=10):
    """Bounded scan using only established page/limit fields, never guessed ID filters."""
    for page in range(1, max_pages + 1):
        data, token = cloud_files({'page': page, 'limit': 10})
        records = listing_records(data)
        if records is None or any(not isinstance(record, dict) for record in records):
            raise Error('Malformed cloud library response. Upload remains registered; do not upload again blindly.')
        matches = [record for record in records
                   if type(record.get('id')) in (int, str) and str(record['id']) == str(uploaded['id'])]
        if len(matches) > 1:
            raise Error('Duplicate cloud file ID in library response; print blocked.')
        if matches:
            record = matches[0]
            for key in ('old_filename', 'original_filename'):
                if key in record and record[key] != uploaded['name']:
                    raise Error('Registered file name does not match the uploaded file; print blocked.')
            for key in ('size', 'file_size', 'filesize'):
                if key in record and (type(record[key]) is not int or record[key] != uploaded['size']):
                    raise Error('Registered file size is missing/malformed or mismatched; print blocked.')
            if record.get('md5') is not None:
                if type(record['md5']) is not str or record['md5'].upper() != uploaded['md5']:
                    raise Error('Registered file checksum differs from the uploaded bytes; print blocked.')
            return record, token
        if not records:
            break
        if isinstance(data, dict) and type(data.get('total')) is int and page * 10 >= data['total']:
            break
    raise Error('Uploaded file ID was not found in the bounded library scan. '
                'Visibility may be delayed; the upload may already be registered. Check file list; do not retry blindly.')


def print_prerequisites(printer, file_record):
    """Explicit evidence gaps, not defaults inferred from ONLINE/IDLE or library presence."""
    return {
        'file_ready_verified': False,
        'options_verified': False,
        'routing_verified': False,
        'task_settings': None,
        'supported_options': None,
        'blockers': [
            'Print-ready slice semantics for the web-library record are unverified (registration alone is insufficient).',
            'Model-130 supported options and current settings are unverified; null settings are preview-only.',
            'PC-session acceptance of Android JSON through the configured proxy is unverified (APK routing is established).',
        ],
    }


def dispatch_android_print(file_id, printer_id, printer, prerequisites, *, experimental=False,
                           confirmed=False, physical_ready=False, existing_cloud_file=False):
    """One-shot transport, gated by preflight, interactive consent and command scope.

    This function is exercised with a patched enable gate and mocked HTTP only.
    Never calls read-like orders, retries or falls back to another transport.
    """
    enabled = EXPERIMENTAL_CLOUD_PRINT_ENABLED if existing_cloud_file is True else LIVE_PRINT_DISPATCH_ENABLED
    if not enabled:
        raise Error('Live print dispatch is source-disabled for this workflow.')
    if experimental is not True or confirmed is not True or physical_ready is not True:
        raise Error('Experimental opt-in, final confirmation and operator physical-readiness confirmation are required.')
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Error('Live dispatch requires an interactive terminal for input and confirmation.')
    if (not isinstance(printer, dict) or config_printer_id(printer.get('id')) != config_printer_id(printer_id)
            or type(printer.get('machine_type')) is not int or printer['machine_type'] != 130
            or interpret_printer_status(printer)[:2] != ('ONLINE', 'IDLE')):
        raise Error('Live dispatch requires the selected model-130 printer to be cloud-reported ONLINE and IDLE.')
    for key in ('registered', 'file_ready_verified', 'options_verified', 'routing_verified'):
        if prerequisites.get(key) is not True:
            raise Error('Live dispatch prerequisite not verified: ' + key + '.')
    if str(prerequisites.get('file_id')) != str(file_id):
        raise Error('Preflight file ID does not match the intended registered file.')
    options = prerequisites.get('supported_options')
    settings = prerequisites.get('task_settings')
    if (not isinstance(options, (set, frozenset, list, tuple)) or
            any(type(name) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in options)):
        raise Error('Verified supported options are required, including an explicit empty collection when none exist.')
    settings = android_task_settings(settings, options)
    if options and not settings:
        raise Error('Supported printer options require device-derived task settings; missing settings cannot default to zero.')
    if options and set(options) - {'stir_settings'} - set(settings):
        raise Error('Missing device-derived values for supported task settings; print blocked.')
    payload = android_print_request(file_id, printer_id, settings, options)
    token, base = credentials()
    request_headers = headers(token)
    request_headers['Content-Type'] = 'application/json; charset=utf-8'
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    try:
        with requests.Session() as client:
            response = client.post(base + PRINT_ENDPOINT, headers=request_headers, data=encoded,
                                   timeout=(15, 120), allow_redirects=False)
    except requests.RequestException:
        raise Error('Print-start submission outcome UNKNOWN after transport failure. Never retry automatically; '
                    'inspect the printer and official app before any further attempt.') from None
    if not 200 <= response.status_code < 300:
        raise Error('Print-start returned HTTP %d; printer acceptance is UNKNOWN. Do not retry blindly.' % response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise Error('Print-start returned malformed JSON; outcome UNKNOWN. Do not retry blindly.') from None
    if not isinstance(body, dict) or type(body.get('code')) is not int:
        raise Error('Print-start response malformed; outcome UNKNOWN. Do not retry blindly.')
    if body['code'] not in (1, 200):
        raise Error('Cloud rejected print-start (HTTP %d, code %d); device acceptance remains unverified. Do not retry blindly.' % (response.status_code, body['code']))
    data = body.get('data')
    if not isinstance(data, dict):
        raise Error('Cloud success lacked acknowledgement data; outcome UNKNOWN. Do not retry blindly.')
    acknowledgement = {}
    if 'msgid' in data:
        if type(data['msgid']) is not str:
            raise Error('Malformed print acknowledgement msgid; outcome UNKNOWN. Do not retry blindly.')
        acknowledgement['msgid'] = sanitized(data['msgid'], token, 'id')
    if 'task_id' in data:
        if type(data['task_id']) is not int or data['task_id'] < 0:
            raise Error('Malformed print acknowledgement task_id; outcome UNKNOWN. Do not retry blindly.')
        acknowledgement['task_id'] = data['task_id']
    return {'submitted': True, 'http_status': response.status_code, 'cloud_accepted': True, 'api_code': body['code'],
            'acknowledgement': acknowledgement, 'printer_accepted': None,
            'note': 'Cloud acknowledgement only. Intended printer/file execution remains unverified; no correlated polling is established.'}


def android_cloud_file(file_id, machine_type=130, max_pages=10):
    """GET the same model-filtered printable library used by Android's cloud tab."""
    for page in range(1, max_pages + 1):
        data, token = api_get_printers('/work/index/files',
            {'page': page, 'limit': 10, 'printable': 1, 'machine_type': machine_type}, success_codes=(1, 200))
        # Retrofit d1.a.f returns ArrayList<FileResponse>, not the web POST wrapper.
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise Error('Malformed Android cloud-file listing; print blocked.')
        matches = [item for item in data if type(item.get('id')) in (int, str) and str(item['id']) == str(file_id)]
        if len(matches) > 1:
            raise Error('Duplicate cloud file identity; print blocked.')
        if matches:
            record = matches[0]
            if type(record.get('file_type')) is not int or record['file_type'] != 1:
                raise Error('Selected cloud file is not a confirmed slice (Android file_type=1 required).')
            if type(record.get('status')) is not int or record['status'] != 1:
                raise Error('Selected cloud file is pending, failed or has unverified readiness (Android status=1 required).')
            extension = record.get('file_extension')
            if type(extension) is not str or extension.lower().lstrip('.') != 'pm4u':
                raise Error('Selected cloud file is not a verified .pm4u slice.')
            for key in ('old_filename', 'original_filename'):
                if key in record and (type(record[key]) is not str or Path(record[key]).suffix.lower() != '.pm4u'):
                    raise Error('Cloud filename and .pm4u extension disagree; print blocked.')
            model = record.get('machine_type')
            if model is not None and (type(model) is not int or model not in (0, machine_type)):
                raise Error('Selected cloud file metadata is incompatible with model 130.')
            return record, token
        if not data:
            break
    raise Error("Cloud file not found in the account's bounded model-130 printable listing. "
                'Check ownership, file readiness and model compatibility; no print was sent.')


def cloud_print_options(printer_id):
    """Only the APK-confirmed read-only form query; never send order 1228/1225."""
    token, base = credentials()
    with requests.Session() as client:
        data = api_post(client, base, token, '/v2/device/getPrinterOptions',
                        {'model_id': 130, 'printer_id': int(printer_id)},
                        verbose=output().verbose, form=True, success_codes=(1, 200))
    if not isinstance(data, dict) or type(data.get('list')) is not list:
        raise Error('Printer options response is missing an explicit options list; null settings are not verified.')
    if data['list']:
        raise Error('Printer exposes device-specific options. Current values require Android device readback; '
                    'no control readback was sent. Cannot safely construct task_settings; print blocked.')
    return {'options_verified': True, 'supported_options': [], 'task_settings': None}


def poll_print_activity(printer_id, attempts=3):
    """Bounded info GET observations; activity alone never identifies a file/task."""
    observation = {'connectivity': 'UNKNOWN', 'activity': 'UNKNOWN', 'samples': 0,
                   'idle_to_printing_observed': False, 'file_correlated': False}
    for index in range(attempts):
        if index:
            time.sleep(2)
        try:
            data, _ = api_get_printers('/v2/printer/info', {'id': printer_id}, timeout=(5, 10))
            if (not isinstance(data, dict) or config_printer_id(data.get('id')) != config_printer_id(printer_id)
                    or type(data.get('machine_type')) is not int or data['machine_type'] != 130):
                raise Error('Unusable activity observation.')
            connectivity, activity, _, _ = interpret_printer_status(data)
        except Error:
            observation['error'] = 'Post-submission status unavailable; inspect the physical printer and official app.'
            break
        observation.update(connectivity=connectivity, activity=activity, samples=index + 1,
                           idle_to_printing_observed=activity == 'PRINTING')
        if activity == 'PRINTING' or connectivity != 'ONLINE':
            break
    return observation


def print_cloud_file(file_id, printer_id):
    """Experimental existing-cloud-file start: read, validate, ask, send at most once."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise Error('Live print requires interactive stdin and stderr; use --dry-run for an offline preview.')
    if not EXPERIMENTAL_CLOUD_PRINT_ENABLED:
        raise Error('Experimental cloud-file dispatch is disabled.')
    # Validate IDs without a control request.
    android_print_request(file_id, printer_id)
    printer, printer_token = workflow_printer_info(printer_id)
    if interpret_printer_status(printer)[:2] != ('ONLINE', 'IDLE'):
        raise Error('Print blocked: selected printer must be cloud-reported ONLINE and IDLE.')
    record, token = android_cloud_file(file_id)
    prerequisites = cloud_print_options(printer_id)
    prerequisites.update(registered=True, file_id=file_id, file_ready_verified=True, routing_verified=True)
    payload = android_print_request(file_id, printer_id, prerequisites['task_settings'], prerequisites['supported_options'])
    out = output()
    result = {'printer_id': int(printer_id), 'file_id': int(file_id),
              'printer': printer_metadata(printer, printer_token), 'file': sanitized(record, token),
              'print_order': payload, 'submitted': False, 'printer_accepted': None}
    # Summary and prompt use stderr so JSON stdout remains one document.
    name = first_field(result['printer'], 'name', 'machine_name', 'model', default='Printer')
    print('Experimental cloud-file print start', file=sys.stderr)
    print('Printer: ' + terminal_text(name) + ' (' + str(printer_id) + ')', file=sys.stderr)
    print('File ID: ' + str(file_id), file=sys.stderr)
    print('Connectivity: ONLINE\nActivity: IDLE', file=sys.stderr)
    print('Check resin, vat, platform, cover and other physical interlocks. Do not bypass printer safeguards. '
          'Yes confirms your physical checks and this experimental submission; cloud acceptance is not proof of a start.', file=sys.stderr)
    if out.verbose and not out.json_mode:
        print('POST ' + PRINT_ENDPOINT + ' (application/json; charset=utf-8)')
        out.json(result)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            answer = input('Start this print? [y/N]: ').strip().lower()
    except EOFError:
        answer = ''
    if answer not in ('y', 'yes'):
        result['cancelled'] = True
        if out.json_mode:
            out.json(result)
        else:
            print('Cancelled. No print-start request sent.')
        return 0
    # The operator may have spent time checking physical setup; do not use cached activity.
    printer, _ = workflow_printer_info(printer_id)
    receipt = dispatch_android_print(file_id, printer_id, printer, prerequisites,
        experimental=True, confirmed=True, physical_ready=True, existing_cloud_file=True)
    if not out.json_mode:
        print('Print-start submitted. Cloud accepted (code %d).' % receipt['api_code'])
        for key, value in receipt['acknowledgement'].items():
            print(key + ': ' + terminal_text(value))
        print('Printer start is UNVERIFIED. Observing activity only; do not resend.')
    receipt['activity_observation'] = poll_print_activity(printer_id)
    result.update(receipt)
    if out.json_mode:
        out.json(result)
    else:
        observation = receipt['activity_observation']
        print('Observed activity: ' + out.color(observation['activity']) + ' (not file/task-correlated).')
        if observation.get('error'):
            print(observation['error'], file=sys.stderr)
        if out.verbose:
            out.json(result)
    return 0


def render_print_workflow(result):
    out = output()
    if out.json_mode:
        out.json(result)
        return
    print('Printer: ' + terminal_text(result.get('printer_name', 'Printer')) + ' (' + str(result['printer_id']) + ')')
    print('Status: ' + out.color(result['connectivity']))
    print('Activity: ' + out.color(result['activity']))
    print('File: ' + terminal_text(result['file']['name']))
    if result['file'].get('id') is not None:
        print('Uploaded. Cloud file ID: ' + str(result['file']['id']))
    else:
        print('Offline dry run: no upload, cloud lookup or printer-control requests.')
    print('Dispatch: ' + ('cloud acknowledged; printer acceptance UNVERIFIED' if result.get('dispatch') else 'NOT SENT'))
    for blocker in result['blockers']:
        print('Blocked: ' + blocker)
    if out.verbose:
        out.json(result)


def upload_to_print(path, printer_id=None, dry_run=False, experimental_live=False):
    out = output()
    if dry_run and experimental_live:
        raise Error('--dry-run and --experimental-live cannot be combined.')
    # Validate before any cloud operations; then reuse the uploader's validation on its open handle.
    with open_slice(path) as (_, size, md5):
        local = {'name': Path(path).name, 'size': size, 'md5': md5}
    printer_id = resolve_printer_id(printer_id)
    # Validate the Android integer domain without constructing a fake cloud-file request.
    if int(printer_id) > 2147483647:
        raise Error('Android printer IDs must be positive 32-bit integers.')
    if experimental_live and (not sys.stdin.isatty() or not sys.stderr.isatty()):
        raise Error('--experimental-live requires an interactive terminal; no upload was attempted.')
    if dry_run:
        result = {'dry_run': True, 'file': local, 'printer_id': int(printer_id),
                  'connectivity': 'UNKNOWN', 'activity': 'UNKNOWN', 'print_order': None,
                  'dispatch_enabled': False, 'ready_to_send': False, 'blockers': ['A real cloud-library ID is required to build the Android request.',
                      'Printer model, current state, file readiness, options and proxy routing are not verified offline.']}
        render_print_workflow(result)
        return 0
    workflow_printer_info(printer_id)  # Reject a mismatched/unsupported model before uploading bytes.
    if not out.json_mode:
        print('Uploading ' + terminal_text(local['name']) + '...')
    uploaded = upload(path, emit=False)
    # Only the permanent newUploadFile ID may enter the constructor, never the reservation ID.
    try:
        record, token = find_uploaded_file(uploaded)
        printer, printer_token = workflow_printer_info(printer_id)  # Fresh state after a potentially long upload.
        connectivity, activity, _, _ = interpret_printer_status(printer)
        prerequisites = print_prerequisites(printer, record)
        prerequisites.update(registered=True, file_id=uploaded['id'])
        payload = android_print_request(uploaded['id'], printer_id,
                                        prerequisites.get('task_settings'), prerequisites.get('supported_options'))
    except (Error, OSError):
        print('Upload completed. Cloud file ID: %s. Print was not sent; retain this file and inspect the library.' % uploaded['id'], file=sys.stderr)
        raise
    blockers = list(prerequisites['blockers'])
    if (connectivity, activity) != ('ONLINE', 'IDLE'):
        blockers.append('Actual dispatch requires cloud-reported ONLINE + IDLE; current activity is not sufficient.')
    if not LIVE_PRINT_DISPATCH_ENABLED:
        blockers.append('Live dispatch is source-disabled; --experimental-live cannot override unresolved evidence gaps.')
    result = {'dry_run': False, 'file': uploaded, 'printer_id': int(printer_id),
              'printer_name': first_field(printer_metadata(printer, printer_token), 'name', 'machine_name', 'model', default='Printer'),
              'connectivity': connectivity, 'activity': activity,
              'library_record': sanitized(record, token),
              'printer_snapshot': printer_metadata(printer, printer_token),
              'method': 'POST', 'endpoint': PRINT_ENDPOINT, 'content_type': 'application/json; charset=utf-8',
              'print_order': payload,
              'dispatch_enabled': LIVE_PRINT_DISPATCH_ENABLED, 'ready_to_send': not blockers, 'blockers': blockers}
    if not experimental_live:
        render_print_workflow(result)
        return 0
    if blockers:
        render_print_workflow(result)
        raise Error('Live print blocked. Uploaded file remains in the library; resolve the reported prerequisites before a controlled test.')
    # This branch is unreachable in this release; tests patch the gate and verified prerequisites.
    if out.json_mode:
        context = _OUTPUT.set(Output(no_color=out.no_color))
        try:
            with contextlib.redirect_stdout(sys.stderr):
                render_print_workflow(result)
        finally:
            _OUTPUT.reset(context)
    else:
        render_print_workflow(result)
    print('Check physical printer readiness, including vat/resin, platform, clear work area and printer interlocks. '
          'ONLINE + IDLE does not establish readiness. Yes confirms your physical checks.', file=sys.stderr)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            answer = input('Start printing? [y/N]: ').strip().lower()
    except EOFError:
        answer = ''
    if answer not in ('y', 'yes'):
        result['cancelled'] = True
        if out.json_mode:
            out.json(result)
        else:
            print('Cancelled. Uploaded file remains in the cloud library.')
        return 0
    printer, _ = workflow_printer_info(printer_id)  # Recheck state after the operator's confirmation delay.
    result['dispatch'] = dispatch_android_print(uploaded['id'], printer_id, printer, prerequisites,
                                                experimental=True, confirmed=True, physical_ready=True)
    render_print_workflow(result)
    return 0


def positive_id(value):
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise argparse.ArgumentTypeError('Expected a positive decimal ID.')
    return value


def add_output_options(parser, root=False):
    for flag, description in (
            ('--verbose', 'show sanitized JSON, API sources and technical details'),
            ('--json', 'emit sanitized JSON only on stdout (overrides --verbose)'),
            ('--no-color', 'disable ANSI colors (also disabled by NO_COLOR or non-TTY stdout)')):
        parser.add_argument(flag, action='store_true', default=False if root else argparse.SUPPRESS,
                            help=description)


def add_cli_command(subparsers, name, command, help_text=None, replacement=None):
    """Configure each operation once for both canonical and legacy parsers."""
    options = {'formatter_class': argparse.RawDescriptionHelpFormatter}
    if help_text is not None:
        options.update(help=help_text, description=help_text)
    if replacement:
        # Omitting help keeps the alias out of argparse's choices help table.
        options['description'] = "Deprecated alias; use '" + replacement + "'."
    parser = subparsers.add_parser(name, **options)
    parser.set_defaults(command=command)
    if command in ('upload', 'files', 'printers', 'printer-info', 'printer-status', 'printer-default', 'local-print', 'print'):
        add_output_options(parser)
    if replacement:
        parser.set_defaults(deprecated_alias=(name, replacement))
    if command == 'local-print':
        parser.add_argument('file', metavar='path', help='already-sliced local .pm4u file; exact bytes are uploaded')
        parser.add_argument('--printer', type=positive_id, help='cloud printer ID (otherwise saved default)')
        parser.add_argument('--dry-run', action='store_true', dest='local_dry_run', help='fully offline validation; no upload or cloud requests')
        parser.add_argument('--experimental-live', action='store_true', help='request controlled live dispatch (currently source-disabled)')
        parser.description += ('\nUploads and previews by default. --dry-run is fully offline. Live dispatch remains source-disabled '
                               'until options, file readiness and proxy acceptance are verified; opt-in alone cannot bypass these gates.')
        parser.epilog = 'Example: ' + parser.prog + ' model.pm4u --dry-run'
    elif command in ('printer-info', 'printer-status'):
        parser.add_argument('printer_id', nargs='?', type=positive_id, help='cloud printer ID (otherwise saved default)')
        parser.description += ('\nOmit the ID to use the saved default. An explicit ID overrides it. '
                               'If none is configured, run printer set-default; no printer is chosen automatically.')
        parser.epilog = 'Example: ' + parser.prog + '\n         ' + parser.prog + ' <printer-id>'
    elif command == 'print':
        parser.add_argument('file_id', type=positive_id, help='registered cloud file ID')
        parser.add_argument('--printer', type=positive_id, help='cloud printer ID (otherwise saved default)')
        parser.add_argument('--dry-run', action='store_true', dest='print_dry_run',
                            help='preview Android JSON strictly offline; never contact the cloud or printer')
        parser.description += ('\nExperimental live start without --dry-run: requires validated file/options, ONLINE + IDLE, and interactive confirmation. '
                               'Omit --printer to use the saved default. --dry-run is strictly offline and does not verify readiness.')
        parser.epilog = ('Example: ' + parser.prog + ' <file-id>  (interactive experimental start)\n'
                         '         ' + parser.prog + ' <file-id> --dry-run\n'
                         '         ' + parser.prog + ' <file-id> --printer <printer-id> --dry-run')
    elif command == 'upload':
        parser.add_argument('file', metavar='path', help='already-sliced .pm4u file')
        parser.add_argument('--dry-run', action='store_true', dest='upload_dry_run',
                            help='validate and hash the local file without network traffic')
        parser.epilog = 'Example: ' + parser.prog + ' model.pm4u --dry-run'
    elif command == 'files':
        for field, default in (('page', 1), ('limit', 10)):
            parser.add_argument('--' + field, type=int, default=default,
                                help='pagination value (default: %(default)s)')
        for field in ('file_type', 'orderby', 'machine_type'):
            parser.add_argument('--' + field.replace('_', '-'), dest=field,
                                help='explicit API value; omitted by default (semantics unverified)')
        parser.epilog = 'Example: ' + parser.prog + ' --page 2 --limit 10'
    elif command == 'printer-set-default':
        parser.description += ('\nRequires interactive stdin. Fetches printers, shows a numbered menu, and saves '
                               'the selected printer immediately. Enter/q cancels. Saves config.json locally '
                               'with permissions 0600; it sends no printer-control requests.')
        parser.epilog = 'Example: ' + parser.prog
    elif command == 'printer-default':
        parser.description += '\nReads ~/.config/photonctl/config.json only; does not authenticate or contact the cloud.'
        parser.epilog = 'Example: ' + parser.prog
    elif command == 'printers':
        parser.description += '\nTables mark the local default with *. Stale-default warnings go to stderr.'
        parser.epilog = 'Example: ' + parser.prog
    elif command == 'login':
        parser.description += ('\nImports PHOTONCTL_TOKEN and PHOTONCTL_API_BASE into a 0600 session file. '
                               'Native email/password login is not implemented. Never put tokens in CLI arguments.')
        parser.epilog = 'Example: ' + parser.prog + '  (with both environment variables set securely)'
    return parser


def build_parser():
    parser = argparse.ArgumentParser(description='Manage Anycubic cloud files, inspect printers, and preview print JSON offline.',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-v', '--version', action='version', version='0.0.1')
    add_output_options(parser, root=True)
    parser.epilog = ('Help: ' + parser.prog + ' help [command [subcommand ...]]\n'
                     'Example: ' + parser.prog + ' help printer print\n'
                     'Use printer set-default to select a printer. Printer print is experimental; --dry-run stays offline.')
    parser.add_argument('--dry-run', action='store_true',
                        help='offline preview/local validation where supported (also accepted on upload/print)')
    sub = parser.add_subparsers(dest='resource', required=True, metavar='{login,file,printer,print,help}')
    help_parser = sub.add_parser('help', help='show help for any command path',
                                 description='Show native argparse help without reading configuration or contacting the cloud.')
    help_parser.set_defaults(command='help')
    help_parser.add_argument('help_path', nargs='*', metavar='command', help='command path, e.g. printer status')
    add_cli_command(sub, 'print', 'local-print', 'upload a local .pm4u and prepare an Android print preview')
    add_cli_command(sub, 'login', 'login', 'securely cache an existing QCloud XX-Token from environment')
    file_parser = sub.add_parser('file', help='upload and list cloud files',
                                 description='Upload already-sliced files or inspect the cloud library.')
    add_output_options(file_parser)
    file_sub = file_parser.add_subparsers(dest='action', required=True)
    add_cli_command(file_sub, 'upload', 'upload', 'upload exact .pm4u bytes; never print')
    add_cli_command(file_sub, 'list', 'files', 'read-only cloud library listing (one page)')
    printer_parser = sub.add_parser('printer', help='inspect printers and experimentally start cloud-file prints',
                                    description='Inspect printers or preview print JSON offline. Use set-default to select a printer; '
                                                'info/status and print use it when an explicit ID is omitted. Printer print requires explicit confirmation unless --dry-run.')
    add_output_options(printer_parser)
    printer_sub = printer_parser.add_subparsers(dest='action', required=True)
    add_cli_command(printer_sub, 'list', 'printers', 'read-only registered printer discovery')
    add_cli_command(printer_sub, 'set-default', 'printer-set-default', 'interactively select and save a default printer')
    add_cli_command(printer_sub, 'default', 'printer-default', 'display the locally configured default (no API call)')
    add_cli_command(printer_sub, 'info', 'printer-info', 'read-only cloud printer information')
    add_cli_command(printer_sub, 'status', 'printer-status', 'GET-only cloud connectivity and activity snapshot')
    add_cli_command(printer_sub, 'print', 'print', 'experimental confirmed cloud-file print start, or offline --dry-run')
    for alias, replacement in (('upload', 'file upload'), ('files', 'file list'),
                               ('printers', 'printer list'), ('printer-info', 'printer info'),
                               ('printer-status', 'printer status')):
        add_cli_command(sub, alias, alias, replacement=replacement)
    return parser


def resolve_help_parser(parser, path):
    """Walk the actual argparse tree at any depth, including hidden aliases."""
    for target in path:
        # argparse has no public subparser lookup API. Keep its internal access
        # in one place; help content remains owned by the parser definitions.
        choices = {name: child for action in parser._actions
                   if isinstance(action, argparse._SubParsersAction)
                   for name, child in action.choices.items()}
        if target not in choices:
            visible = [name for name, child in choices.items() if not child.get_default('deprecated_alias')]
            message = "Unknown help target '%s' under '%s'." % (target, parser.prog)
            if visible:
                close = difflib.get_close_matches(target, visible, n=1, cutoff=0.6)
                if close:
                    message += " Did you mean '%s'?" % close[0]
                message += ' Available commands: ' + ', '.join(visible) + '.'
            else:
                message += ' This command has no subcommands; omit positional values and options from the help path.'
            parser.error(message)
        parser = choices[target]
    return parser


def _main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'help':
        resolve_help_parser(parser, args.help_path).print_help()
        return 0
    _OUTPUT.set(Output(args.verbose, args.json, args.no_color))
    if args.json and args.command in ('login', 'printer-set-default'):
        parser.error('--json is not supported for this interactive/session command.')
    if getattr(args, 'deprecated_alias', None):
        alias, replacement = args.deprecated_alias
        print("Warning: '%s' is deprecated; use '%s'." % (alias, replacement), file=sys.stderr)
    try:
        if args.command == 'local-print':
            return upload_to_print(args.file, args.printer, args.dry_run or args.local_dry_run, args.experimental_live)
        elif args.command == 'print':
            printer_id = resolve_printer_id(args.printer)
            if args.dry_run or args.print_dry_run:
                print_dry_run(args.file_id, printer_id)
            else:
                return print_cloud_file(args.file_id, printer_id)
        elif args.command in ('printer-set-default', 'printer-default'):
            if args.dry_run:
                raise Error('--dry-run is not applicable to default-printer management.')
            if args.command == 'printer-set-default':
                return set_default_printer()
            show_default_printer()
        elif args.command == 'printer-status':
            if args.dry_run:
                raise Error('printer-status performs a read-only GET; --dry-run is not applicable.')
            return printer_status(resolve_printer_id(args.printer_id))
        elif args.command in ('printers', 'printer-info'):
            if args.dry_run:
                raise Error('Use print --dry-run for an offline request template.')
            printers(resolve_printer_id(args.printer_id) if args.command == 'printer-info' else None)
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
        print(output().color('Error:') + ' ' + (str(exc) if isinstance(exc, Error) else 'Local file or session I/O failed.'), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. If confirmation was in progress, check the app before retrying.', file=sys.stderr)
        return 130
    return 0


def main():
    context = _OUTPUT.set(None)
    try:
        return _main()
    finally:
        _OUTPUT.reset(context)


if __name__ == '__main__':
    sys.exit(main())
