# Mono 4 Ultra experimental cloud-file print start

## Current capability and boundary

`./photonctl.py printer print <cloud-file-id>` can now submit one real order 1
request after preflight and explicit interactive y/yes confirmation. It uses the
saved default or explicit `--printer`. There is no --yes shortcut. Noninteractive
stdin/stderr is rejected before authentication/network traffic.

The existing `--dry-run` remains strictly offline: no authentication, options,
library, status or control requests. The separate local-file upload workflow
still has `LIVE_PRINT_DISPATCH_ENABLED = False`; this change enables only the
confirmed existing-cloud-file path through EXPERIMENTAL_CLOUD_PRINT_ENABLED.

No real API call, authentication attempt, printer-control request, live print or
APK execution occurred during development. All tests mock HTTP. The APK source
output remains outside Git, reused only for focused signature/caller checks.
Server acceptance with photonctl's PC headers and actual device execution remain
UNVERIFIED pending an attended operator-initiated test.

## Confirmed request constructor and routing evidence

Reuse [android-print-start.md](android-print-start.md) as the detailed eight-DEX
static analysis. The relevant source files remain outside Git under
`/tmp/photon-android-jadx/sources/`. Confirmed APK chain:

- classes2.dex: `LCDPrintableFileCloudNewFragment.Q` lines 944–986 creates the
  cloud slice request; `LCDStartPrintActivity.M0` lines 631–680 adds task_settings.
- classes6.dex: `SendCommandRepository.sendCommand` lines 75–80 serializes Gson
  JSON, including nulls; its coroutine invokes `CommandApiService.sendCommand`.
- `CommandApiService.java:59–60`: POST `api/work/operation/sendOrder`, @Body.
- classes3.dex: `BaseCommandRequest`, `SendCommandRequest`, `SendCommandData`
  establish inherited fields/defaults and nested data. The CLI's existing
  `android_print_request` is reused unchanged.

Full body, with typed placeholders (not literal strings to send):

```jsonc
{
  "printer_id": <PRINTER_ID_INT>,
  "order_id": 1,
  "project_id": 0,
  "data": {
    "file_id": <REGISTERED_CLOUD_LIBRARY_ID_INT>,
    "filename": "",
    "filepath": "/",
    "project_type": 1,
    "template_id": 0,
    "plate_id": 0,
    "is_delete_file": 0,
    "matrix": "",
    "slice_size": null,
    "slice_param": null,
    "hollow_param": null,
    "punching_param": null,
    "task_settings": null,
    "filetype": 0,
    "file_key": "",
    "file_name": "",
    "official_file_id": "",
    "official_file_type": -1
  },
  "settings": null,
  "ams_info": null
}
```

The transport is UTF-8 JSON bytes with Content-Type
`application/json; charset=utf-8`. The library ID comes from `newUploadFile.data.id`,
never the storage reservation or gcode_id. There is no taskid, task_mode, filesize,
form encoding, project creation, mandatory settings order 1225 or v5 fallback.

Additional static routing traced in this pass:

1. `CommandNetworkApi$Companion$apiService$2.invoke` obtains
   `CommandApiService.INSTANCE.getSERVER_URL()`.
2. `CommandApiService.Companion.SERVER_URL` is `c0.a.f14650a.b().b()`.
   `c0.a.b()` returns the common repository service (classes3.dex), whose
   `ac.cloud.reposervice.service.b.b()` calls `ICommonService.getProperties()`.
3. `ac.cloud.common.CommonServiceImpl.getProperties:53–54` returns `e.b.y()`.
   Its release branch (`e/b.java:442` onward) returns RELEASE_BASE_URL.
4. `ConfigConstants.EnvironmentConfig` initialization (`ConfigConstants.java:410`)
   forms RELEASE_PHP_BASE_URL + `/workbench/`. Lines 1070–1078 form
   `https://<domain>/p/p`. `e.a` line 38 initializes domain from resource
   `domain_name_no_http`; decoded resources `res/values/strings.xml:976` contains
   `cloud-universe.anycubic.com`.

Thus the **release-configured APK URL** resolves to:

```text
https://cloud-universe.anycubic.com/p/p/workbench/api/work/operation/sendOrder
```

This agrees with appending `/work/operation/sendOrder` to the existing CLI base
which already ends in `/api`. No duplicate `/api` is added, and no new hostname
is invented. Static path resolution is established; server acceptance of this
body using photonctl's existing PC/version/XX-Token headers is **not live-tested**.
The dispatcher reuses `credentials()`, `headers()`, Session and existing timeouts;
working authentication/signing behavior is unchanged. It does not substitute
Android device credentials, execute OAuth or expose headers.

## Verified preflight reads and explicit blockers

### Account library, slice state and model

The preflight uses the **Android GET** `/work/index/files`, preserving the existing
web POST listing command. Evidence from classes2.dex:

- `d1/a.java:193–194`, method `f`: GET with page, limit, optional printable,
  machine_type, keyword and folder_id; response ArrayList<FileResponse>.
- `LCDPrintableFileCloudNewFragment.Z:889–891` calls
  `FileFragmentViewModel.m(..., 10, 1, mMachineType, ...)`.
- `FileFragmentViewModel.j.invokeSuspend:370–391` passes pageNo, limit, printable,
  machine_type, with null keyword/folder defaults, to repository `a.L` / `a.K`.
- `LCDPrintableFileCloudNewFragment.Q:944–986` rejects status=3, blocks status=2
  for slice file_type=1, and constructs the Android start request for that slice
  branch. Other file types do not enter this already-sliced constructor.

The CLI queries page/limit=1/10, printable=1, machine_type=130, omitting null query
fields exactly as Retrofit does. It scans at most ten pages, stopping on the ID
or an empty page. Missing IDs, duplicate identities and malformed list shapes
block. A file returned by this authenticated account library establishes account
visibility without inspecting or exposing user IDs.

The conservative accepted subset is integer file_type=1, integer status=1, and
file_extension pm4u (case-insensitive, optional leading dot); supplied original
filenames must agree on the .pm4u suffix. Status=1 passes the APK's constructor
checks; pending 2, failed 3 and all missing/unknown/malformed codes are blocked.
This is not a global status taxonomy and is not applied to the web POST listing.

Explicit machine_type metadata conflicting with 130 blocks. An absent/generic
0 model relies on inclusion in Android's model-130 **printable** result and the
pm4u format, not on interpreting 0 as a compatible model. The exact server-side
filter implementation remains undocumented. No file conversion, slicing, upload,
URL retrieval or deletion occurs in this command.

### Device-specific task settings

The only new POST preflight is an APK-confirmed **read-only** options query:

```text
POST <existing base>/v2/device/getPrinterOptions
Content-Type: application/x-www-form-urlencoded
model_id=130&printer_id=<selected printer ID>
```

`a2/a.java:74–77` declares these fields. `LCDStartPrintActivity.D0` invokes it;
callback `b.b:175–215` reads PrinterOptionsResponse.list. That response class
(`ac/cloud/workbench/onlineSlice/data/response/PrinterOptionsResponse.java`,
classes2.dex, fields/constructor lines 13–26) declares List<OptionsListBean> list.
`LCDStartPrintActivity.M0:631–680` emits a null task_settings when the final map
is empty. An explicit successful `data: {"list": []}` therefore permits null in
this experimental subset. Unlike the app's fallback, missing/null/malformed lists
are not treated as empty.

**Any nonempty options list blocks this version.** Initial default_value/zero
fields are not a verified current selection. The APK normally requests order 1228
and receives current values in a 1225 MQTT event; conditional stir values use
`POST /v5/printer/properties/query`. These are not called here, and no UI-default
zeros are invented. The existing constructor/transport still validates non-null
device-derived maps, including stirBeforePrint and nested stir_settings.stirTime;
there is no CLI override that marks unknown settings verified.

Specific remaining blocker for a printer exposing options: obtain its complete
supported/current option values through a separately authorized observation or a
verified read-only current-state source. Implement that provenance/range handling
before allowing non-null settings. Do not send read-like control commands as
ordinary preflight or dry-run.

### Printer activity and physical readiness

GET `/v2/printer/info?id=<selected ID>` must return matching model-130 identity,
strict integer device_status=1 and is_printing=1. Offline/busy/unknown states block
before file/options queries. State is refreshed after final confirmation and
checked again by the dispatcher. No undocumented ready_status inference is used.

ONLINE + IDLE is not physical readiness. The summary explicitly asks the attended
operator to check resin, vat, platform, cover and other interlocks. y/yes confirms
those checks and the experimental submission. The CLI does not bypass any printer
safety mechanism or claim to verify physical sensors remotely.

## Submission, acknowledgement and observation

The reused `dispatch_android_print` signs using existing credentials()/headers(),
sets UTF-8 application/json, disables redirects and submits **once** with timeout
(15,120). No authentication changes, preliminary order 1225, read-like 1228/1231,
v5 fallback, automatic retries or repeated starts are used.

Android `SendPrinterResponse` (classes3.dex) declares String msgid and int task_id
(default 0). ApiResponse.isSuccess accepts 1 or 200. The dispatcher distinguishes:

1. HTTP transport status (`http_status`),
2. successful cloud API code (`api_code`, cloud_accepted),
3. independent execution (`printer_accepted` remains null).

It requires a JSON acknowledgement object, validates any supplied msgid/task_id,
and redacts credentials/URLs. Missing identifiers or task_id=0 never identify a
job. `LCDStartPrintActivity.k.a:506–514` navigates UI on API success only; it is not
a device receipt. HTTP 200 with a rejected/malformed API body is not acceptance.

After accepted submission the command performs at most three short-timeout info
GET observations, two seconds apart, stopping on PRINTING or loss of ONLINE.
It reports idle_to_printing_observed and file_correlated=false. A failed poll does
not erase cloud acceptance, does not resend, and is reported as unavailable state.
An IDLE→PRINTING transition is useful evidence of activity, not proof of this file
or task. JSON output remains one sanitized result; summary/prompt stay on stderr.

Task/file correlation is still unverified. `j2/a.java` declares GET
api/work/project/info?id, api/v5/project/source?id and api/v5/project/info?id;
PrintProjectV5Response includes id, gcode_id, gcode_name, m_file, print_status and
progress. No proven mapping of returned task_id to these query IDs and the exact
library ID was established, so these APIs are not called. gcode_id is not assumed
to equal file_id, and busy state is never called file-specific acceptance.

## Attended controlled live test (not executed)

1. Verify your default with `./photonctl.py printer default`, or supply the intended
   `--printer`. Use your own cloud-library file ID; 98988601 below is user-supplied
   example input, not a production default.
2. Inspect the offline schema first:

   ```bash
   ./photonctl.py printer print 98988601 --dry-run
   ```

3. Be physically present. Check the vat/resin, platform, cover, work area and all
   physical safeguards. Identify the intended file in the official library.
4. Explicitly initiate the experimental command:

   ```bash
   ./photonctl.py printer print 98988601
   # Or explicitly select the discovered printer:
   ./photonctl.py printer print 98988601 --printer <printer-id>
   ```

   This performs authenticated preflight reads, including the read-only form POST.
   Any missing file/readiness/state/options prerequisite stops before confirmation
   or sendOrder. If options are nonempty, the required next step is the specific
   settings-readback investigation above, not an override or forced null.
5. Review printer/name/ID, cloud file ID and ONLINE/IDLE summary. Only answer y/yes
   to `Start this print? [y/N]:` after completing physical checks. Blank/no/EOF
   cancels. No upload or file mutation is performed by this command.
6. Keep attending the printer. Record sanitized HTTP/API result and optional
   msgid/task_id. Inspect the physical printer and official app to verify the new
   job's intended file and actual operation. CLI activity alone is not that proof.

A timeout/transport exception after submission or ambiguous HTTP/malformed response
is **UNKNOWN OUTCOME**. Never automatically resend. Even a cloud rejection must
not trigger an automatic retry. Inspect printer/app before considering another
manual command. No live print was initiated by development tools.

## Validation

`python3 -B -m unittest discover -s tests`: 96 tests pass. Added CLI/mock coverage
includes signed exact JSON/URL/content type, default/override IDs, offline preview,
account/model-filtered file reads, readiness and options blockers, y/default-no/EOF,
TTY safety, state changes during confirmation, cloud acceptance versus observation,
API/malformed/timeout outcomes, bounded polling and exactly one print-start attempt.
Existing uploads, printer management, help, output modes and local-workflow gates
remain covered. Static analysis/mocks cannot establish server or printer acceptance.
