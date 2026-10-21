# Focused Mono 4 Ultra print-start investigation

Existing `upload`, `files`, signing, credentials and login functions were preserved
byte-for-byte. No local APW DLLs or saved printer/control captures were found.

## Printer discovery evidence and live read checks

Primary source for the client implementation (third-party, not official API docs):
https://github.com/vitkuz573/anycubic-mcp/blob/8911d561b85acced86c07625ced69c9f6172d0ff/src/anycubic.ts

Its `listPrinters()` uses `GET /work/printer/getPrinters` without parameters;
`printerInfo(printerId)` uses `GET /v2/printer/info?id=<printerId>`.
Both methods were independently checked with this repository's cached session and
unchanged signing against the known working API base. Both returned code 1.

Live snapshot: registered printer ID 797953, machine_type 130, model/name
Anycubic Photon Mono 4 Ultra, firmware 4.0.7.9. List reported status 1,
device_status 2, is_printing 1, ready_status 0. Info also reported device_status 2
and is_printing 1. These are raw values; their online/idle/readiness meanings for
model 130 have NOT been verified. No explicit online/is_online field was returned
in the displayed metadata. Do not equate any of these values with safe readiness.

The new commands return only allowlisted printer identity/model/state fields;
serial numbers, account IDs, MQTT credentials, addresses and unrelated identifiers
are omitted, including numeric values. Missing fields are not inferred.

## Print-request evidence boundary

The user-supplied APW static analysis establishes the following partial shape:
`POST /work/operation/sendOrder`, form-urlencoded fields `printer_id`, `project_id`,
`order_id`, JSON-encoded `data`, and sometimes `is_delete_file`. Order 1's data has
`taskid`, `task_mode=1`, `filename`, `filesize`, `project_type`, `filetype=0`, and
`file_id` as a decimal string. Model 130 first receives settings order 1225.
The response contains `msgid` and `task_id`.

The public client's startPrintCloud implementation targets an FDM/AMS workflow,
uses JSON and guessed/default settings, and supplies neither this taskid sequence
nor the complete resin settings order. It is not evidence for a complete Mono 4
Ultra request. None of its control logic or authentication was adopted.

Unresolved values and checks:

- **taskid:** format, source, uniqueness, lifecycle and correlation with the
  returned task_id. Do not invent a UUID or timestamp.
- **project_id/project_type:** whether a separate project is created or selected,
  and the correct type for an already uploaded resin file. Do not substitute 0/1
  or use an unrelated/latest print-history project.
- **1225:** complete settings data, model/firmware-dependent options, serialization,
  required ordering and whether device acknowledgement is needed before order 1.
- **State/capabilities:** identify model 130 and supported firmware/cloud printing;
  establish online, idle, error/interlock and device readiness meanings from the
  official client. Exact required checks remain unknown. Physical printer setup
  still requires operator confirmation before any eventual real start.
- **File fields:** confirm original filename vs storage name/path, exact byte size,
  and newUploadFile's registered data.id vs the record's distinct gcode_id. Keep
  file_id decimal text as indicated by APW; verify this for the chosen flow.
- **Deletion flag:** whether is_delete_file is required, at which level, and its
  non-deleting value. It is omitted from the incomplete preview, not guessed.
- **Acknowledgement:** code 1/msgid/task_id may establish API dispatch only. Device
  acceptance must be correlated with actual printer/cloud task state (or the
  official client's device acknowledgement). No receipt or readiness is implied.

`print ... --dry-run` is an offline incomplete template, not a ready request.
Unknowns are null placeholders explicitly forbidden to transmit. It requires no
credentials, performs no network calls and has no control dispatch implementation.
Omitting --dry-run fails. It does not look up or certify the supplied IDs.

## Smallest safe next experiment

Capture the official client's printer-info/file-selection traffic and inspect its
request builder at the final confirmation screen, without pressing Start. Record
how it resolves the selected cloud file and project, generates taskid, constructs
1225, and gates readiness. If the final requests are built only after clicking
Start, first use a verified interception setup that blocks **all** sendOrder
requests before forwarding; do not rely on a printer being offline. That setup
must be validated before any click. Save only sanitized payloads and no tokens,
serials, signed URLs or storage paths. No interception or control experiment was
performed here. A real print remains a separate explicitly approved experiment.
