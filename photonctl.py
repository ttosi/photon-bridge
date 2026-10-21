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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='validate and hash only; no network')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login', help='securely cache an existing QCloud XX-Token from environment')
    up = sub.add_parser('upload', help='upload exact .pm4u bytes; never print')
    up.add_argument('file')
    up.add_argument('--dry-run', action='store_true', dest='upload_dry_run')
    up.add_argument('--verbose', action='store_true', help='show sanitized reservation/confirmation/release JSON')
    listing = sub.add_parser('files', help='read-only cloud library diagnostic (one page)')
    for field, default in (('page', 1), ('limit', 10)):
        listing.add_argument('--' + field, type=int, default=default,
                             help='pagination value (default: %(default)s)')
    for field in ('file_type', 'orderby', 'machine_type'):
        listing.add_argument('--' + field.replace('_', '-'), dest=field,
                             help='explicit API value; omitted by default (semantics unverified)')
    args = parser.parse_args()
    try:
        if args.command == 'login':
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
