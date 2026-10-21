# Android cloud-file print start: Mono 4 Ultra

## Result and evidence boundary

The supplied APK contains a complete **Android client-side cloud-file print
constructor**. For the traced LCD flow, start is **JSON**, not form-urlencoded;
`data.file_id` is an integer cloud-library ID; `data.project_type` is `1`;
`project_id` remains `0`. The client does not supply `taskid`, `task_mode` or
`filesize`. It retrieves automatic-operation settings with **order 1228** and
receives their values through a **1225 event**. It does **not** send a settings
order 1225 as a prerequisite to this start path.

These are static APK findings, not a tested print. Device-specific option names
and current values come from the server/device and cannot be recovered as one
fixed Mono 4 Ultra payload from the APK alone. No account authentication, API
calls, printer commands or APK execution were performed. Production photonctl
code was not modified.

Input: `discovery/apk/base.apk`, supplied package `ac.cloud.com`.
SHA-256: `c992b653e1eb3bbdf935d875e495aaa1b89aac39a1afc7de355a48f873114986`.
JADX CLI 1.5.6 loaded all eight DEX files (`classes.dex`–`classes8.dex`). Sources
and logs are only in `/tmp/photon-android-jadx/` and
`/tmp/photon-android-jadx.log`, outside Git. Reproduction:

```bash
jadx -r --show-bad-code --no-inline-methods -j 4 \
  -d /tmp/photon-android-jadx discovery/apk/base.apk
```

JADX reported 89 errors across the APK; the principal constructors and dispatch
methods cited below have bodies and no method-decompilation failure markers.
Java is a reconstruction; Kotlin metadata, constructor defaults, coroutine
wrappers, and Retrofit annotations were cross-checked. This is not a proof that
no other runtime/reflection-driven path exists.

## Relevant locations

Paths below are relative to `/tmp/photon-android-jadx/sources/`. Line numbers
refer to that output with the options above. Obfuscated names are retained so
findings can be reproduced; the original Kotlin filename is given where useful.

| DEX | Class/path and method | Location / role |
|---|---|---|
| 2 | `ac/cloud/workbench/main/activity/printer/LCDPrintableFileNewActivity.java`, inner pager `createFragment` | 213–215: cloud tab instantiates `LCDPrintableFileCloudNewFragment` |
| 2 | `ac/cloud/workbench/main/fragment/LCDPrintableFileCloudNewFragment.java`, `Q(FileResponse,int)` | 944–986: checks file status/type, builds cloud print request |
| 1 | `t/d.java`, `g(int,FileResponse,AmsInfo)`; compiled `PrepareCloudPrintSelectionUseCase.kt` | 374–404: independent newer selection builder confirms integer library ID and project type 1; AMS attached only for FDM |
| 1 | `ac/cloud/common/ext/h.java`, `m`, synthetic default wrapper `n` | 257–267: passes request and printer context to LCD start activity |
| 1 | `ac/cloud/common/utils/PrinterTypeUtils.java`, `Companion.getPrinterTypeById` | `<10000` is LCD (`0`), otherwise FDM (`2`); model 130 takes LCD route |
| 2 | `ac/cloud/workbench/onlineSlice/LCDStartPrintActivity.java` | `initView` 795–820; `D0` 560; `B0` 544; start-button `H0` 578; final `M0` 631–680 |
| 2 | `a2/a.java`; compiled `FileApiService.kt` | 66–77: option/property prerequisite endpoints |
| 2 | `ac/cloud/workbench/onlineSlice/viewmodel/StartPrintViewModel.java`, `d`, `f` | 456–489: option/property query wrappers |
| 2 | `ac/cloud/workbench/onlineSlice/viewmodel/a.java`; compiled `OnlineSliceRepository.kt` | `e.invokeSuspend` 257–276 and `f.invokeSuspend` 311–331: actual API invocation |
| 1 | `ac/cloud/common/base/BaseMqttHandleActivity.java`, `sendMqttCommand` | 1517–1534: despite name, invokes HTTP viewmodel |
| 6 | `com/cloud/mqttservice/apiservice/SendCommandViewModel.java`, `sendCommand` | 25–43, plus `SendCommandViewModel$sendCommand$1$1.invokeSuspend` 42–66 |
| 6 | `com/cloud/mqttservice/apiservice/SendCommandRepository.java`, `sendCommand` | 75–80 builds JSON RequestBody; `AnonymousClass2.invokeSuspend` 47–69 invokes Retrofit |
| 6 | `com/cloud/mqttservice/apiservice/CommandApiService.java`, `sendCommand` | 59–60: `@POST("api/work/operation/sendOrder")`, `@Body okhttp3.c0` |
| 3 | `ac/cloud/publicbean/main/data/request/{BaseCommandRequest,SendCommandRequest,SendCommandData}.java` | Base defaults 86–94; request defaults 100–109; data fields 21–38 and defaults 318–319 |
| 3 | `ac/cloud/publicbean/main/data/response/SendPrinterResponse.java` | fields `String msgid`, `int task_id`; constructor and getters |

Direct-call search across all generated sources found one Retrofit
`CommandApiService.sendCommand` invocation: the repository's coroutine. Repository
callers are the SendCommandViewModel coroutine and an unrelated
`OtaUpdateViewModel` coroutine. SendCommandViewModel's shared UI callers are
`BaseMqttHandleActivity.sendMqttCommand` and
`BaseMqttHandleFragment.sendMqttCommand` (fragment lines 1526–1543). Generated
`$default` methods only set defaults/delegate; they do not construct a second
wire payload. This documents the shared callers without tracing unrelated
motion, exposure, OTA and other command producers.

The concrete path here is:

```text
LCDPrintableFileNewActivity cloud tab
  -> LCDPrintableFileCloudNewFragment.Q(selected FileResponse)
  -> ac.cloud.common.ext.h.n -> h.m
  -> LCDStartPrintActivity (options/readback, then Start button H0 -> M0)
  -> BaseMqttHandleActivity.sendMqttCommand
  -> SendCommandViewModel.sendCommand$default -> sendCommand
  -> SendCommandViewModel$sendCommand$1$1.invokeSuspend
  -> SendCommandRepository.sendCommand
  -> SendCommandRepository.AnonymousClass2.invokeSuspend
  -> CommandApiService.sendCommand
```

## Serialization and full cloud-file schema

`SendCommandRepository` calls `com.blankj.utilcode.util.p.g(request)` (GsonUtils),
then `okhttp3.c0.INSTANCE.b(json, okhttp3.x.INSTANCE.b("application/json"))`.
The `data` property is a nested JSON object, **not a JSON-encoded string inside a
form field**. `okhttp3/c0.java:178–191` adds `; charset=utf-8` and encodes UTF-8.
Thus the constructed body's content type is `application/json; charset=utf-8`.

GsonUtils (`com/blankj/utilcode/util/p.java:15–16,27–40,53–58`) uses
`new GsonBuilder().f().c().b()` in the obfuscated names. Inspection of
`com/google/gson/e.java:108–141` and `Gson.java:487,565–573` establishes that `f()`
enables **serializeNulls**, and `c()` disables HTML escaping. `Gson.r` uses the
runtime class, so inherited private base fields are included. No delegate Gson
registration was found in the generated sources. This is distinct from the
Retrofit response converter's GsonFactory.

For an already uploaded slice selected in the printer's cloud tab, `Q` creates
both classes with all Kotlin constructor defaults, then sets only printer ID,
order 1, file ID, filetype 0, and project type 1. `M0` subsequently assigns
`task_settings`. This yields the following complete JSON shape. Uppercase angle
brackets denote **typed placeholders**, not literal JSON strings to send. The
`null` task-settings variant is exactly what `M0` uses when its map is empty;
do not force it when the device exposes options.

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

All numeric fields above are Java/Kotlin `int` → JSON numbers. Filename/path/key
fields are strings. The nullable slice/settings/AMS properties are objects when
populated in other flows, but remain null in this traced constructor. They are
not needed to reconstruct this already-sliced cloud-file branch.

`task_settings` is null or an object built from current option selections:

- Item type 1: `settings[option.name] = option.default_value`, an integer.
- Item type 2 with `PrinterFunctionType.STIR_PRINT`: add integer
  `stirBeforePrint`; only when selected (`1`), also add
  `stir_settings: {"stirTime": <INT>}`. The nested field really is camelCase
  `stirTime` (`StirBeforePrintSetting.java`), not `stir_time`.
- Other item types are not added by this LCD `M0` method.

Option names, whether these options are supported on model 130, and their actual
read-back/UI-selected values are **unknown without device/server responses**.
They must not be guessed from APW or an FDM example.

### Field provenance

| Field | APK evidence for this flow |
|---|---|
| `printer_id` | Selected printer context; overwritten with activity printer ID again in `M0` |
| `file_id` | `FileResponse.id` (`Q:982`, `t.d.g:385`), **not** `gcode_id`; use registration's cloud-library ID |
| `project_id` | Base constructor default `0`; neither cloud-tab constructor nor LCD `M0` changes it; no prior project creation here |
| `project_type` | Explicit integer `1` in the cloud-tab/use-case constructors |
| `filename`, `file_name` | Empty constructor defaults in this branch; original filename stays UI/library metadata, not populated in this request |
| `filesize`, `taskid`, `task_mode` | Not fields of these request classes and not added by this dispatch chain; no client-side generation/acquisition here |
| returned `task_id` | Integer in `SendPrinterResponse`; correlation/generation inside server/firmware is outside APK evidence |
| `is_delete_file` | Integer `0`, **inside data**, from constructor default; no top-level deletion flag |

Other Android flows differ. For example, `SelectPrinterActivity.L0` (942–975)
and `ac.cloud.common.navigator.a.g` use `project_type=3` and fill official/model
fields. These are not evidence to replace `project_type=1` in the already-sliced
cloud-library path. `SliceDetailsActivity` (130–139) and `SliceFileActivity`
(173–180) independently corroborate type 1 for slice-file selection.

## Ordered workflow and prerequisites

1. **Select the printer and library file.** MainApiService (`d1/a.java`) exposes
   `GET api/work/printer/getPrinters`, `GET api/v2/printer/info?id=…`, and Android
   `GET api/work/index/files` with page/limit and optional/contextual printable,
   machine_type, keyword and folder_id arguments (193–194). This differs from
   the working web POST listing; do not change photonctl's working method.
2. **Check file state.** `LCDPrintableFileCloudNewFragment.Q` rejects status `3`
   with an error, blocks status `2` as rendering for file_type `1`, and constructs
   the print request only for the slice branch `file_type=1`. Model `file_type=2`
   goes to slicing instead and is outside our goal. A mere library record is not
   sufficient evidence of readiness.
3. **Pass the request into LCDStartPrintActivity.** ARouter passes the request,
   printer ID, device key, machine type and thumbnail. `initView` acquires an
   MQTT session through `z1.a` and calls `A0` → `D0(false)`.
4. **Fetch supported print options:** FileApiService `i` makes
   `POST api/v2/device/getPrinterOptions`, **form-urlencoded**
   `model_id=<MACHINE_TYPE_INT>&printer_id=<PRINTER_ID_INT>` (here model 130).
   StartPrintViewModel `d` → OnlineSliceRepository coroutine `e` → API `i`.
   Callback `LCDStartPrintActivity.b.b` initially sets each option's value to 0,
   installs the list, optionally queries stir properties, then calls `B0`.
5. **Read settings from the printer:** `B0` builds `SendStatusCommandRequest`,
   explicitly sets data null, order **1228**, printer ID, leaving project ID 0.
   It uses the same JSON sendOrder transport:

   ```jsonc
   {"printer_id": <PRINTER_ID_INT>, "order_id": 1228,
    "project_id": 0, "data": null}
   ```

   The activity subscribes to event key **1225** targeting `CurrentStatusData`
   (`createObserver:786–791`). `onMqttPrintM7proAutoOperationEvent` (831–864)
   matches `currentStatus[].name` to options, requires `support==1`, and updates
   option values from `status`. This is asynchronous state initialization, not
   an HTTP 1225 settings write and not a chained HTTP acknowledgement.
6. **Conditional reads while configuring:** if STIR_PRINT is present,
   `E0` → StartPrintViewModel `f` → repository coroutine `f` calls
   `POST api/v5/printer/properties/query` with JSON
   `{"printer_id": <INT>, "properties": ["stir_settings"]}`.
   This may overlap order 1228; there is no single synchronous total ordering.
   Enabling certain non-stir options invokes `I0`: JSON sendOrder order **1231**,
   printer ID, project ID 0, no data field (plain BaseCommandRequest). Event 1231
   returns peripheral state; AI/timelapse enablement checks `camera==1`.
   These read-like orders still contact a control endpoint and were **not sent**
   in this investigation.
7. **Start button:** `H0` calls `M0`, which materializes the settings map, updates
   request printer/order, and sends order **1** JSON once. This method does not
   first send order 1225, create a project, or request a taskid. `M0` explicitly
   disables the generic MQTT countdown and progress dialog for this call.
8. **Handle API response separately from device state:** see next section.

## Order 1225: what the APK actually shows

`MQTTConstants.java` defines `COMMAND_ID_GET_M7_AUTO_OPERATION=1228` and
`COMMAND_ID_M7_AUTO_OPERATION=1225` (`classes3.dex`, lines 22,30).
A real **1225 write** exists in
`ac.cloud.workbench.control.activity.M7ProAutomaticInterventionActivity.p0`
(`classes2.dex`, 273–302), entered when changing automatic-intervention settings.
It checks its state predicate, changes the selected item in the complete current
function list, and sends:

```jsonc
{
  "printer_id": <PRINTER_ID_INT>,
  "order_id": 1225,
  "project_id": 0,
  "data": {
    "currentStatus": [
      {
        "name": "<SERVER_OPTION_NAME>",
        "help_url": "<SERVER_METADATA_OR_NULL>",
        "title": "<SERVER_METADATA_OR_NULL>",
        "des": "<SERVER_METADATA_OR_NULL>",
        "support": <INT>,
        "editable": <INT>,
        "status": <SELECTED_INT>
      }
    ]
  }
}
```

The list items are `ac.cloud.publicbean.main.data.response.AdvanceParam`
(`classes3.dex`, fields 14–20, constructor 221–229); request wrapper is
`SendM7AutoOperationCommandRequest.CurrentStatusInfo` (`classes3.dex`). This is a
retained server/device list, not a reason to fabricate a one-item settings list.
The activity's name says M7Pro; that alone does not prove model-130 support or a
requirement to invoke it. No 1225 write constructor was found in the traced LCD
start activity. The earlier APW pre-start behavior must remain separate evidence.

## Model 130 and the v5 route

130 satisfies `PrinterTypeUtils.getPrinterTypeById`'s LCD rule. Both
`LCDPrintableFileCloudNewFragment.Q` and `t.d.g` route a cloud slice into the LCD
activity. Its final `M0` calls the shared legacy HTTP sendOrder path without a
machine-specific v5 switch. Thus **this recovered model-130 cloud-file start path
uses sendOrder**, even though its option-query API can have a v5 prefix.

The APK also contains `CommandApiService.callDeviceService` at
`POST api/v5/devices/{module}/call` (47–48). Its separate constructor is
`NewPrinterProtocolRepository.callService` (367–369), whose coroutine invokes
that method at 72, reached through `o.a.a` (compiled
`NewPrinterHttpCommandClient.kt`). No edge from the traced LCD cloud-file start
chain to that client was found. Do not invent module/service parameters or infer
that the presence of v5 means model 130 must use it.

## Acknowledgement, monitoring, and readiness

`SendPrinterResponse` contains string `msgid` and integer `task_id` (default 0).
BaseRepository `requestResponse` unwraps successful response data;
`ApiResponse.isSuccess` accepts codes 1 or 200. Then
`BaseMqttHandleActivity$sendMqttCommand$1$2.invoke2` calls the UI success callback
for non-null data; it wraps it in a local ApiResponse with code 200. That locally
constructed code must not be confused with a device acknowledgement.

`LCDStartPrintActivity.k.a` (506–514) does not inspect msgid/task_id: it dismisses
loading, posts local app event 1009, navigates to the printer workbench, and
finishes the start screen. Since `M0` disables `needCountDown`, this transition
is **HTTP/API success**, not proof that a matching device accepted or began the
print. The start screen has no task-status polling loop in this path.

Subsequent device observation is separate. The shared MQTT handler forwards key
10003 to `onMqttPrintStatusEvent`; `LCDPrinterDetailsActivity` (3290–3310) handles
states `offline`, `busy`, `free` for the matching device. The app also declares
read APIs such as printer info and `GET api/v5/project/info?id=…` (`j2/a.java`),
but this does not establish a complete robust receipt/correlation algorithm for
photonctl. A real acceptance check must associate the intended file/new task,
not merely see an arbitrary busy device.

`SelectPrinterActivity.x0` (1049–1064) filters its **DeviceInfoBySupportResponse**
list by `available==1 && device_status==1 && is_printing==1`; the source endpoint
is `GET api/v5/printer/status/list` with file_id/file_ext/only_slice/include_all
(`d1/a.java:244–245`). These names and predicates are endpoint-specific: do not
apply them blindly to the older printer-list snapshot's different status fields.
The direct already-selected-printer path does not repeat this predicate in
`LCDStartPrintActivity.M0`. Exact readiness meanings and physical setup remain
requirements for future implementation, not inferred from numeric names.

## Comparison with existing reports and photonctl

- `discovery/web-bundle-control-report.md` remains consistent: no print client in
  that web deployment. The APK supplies new native-client evidence.
- `discovery/print-start.md` and the current `print_dry_run` intentionally preserve
  partial **desktop** findings. They should not be treated as the Android schema.
- A future Android-mode dry-run should use JSON, integer file ID, project type 1,
  project ID 0, default empty filename/path fields, nested deletion flag 0 and
  dynamic task_settings. It should not add taskid/task_mode/filesize or a mandatory
  1225 write. Preserve an explicitly labelled APW hypothesis if needed; do not
  silently mix the two flows.
- The current live-working signing/session/upload/library logic must remain
  unchanged. Android's relative `api/...` Retrofit path does not justify blindly
  appending another `api/` to photonctl's proxy base, which already ends in `/api`.
  API host/proxy acceptance of Android JSON remains untested here.

## Remaining unknowns and concrete implementation plan

1. **Recover actual model-130 options/state without starting anything.** The
   smallest next step is a separately authorized read-only call/capture of
   `getPrinterOptions(model_id=130, printer_id=<ID>)`, plus the chosen file's
   ready library record and printer's file-specific status response. This pass
   did not make those requests. Current option values requiring 1228 or MQTT
   need a later expressly authorized, no-start observation; do not classify
   sendOrder calls as ordinary read endpoints in development tooling.
2. **Update only the offline preview first.** Build/test the full Android JSON
   schema above with explicit numeric types and nulls, defaults sourced from the
   APK, and server-derived settings. Surface unsupported/unresolved settings;
   do not silently send all-zero options. Require a ready `.pm4u` library record,
   the correct model, and explicit selected printer/file IDs. No slicing/upload
   changes, no settings writes, no v5 dispatch fallback.
3. **Verify request bytes without forwarding a start.** Compare the offline body
   against an authorized official-client capture/interception where **all**
   printer-control requests are blocked before network forwarding. Static
   constructor evidence is strong, but live body/settings and proxy routing have
   not been tested. Do not click Start relying only on a printer being offline.
4. **Implement dispatch only in a later approved change.** Reuse signing and the
   known proxy path; send one JSON order 1 with no automatic retries or fallback
   controls. Treat a timeout as unknown outcome, and report msgid/task_id as API
   acknowledgement. Require explicit approval and operator readiness before any
   real print, regardless of successful dry-run or API response.
5. **Verify acceptance separately.** Define and mock-test a bounded read-only
   task/printer observation tied to returned task_id, selected printer and file.
   Fail/report ambiguous state rather than resending. Determine the needed state
   transitions from an approved controlled test; do not equate UI navigation,
   code 1/200, an unrelated busy state, or file registration with print acceptance.

No production code changes or real control experiments are part of this report.
