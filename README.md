# photonctl

Uploads an already-sliced `.pm4u` file's exact bytes using the supplied QCloud
reservation → storage PUT → confirmation → reservation release protocol. Never slices.
Existing cloud files can be started experimentally with explicit interactive consent.
Requires Python 3.8+ and `requests`:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install requests
```

## Command layout

```text
python3 photonctl.py login
python3 photonctl.py print <local-file.pm4u> [--printer <printer-id>] [--dry-run]
python3 photonctl.py file upload <path> [--dry-run] [--verbose]
python3 photonctl.py file list [--page 1] [--limit 10]
python3 photonctl.py printer list
python3 photonctl.py printer set-default
python3 photonctl.py printer default
python3 photonctl.py printer info [printer-id]
python3 photonctl.py printer status [printer-id]
python3 photonctl.py printer print <file-id> [--printer <printer-id>] [--dry-run]
```

Explore native command help without authentication, configuration reads or cloud requests:

```bash
python3 photonctl.py help
python3 photonctl.py help login
python3 photonctl.py help file
python3 photonctl.py help file upload
python3 photonctl.py help printer
python3 photonctl.py help printer set-default
python3 photonctl.py help printer status
python3 photonctl.py help printer print
```

`help` follows the actual command hierarchy at any depth; supply command names,
without positional IDs or option values. Unknown targets fail with a clear error
and a spelling suggestion when available. Standard `--help` remains equivalent:
`python3 photonctl.py --help`, `python3 photonctl.py printer --help`, and
`python3 photonctl.py printer status --help`. Leaf help includes arguments,
relevant defaults and examples. Deprecated aliases remain hidden in root help.
Existing upload/list filters and argument types are unchanged. The legacy root
`--dry-run` spelling (before the resource) still works where it previously did;
prefer `--dry-run` on the upload or print command. Existing-cloud-file print dispatch
is experimental and requires confirmation; the local upload workflow remains disabled.

The following old top-level commands are temporarily accepted as hidden deprecated
aliases. They parse the same operation arguments and call the same handlers:

| Deprecated command | Preferred command |
|---|---|
| `upload` | `file upload` |
| `files` | `file list` |
| `printers` | `printer list` |
| `printer-info` | `printer info` |
| `printer-status` | `printer status` |

Aliases emit a warning on stderr and use the same output modes and operation exit
codes as their preferred commands. Their own `--help` remains available, but root help advertises only the
new layout. Migrate scripts to the preferred forms, particularly scripts requiring
empty stderr. Aliases can be removed in a future major release after at least one
published deprecation release; no removal date is scheduled. `login` is unchanged.

## Experimental print of an existing cloud file

With the operator physically present, use the saved default printer:

```bash
./photonctl.py printer print 98988601
# Optional explicit override:
./photonctl.py printer print <cloud-file-id> --printer <printer-id>
```

The supplied ID is an existing cloud-library ID; it is not a built-in default.
This command is experimental. It performs only preflight reads until you explicitly
answer `y` or `yes` to:

```text
Printer: Anycubic Photon Mono 4 Ultra (<printer-id>)
File ID: 98988601
Connectivity: ONLINE
Activity: IDLE
Start this print? [y/N]:
```

You must check resin, vat, platform, cover and other physical interlocks before
confirming. Never bypass printer safety mechanisms. ONLINE + IDLE is not physical
readiness. Interactive stdin/stderr is required; blank/no/EOF cancels, Ctrl-C
interrupts. There is no `--yes` shortcut.

Preflight checks model 130, matching printer identity, ONLINE + IDLE, and finds the
ID in the authenticated Android **GET** `/work/index/files` with `printable=1`
and `machine_type=130`. It requires `.pm4u`, slice `file_type=1`, and integer
`status=1`; pending/failed/missing/unknown flags block. Any explicit conflicting
model metadata blocks. A generic model marker 0 or absent model relies on the
Android model-filtered printable result plus the `.pm4u` format, not a claim that
0 identifies model 130. Scanning is bounded to ten pages of ten. The working web
**POST** file-list command remains unchanged.

The read-only, APK-derived form POST `/v2/device/getPrinterOptions` must return an
explicit `data.list: []` to use Android's null task_settings. **Nonempty or malformed
options block dispatch:** their current device-derived settings are not yet
available without separately authorized readback. No zeros are invented, no
1228/1225 settings/readback commands are sent, and there is no settings override.
A printer exposing options therefore cannot yet print through this CLI.

After confirmation, printer state is refreshed and exactly one signed JSON order 1
is sent. The release APK resolves to the existing proxy prefix, with no doubled
`/api`. PC-session server acceptance is still unverified until an authorized live
test. No upload, slicing, deletion, control fallback or automatic resend occurs.

HTTP success, cloud API acceptance and actual printer execution are separate.
The result reports `http_status`, `api_code`, optional `msgid`/`task_id`, and keeps
`printer_accepted: null`. Up to three info GETs (two seconds apart, short bounded
timeouts) observe subsequent activity; an IDLE→PRINTING observation is useful but
is **not file/task-correlated acceptance**. No print-start is resent on timeout or
failed polling. A submission timeout is UNKNOWN OUTCOME: inspect the physical
printer and official app before considering another command.

Offline preview remains unchanged and makes no API requests, including no options
or status reads:

```bash
./photonctl.py printer print 98988601 --dry-run
```

`--verbose`, `--json` and `--no-color` work globally or on this command. Live JSON
contains the sanitized preflight record, full constructor and cloud receipt/
activity observation; summaries and confirmation stay on stderr. On rejection or
ambiguous submission, errors stay on stderr and no successful receipt is emitted.
No live test was performed during development. See the
[live validation procedure](discovery/live-print-validation.md).

## Upload and prepare a print

```bash
# Fully offline: validate/hash local bytes and resolve the local default.
python3 photonctl.py print model.pm4u --dry-run
python3 photonctl.py print model.pm4u --printer <printer-id> --dry-run --json

# Upload exact bytes, verify library visibility, inspect printer state, then preview.
# These commands DO upload; they never start a print in this release.
python3 photonctl.py print model.pm4u
python3 photonctl.py print model.pm4u --printer <printer-id> --verbose
```

The default printer is resolved exactly as for printer info/status; `--printer`
overrides it. The workflow supports model 130 only, verifies printer identity and
model before upload, reuses the permanent uploader, then scans the existing web
library for the returned cloud-library ID. Original filename, size and MD5 are
cross-checked when returned. The reservation ID never becomes the print file ID.
A second info GET refreshes activity after upload. The library scan is bounded to
10 pages of 10, stopping at the matching record, an empty page or reported total.
A missing record can mean delayed visibility or a bounded scan, not lost bytes;
check `file list` before uploading again. No deletion or rollback is performed.

Without `--experimental-live`, the result is an upload-and-preview, even when
ONLINE/IDLE. `--dry-run` does not upload or make **any** cloud requests, does not
read credentials, and cannot construct an order with a real file ID: its
`print_order` is null. Use the existing cloud-file command for a complete offline
constructor when you already know the cloud-library ID:

```bash
python3 photonctl.py printer print <cloud-file-id> --printer <printer-id> --dry-run
```

The former hidden root `print <cloud-file-id>` alias is replaced by the local-file
workflow. Migrate that old usage to `printer print`; other deprecated aliases
remain supported.

The future controlled-test spelling is:

```bash
python3 photonctl.py print model.pm4u --printer <printer-id> --experimental-live
```

**The local-file upload workflow remains source-disabled. This flag cannot enable
that workflow in this release.** Existing cloud files use the separately gated,
confirmed `printer print` command above. It currently uploads/prepares, then reports unresolved prerequisites
and exits nonzero; uploaded files remain in the library. Noninteractive live
invocations are rejected before any cloud request or upload. Ordinary previews
may be used in scripts. `--dry-run` and `--experimental-live` are incompatible.

A one-shot dispatcher is implemented and mock-tested. Before enabling it in a
separately reviewed change, establish model-130 options/current settings, file
print-readiness and PC-session acceptance of Android JSON. Do not default unknown
options to zero or force null settings. ONLINE + IDLE is activity, **not readiness**.
No order 1228/1225/1231 or other prerequisite control command is sent here.

Once the prerequisites have been implemented/verified and the source gate is
reviewed, live mode requires interactive stdin/stderr, a specific compatible
printer, a registered ready file, ONLINE + IDLE, an operator physical check, and
`Start printing? [y/N]:`. Only y/yes proceeds. Check vat/resin, platform, clear
work area and physical interlocks first; the prompt confirms these checks. State
is checked again after confirmation. Cancel/EOF leaves the upload in the library.
No `--yes`, automatic retries or fallback commands exist.

Cloud acceptance is separate from actual printer execution. A future success
reports API code and optional msgid/task_id, with `printer_accepted: null`; it never
claims a particular job started. A timeout, malformed acknowledgement or ambiguous
HTTP outcome must not be retried blindly. Inspect the physical printer and official
app. File/task-correlated read-only polling is not yet established or implemented.

`--verbose`, `--json` and `--no-color` work on the combined command. JSON is a single
workflow object containing local/uploaded `file`, selected printer, interpreted
states, sanitized library/printer snapshots, `print_order`, blockers and gate state.
Offline results have no uploaded ID; future dispatch results add a separate receipt.
Human confirmation/physical-check messages go to stderr in JSON mode. An error
after upload reports its cloud ID on stderr so it can be investigated without a
second upload. See [live validation plan](discovery/live-print-validation.md).

## Output modes

Printer list/info/status/default, file list/upload and local print preparation use concise human output by
default. Use the same flags before the resource, after the resource, or after the
command:

```bash
python3 photonctl.py printer status
python3 photonctl.py --verbose printer status
python3 photonctl.py printer status --verbose
python3 photonctl.py printer list --json
python3 photonctl.py file list --json
python3 photonctl.py printer status --no-color
```

`--verbose` adds sanitized JSON, API sources, raw flags and interpretation/freshness
notes. Upload verbose output retains reservation/confirmation/release API codes
and IDs. `--json` overrides verbose output and emits one JSON document on stdout,
without headings, progress or colors. Errors and default-configuration warnings
remain on stderr; check the exit code. No retries or extra API calls are added.
`--json` is not supported for login or interactive set-default.

JSON structures:

- File list: sanitized API `data`, preserving containers, field names and types.
- Printer list/info/status: allowlisted API `data`, preserving containers and raw
  IDs/types/flags; interpreted labels are human output only.
- Printer default: local `{id, name?, model?}` object, with the stored string ID.
- Successful upload: `{id, name, size, md5}`, using the returned cloud ID unchanged,
  original filename, byte count and uppercase checksum. Local upload dry-run emits
  `{name, size, md5, dry_run: true}`.

Redaction may replace sensitive values; JSON is not an unfiltered server dump.
An invalid/missing printer snapshot may still emit its sanitized data before
exiting nonzero. Transport/API failures emit no JSON document. Scripts that used
raw JSON embedded in old human output should migrate to `--json`.

Colors apply only on a TTY, with green ONLINE, red OFFLINE, yellow PRINTING and
muted IDLE/UNKNOWN. `NO_COLOR` (including an empty value), `--no-color`, pipes and
redirects disable colors. `-v` remains the version flag; use `--verbose` for details.
Tables preserve server order, abbreviate long display text, and use KiB/MiB sizes;
JSON/verbose retain complete metadata. IDs are not truncated or renumbered.

Illustrative output using mocked records, not a live account check:

```text
Anycubic Photon Mono 4 Ultra
ID: 797953
Status: ONLINE
Activity: IDLE
Firmware: 4.0.7.9

ID        PRINTER                       STATUS   ACTIVITY
797953 *  Anycubic Photon Mono 4 Ultra  UNKNOWN  UNKNOWN

ID     NAME               TYPE  SIZE       STATUS
12345  calibration.pm4u   pm4u  482.0 KiB  2
```

The list endpoint's state flags are not independently established here, so list
rows show UNKNOWN without fetching individual printer details. Use `printer
status` for the verified info-endpoint interpretation. File statuses remain raw
values: numeric `2` is not labeled Ready or Processing. `*` marks the local default.

## Default printer

Select a printer from your authenticated account in an interactive terminal:

```bash
python3 photonctl.py printer set-default
```

The command fetches the registered printer list once, shows names/models and
cloud IDs as `1) Printer name (cloud-id)`, marking the current default as
`*1) Printer name (cloud-id)`. Enter its menu number to save immediately;
there is no confirmation prompt. Success prints `Default printer set to <printer-name>.`
Even a single returned printer requires explicit selection. Enter/q/cancel at
the selection prompt leaves configuration unchanged. EOF cancels; Ctrl-C exits 130.
Noninteractive stdin is rejected before any cloud request or prompt. An empty
printer list or API failure does not change the saved default.

The ID and optional display metadata are saved atomically in
`~/.config/photonctl/config.json` (0600; its directory is restricted to 0700).
Authentication remains separate in `session.json`. No passwords, tokens or device
keys are stored in the default-printer entry. Show the local setting without a
cloud request:

```bash
python3 photonctl.py printer default
```

Use the default without repeating its ID:

```bash
python3 photonctl.py printer info
python3 photonctl.py printer status
python3 photonctl.py printer print <cloud-file-id> --dry-run
```

Explicit positional IDs or `--printer <printer-id>` always override the default,
including when local configuration is missing or invalid. Without an explicit ID
or saved default, these commands fail with instructions to run `printer set-default`;
they never select a printer automatically. Hidden deprecated command aliases use
the same default resolution.

`printer list` marks the configured default with `*` in its table. A warning on
stderr reports when a successful list no longer contains it; configuration is
never silently changed. Run `printer set-default` to select a replacement.
`--json` keeps the API data separate from this local display marker. Malformed
list/config data produces a diagnostic rather than claiming the default is absent.
Repair invalid JSON or permissions before changing the configuration.

Defaults are local preferences, not account-bound device validation. Reselect
after changing accounts. Ordinary info/status commands resolve the default
locally and make only their usual GET; they do not fetch a second printer list.
A stale ID remains explicit until you reselect, and its API lookup can fail or
return no record. The default display and print dry-run do not validate it online.
Print remains offline/dry-run-only, with readiness and option checks unresolved.

## Login (existing session fallback)

Email/password automation is **not established or implemented**. APK analysis
found a hosted Casdoor login with an OAuth-to-QCloud exchange, not a complete
native password protocol. See [authentication findings and blockers](discovery/authentication.md).
No native live-login test is available yet; `login` continues to securely import
an existing QCloud session and does not authenticate or validate it online.
Failed or incomplete imports preserve an existing cached session. No refresh
mechanism or token lifetime is assumed.

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

The default output is a table of IDs, filenames, types, human-readable sizes and
raw statuses. `--json` retains sanitized API data under the server's original field
names and types; `--verbose` additionally shows the API response envelope/code and
request details. URL/credential fields and unrecognized text are redacted. Missing
metadata is not guessed; use verbose output to inspect unfamiliar response shapes.

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
They display a compact list or detail view. `--verbose` and `--json` retain
allowlisted identity/model metadata and raw state values.
`printer print --dry-run` makes no network requests and shows the complete Android
APK-derived cloud-slice JSON constructor under `print_order`. IDs are positive
32-bit JSON integers; `project_id=0`, `order_id=1`, `project_type=1`, `filetype=0`.
All documented empty-string defaults and explicit nulls are preserved. There are
no desktop task fields or preliminary settings write. Omitting `--dry-run` invokes
the separately documented experimental preflight and confirmation flow.

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

## Cloud connectivity and activity snapshot

```bash
python3 photonctl.py printer status 797953
```

Makes one read-only GET using the existing session. Shows concise cloud-reported
ONLINE/OFFLINE when supported by APK LCD semantics;
otherwise UNKNOWN with a reason. For the physically tested Photon Mono 4 Ultra
(`machine_type=130`), online `is_printing=1` reports IDLE and `2` reports PRINTING.
Offline activity is always UNKNOWN because cached values may be stale. Missing,
malformed or unrecognized flags also give UNKNOWN; activity on other models is
UNKNOWN. Cloud status may lag physical changes. IDLE does not mean ready to print,
and PRINTING does not identify or prove acceptance of a particular job.

Example default summary lines (sanitized raw metadata, source and freshness
reasoning are available with `--verbose`; raw data alone with `--json`):

```text
# Online, idle
Status: ONLINE
Activity: IDLE
# Online, printing
Status: ONLINE
Activity: PRINTING
# Offline, regardless of cached activity
Status: OFFLINE
Activity: UNKNOWN
```
No control commands are sent. See [endpoint evidence and test guidance](discovery/printer-status.md).
