# photonctl

Uploads an already-sliced `.pm4u` file's exact bytes using the supplied QCloud
reservation → storage PUT → confirmation → reservation release protocol. Never slices or starts a print.
Requires Python 3.8+ and `requests`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install requests
```

## Command layout

```text
python3 photonctl.py login
python3 photonctl.py file upload <path> [--dry-run] [--verbose]
python3 photonctl.py file list [--page 1] [--limit 10]
python3 photonctl.py printer list
python3 photonctl.py printer info <printer-id>
python3 photonctl.py printer status <printer-id>
python3 photonctl.py printer print <file-id> --printer <printer-id> --dry-run
```

Use `--help` at the root, resource or command level, for example
`python3 photonctl.py file --help` or `python3 photonctl.py printer print --help`.
Existing upload/list filters and argument types are unchanged. The legacy root
`--dry-run` spelling (before the resource) still works where it previously did;
prefer `--dry-run` on the upload or print command. Print dispatch remains disabled.

The following old top-level commands are temporarily accepted as hidden deprecated
aliases. They parse the same operation arguments and call the same handlers:

| Deprecated command | Preferred command |
|---|---|
| `upload` | `file upload` |
| `files` | `file list` |
| `printers` | `printer list` |
| `printer-info` | `printer info` |
| `printer-status` | `printer status` |
| `print` | `printer print` |

Aliases emit a warning on stderr; normal stdout and operation exit codes are
unchanged. Their own `--help` remains available, but root help advertises only the
new layout. Migrate scripts to the preferred forms, particularly scripts requiring
empty stderr. Aliases can be removed in a future major release after at least one
published deprecation release; no removal date is scheduled. `login` is unchanged.

## Login (existing session fallback)

Email/password automation is **not verified or implemented**. `login` securely
imports an existing QCloud session; it does not authenticate or validate it online.

1. Sign in yourself at <https://cloud-universe.anycubic.com/file> using the official
   browser login, including any CAPTCHA or MFA.
2. Open Developer Tools → Network and inspect an authenticated cloud API request.
   Use the **request header `XX-Token`** from that request. The same value is saved
   under Application → Local Storage → `XX-Token` by the official website.
   Do not use a Makeronline/CAS JWT, OAuth `access_token`, or `id_token`.
3. Copy the API prefix from that request's URL, excluding the endpoint path.
   The official site's current JavaScript configures
   `https://cloud-universe.anycubic.com/p/p/workbench/api`.
   Set this base only if it matches your observed authenticated requests. For an
   Android session, use its actual `XX-Token` and matching regional API prefix
   from your own authorized observations. Do not guess a regional hostname.

In Bash, enter the token without displaying it or placing its value in history:

```bash
read -rsp 'QCloud XX-Token: ' PHOTONCTL_TOKEN
printf '\n'
export PHOTONCTL_TOKEN
read -rp 'Observed HTTPS API base: ' PHOTONCTL_API_BASE
export PHOTONCTL_API_BASE
python3 photonctl.py login
unset PHOTONCTL_TOKEN PHOTONCTL_API_BASE
```

The session is stored at `~/.config/photonctl/session.json`, mode `0600`.
No password or refresh credential is stored. Both environment variables together
can also override the cache directly for an upload. An expired session must be
reimported from the official client.

## Upload

First check the local file without any network traffic:

```bash
python3 photonctl.py file upload /path/to/model.pm4u --dry-run
```

This checks extension, regular/nonempty file, size and MD5. It does not parse or
certify the internal slice format. Then upload explicitly:

```bash
python3 photonctl.py file upload /path/to/model.pm4u
```

Check the official Android app's cloud files afterward. A successful server
confirmation is reported separately from actual Android visibility, which the
script cannot verify. If confirmation fails or times out, check the app before
repeating the upload: confirmation is never automatically retried. Failed uploads
may leave a storage reservation; this script does not delete account data.

## Verified locally and remaining blockers

```bash
python3 -m unittest discover -s tests -v
```

Mock tests cover the supplied signature vector, full byte-preserving upload,
API/storage header separation, MD5 mismatch, confirmation timeout without retry,
o-network dry-run, session cache permissions, permanent reservations, distinct registered
file IDs, reservation release without deletion, release failures and listing pagination.

Public evidence inspected on 2026-10-19:
[official cloud client JavaScript](https://cloud-universe.anycubic.com/js/app.982cbff9.js),
loaded by the [official cloud page](https://cloud-universe.anycubic.com/file).
It specifies the API proxy above and this browser flow:

- Authorization at `https://uc.makeronline.com/login/oauth/authorize` with a
  redirect to the official cloud `/login` page.
- `GET /v3/public/getoauthToken` with the authorization `code`.
- `POST /v3/public/loginWithAccessToken` with the returned `access_token` and
  `device_type: web`.
- The response's `data.token` is stored as `XX-Token`.

This verifies the origin of the browser session token; it does **not** establish
that an arbitrary Makeronline JWT is exchangeable. The script does not intercept
or replay the browser's authorization codes.

The uploader now follows the successful official browser capture supplied on
2026-10-21: reserve with `is_temp_file: 0`, PUT unchanged bytes, confirm with only
`user_lock_space_id`, then release with `id` and `is_delete_cos: 0`. Confirmation's
`data.id` is the newly registered cloud file ID, distinct from the reservation ID.
If release fails, confirmation is not repeated. Check the library before retrying.

Direct email/password authentication remains unimplemented. The updated uploader
has been tested with mocks only; no live upload was run for this update. Neither
registration nor `machine_type: 0` / `status: 2` establishes print readiness.

## Diagnose an upload missing from the Android app

Run this next, using the existing cached session:

```bash
python3 photonctl.py file list
```

This makes exactly one read-only `POST /work/index/files` request with
`{"page": 1, "limit": 10}`.
It uses the existing signing and session handling and makes no registration,
reservation, storage, confirmation, deletion or printing requests. As with upload,
a complete pair of environment variables overrides the cached session.

The command prints sanitized JSON, retaining the server's field names and response
structure, API code, filenames, IDs, types, statuses, sizes and timestamps where
provided. URL/credential fields and unrecognized text values are redacted. It does
not assume the response schema or substitute guessed metadata. Validation errors
are shown in sanitized form so required fields can be identified.

Pagination uses positive integers: `--page` defaults to 1 and `--limit` to 10.
For the next page:

```bash
python3 photonctl.py file list --page 2 --limit 10
```

`--file-type`, `--orderby` and `--machine-type` remain optional with no defaults;
their semantics on this endpoint are unverified. Numeric values become JSON
integers, other values remain strings. The default listing adds no such filters.

Do not interpret an empty page as proof of missing registration. Server defaults,
pagination, machine filters and file-type filters may hide a registered file.
Compare returned IDs and original filenames with the completed upload first.

`file upload --verbose` prints sanitized JSON from reservation, confirmation and release,
including API codes and returned IDs. This command performs a fresh upload:

```bash
python3 photonctl.py file upload /path/to/model.pm4u --verbose
```

## Read-only printer discovery and offline print preview

```bash
python3 photonctl.py printer list
python3 photonctl.py printer info 797953
python3 photonctl.py printer print <cloud-file-id> --printer 797953 --dry-run
```

`printer list` and `printer info` perform only source-backed read-only GET requests.
They display allowlisted identity/model/state metadata with raw status values.
`printer print --dry-run` makes no network requests and shows the complete Android
APK-derived cloud-slice JSON constructor under `print_order`. IDs are positive
32-bit JSON integers; `project_id=0`, `order_id=1`, `project_type=1`, `filetype=0`.
All documented empty-string defaults and explicit nulls are preserved. There are
no desktop task fields or preliminary settings write. Real dispatch is disabled;
omitting `--dry-run` fails.

The CLI currently previews `task_settings: null` because no live option metadata
is supplied. This is a constructor preview, not evidence that model 130 has no
options or that a null-settings request is suitable for this printer.
The pure `android_print_request` helper also accepts a settings dictionary and
explicit server-derived `supported_options` names. It checks names and integer
value types, preserves selected values, and never fills missing options with 0.
An empty dictionary becomes null, matching Android. Enabled `stirBeforePrint`
requires `stir_settings: {"stirTime": <positive integer>}`; the nested name and
shape are checked. This validates structure/support-name membership only: the
caller must supply genuine current server/device metadata and values. Provenance,
option-specific ranges and capabilities cannot be verified offline. No settings
or option-fetching CLI feature is added in this change.

Missing live prerequisites remain:

- Model-130 supported options, current values and device-specific ranges.
- The selected printer's model and readiness, and the selected `.pm4u` file's
  readiness; these IDs are not looked up during dry-run.
- Acceptance/routing of Android JSON through the existing cloud proxy.
- Correlation of API `msgid`/`task_id` with actual printer/task acceptance.

See [discovery/android-print-start.md](discovery/android-print-start.md) for APK
locations, the Android workflow and the next separately authorized experiments.
No control endpoint is called, including read-like orders 1228/1231, and no APK
code is executed by the preview.

## Cloud connectivity snapshot

```bash
python3 photonctl.py printer status 797953
```

Makes one read-only GET using the existing session. Displays sanitized raw status
metadata and cloud-reported ONLINE/OFFLINE when supported by APK LCD semantics;
otherwise UNKNOWN with a reason. Cloud status may lag physical power changes.
No control commands are sent. See [endpoint evidence and test guidance](discovery/printer-status.md).
