# Read-only printer connectivity diagnostic

## Command

```bash
python3 photonctl.py printer-status 797953
```

Uses the existing cached session/environment override, signing and API base. It
makes **one GET `/v2/printer/info?id=797953`**, with redirects disabled and the
existing connect/read timeouts. No file ID is required. It makes no POST/PUT,
`sendOrder` (including 1228), v5 device calls, refresh requests or automatic retry.
The APK is not executed. Only local static analysis and mocks were used for this
change; no live cloud request was performed.

## Why not `/v5/printer/status/list`?

Evidence is the same APK and JADX output identified in
[android-print-start.md](android-print-start.md). Paths below are relative to
`/tmp/photon-android-jadx/sources/`; decompiled output remains outside Git.

`d1/a.java` (MainApiService.kt, classes2.dex), lines 244–245:

```text
@GET("api/v5/printer/status/list")
u(@Query("file_id") int fileId,
  @Query("file_ext") String fileExt,
  @Query("only_slice") int onlySlice,
  @Query("include_all") boolean includeAll, Continuation...)
    -> ApiResponse<ArrayList<DeviceInfoBySupportResponse>>
```

The first annotation uses `FontsContractCompat.Columns.FILE_ID`, whose value is
`file_id`. There is **no printer-ID query parameter**. The file ID is a primitive
integer, not a nullable omitted argument.

Actual caller:
`ac/cloud/workbench/main/viewmodel/a.java`, coroutine `f0.invokeSuspend`,
lines 645–699 (MainRepository.getSelectPrintersBySupport):

```text
api.u(fileId, fileExt, onlySlice ? 1 : 0, true, continuation)
```

`SelectPrinterActivity.x0` (1049–1064) filters those results using
`available == 1 && device_status == 1 && is_printing == 1`.
This is a **file-specific compatible/selectable printer list**, not evidence that
an arbitrary printer can be queried without a file. No supported printer-only
variant or file-ID sentinel was found. The command does not guess file_id=0 or
interpret absence from this list as offline.

Response class:
`ac/cloud/workbench/main/data/response/DeviceInfoBySupportResponse.java`,
classes2.dex, fields 22–34:

| Fields | APK types |
|---|---|
| `id`, `model_id`, `available`, `device_status`, `is_printing` | int |
| `name`, `model_name`, `machine_type`, `img`, `key` | String |
| `max_box_num` | nullable Integer |
| `machine_data` | MachineData object |
| `type_function_ids` | List<Integer> |

The availability predicate concerns that endpoint and the selected file. Its
field names do not establish shared numeric semantics for every other API.

## Selected endpoint and connectivity evidence

`d1/a.java:231–232` declares
`GET api/v2/printer/info` with nullable `@Query("id") Integer` returning
`ApiResponse<PrinterDetailsNewResponse>`. The app's MainRepository coroutine `x`
(`ac/cloud/workbench/main/viewmodel/a.java:2456–2499`) calls `api.q(id, ...)`.
It is already used by photonctl's working `printer-info`.

`ac/cloud/workbench/main/activity/printer/LCDPrinterDetailsActivity.java`,
classes2.dex, provides direct semantics for **this response**:

- `p2()` (2244 onward): if the response is absent or `device_status != 1`, call
  `j2()`; otherwise choose busy/free UI separately using `is_printing`.
- `j2()` (2106–2123): displays `main_printer_offline_icon`,
  `new_printer_offline`, and `main_printer_disconnect`.
- `o1()` (2217 onward) displays the busy UI; `y1()` (2809 onward) displays the
  available print button. Thus `is_printing` is not the connectivity discriminator.

The implementation conservatively recognizes only integer `device_status=1`
(ONLINE) and `2` (OFFLINE, which takes the APK's offline branch). Other values,
including missing/null, booleans and numeric strings, are UNKNOWN rather than
extending the app's blanket non-1 branch to future or malformed values.
The interpretation is restricted to integer LCD `machine_type` values in
`1..9999`, following `PrinterTypeUtils`' LCD/FDM split; model 130 is included.
Missing model metadata or an FDM model produces UNKNOWN because this inspection
did not establish that model family's UI semantics.

Another appropriate-by-ID read exists in the APK:
`m0/a.java:52–53` (ControlApiService.kt, classes2.dex) declares
`GET api/v2/Printer/status?id=<int>` returning `PrinterStatusResponse` with integer
`device_status`, `id`, `is_printing`, `machine_type` and string `key`.
ControlRepository coroutine `d.invokeSuspend` calls it at lines 210–226.
Its control-screen callbacks use combinations of device_status/is_printing for
operation state, but those are weaker connectivity evidence than the explicit
LCD offline rendering above. It is not called as a fallback, and no semantics
are transferred between these two endpoints. The command reuses the already
working info endpoint and its existing GET helper unchanged.

Android Retrofit paths include `api/`; photonctl's configured proxy base already
ends in `/api`, so the command appends `/v2/printer/info`, not another `/api`.

## Output and failure behavior

Output displays allowlisted, sanitized raw discovery/status metadata, including
ID/model, `device_status`, `is_printing`, and firmware/timestamps where returned.
No credentials, device key, account ID, URLs or unrelated identifiers are printed.
Raw numeric flags are not rewritten. The new interpretation is a separate line:

```text
Connectivity: ONLINE (cloud-reported)
```

or OFFLINE/UNKNOWN, followed by the evidence/reason. The result is **the cloud's
snapshot**, not a network probe or proof of current power/readiness. Freshness is
not guaranteed: unplugging a printer may take time to propagate. Cached
`is_printing`, missing data, an unavailable API, or absent registration never
establish offline.

Identity must be present and match the requested printer. Missing/empty data,
malformed structures or IDs, mismatched identity, invalid JSON, rejected API
codes, HTTP errors (including 401/404), timeouts, connection errors and missing
sessions report UNKNOWN with an explanation and exit 1. Existing GET error
handling suppresses unsafe server messages and transport exception strings. API
failures mention reimporting the session via `login` if it expired; no credentials
are requested or refreshed automatically. A well-formed matching snapshot exits
0, even when connectivity is UNKNOWN because a status/model value is unsupported.

All existing helper functions, including authentication, upload, listing, printer
discovery and Android dry-run constructors, were preserved byte-for-byte. Only
new status logic and CLI dispatch were added.

## Tests and live check

```bash
python3 -B -m unittest discover -s tests -v
```

26 tests pass. New mock coverage includes ONLINE/OFFLINE from device_status with
the same is_printing value, unknown/missing flags, missing/malformed/mismatched
records, unsupported model semantics, expired/API/HTTP failures, invalid JSON,
timeouts, secret redaction, exact signed GET query and no write calls. Existing
upload and print-preview tests still pass.

With the printer physically off, run the command above using your cached session.
Repeat manually after allowing cloud disconnect propagation; compare the printed
raw `device_status` with the Android app. UNKNOWN or a delayed ONLINE result is
not proof the printer is powered on. If useful, compare again after powering on,
without starting any operation. A cloud-reported ONLINE result is not permission
to print and does not certify readiness or task acceptance.
