# Anycubic file-manager bundle inspection

**Result:** No printer-control route or constructor was found in this deployment's
seven publicly served JavaScript bundles. They reveal no missing print-start
values for photonctl. Stop here: the evidence does not justify assuming this web
application contains a hidden printer-control client.

## Scope and method

Retrieved the public HTML at https://cloud-universe.anycubic.com/file and its two
script assets. The webpack runtime names five additional lazy chunks; all five
were also retrieved, including routes other than `/file`. No browser session,
cookies, credentials, authenticated APIs or control requests were used. No website
JavaScript was executed. Existing photonctl code was not modified.

All URLs below are relative to `https://cloud-universe.anycubic.com/js/`:

| Bundle | Role from router/code | Decoder | Index base / table rotation |
|---|---|---|---|
| `app.982cbff9.js` | Bootstrap, router, API wrapper, login | `a0_0x51f5` | `0x191` / 19 |
| `chunk-vendors.0f830f0e.js` | Shared dependencies | `a6_0x5ec7` | `0xb6` / 183 |
| `705.c7f36f4e.js` | `/file` | `a2_0x3b29` | `0xee` / 294 |
| `223.67d219c4.js` | `/fileDetail`, preview dependencies | `a3_0x5953` | `0x174` / 428 |
| `278.d813658c.js` | `/login` | `a1_0x1037` | `0x93` / 8 |
| `858.fb64dc15.js` | `/ai` wrapper | `a5_0x17a3` | `0x8c` / 66 |
| `920.eb0d951b.js` | `/appDownload` | `a4_0x5ca8` | `0x77` / 214 |

Static Python analysis extracted literal string arrays, identified each decoder's
index subtraction, and recovered its left rotation by matching the bootstrap's
numeric checksum. Only allowlisted numeric arithmetic was interpreted; neither
JavaScript nor general expressions were evaluated. Decoder aliases and constant
lookups were then substituted for call-site inspection. All seven checksums
matched. This bypasses the limitation of searching only visible endpoint literals.
Translation tables, graphics libraries and UI code were not otherwise analyzed.

## Relevant endpoint inventory and function locations

Locations are zero-based **byte offsets in the original minified UTF-8 bundle**;
function names are additional search anchors. Normalize leading `/` when reading
relative API paths. The API wrapper configures the already-known base
`https://cloud-universe.anycubic.com/p/p/workbench/api`.

| Method / path | Constructor and evidence |
|---|---|
| POST `v2/cloud_storage/lockStorageSpace` | 705, `lockStorageSpace`, byte 29793: `{name, size, is_temp_file: 0}` |
| PUT returned `preSignUrl` | 705 upload code: original raw file wrapped in a Blob; no print request |
| POST `v2/profile/newUploadFile` | 705, `submitUploadFile`, byte 26822: only `{user_lock_space_id}` |
| POST `v2/cloud_storage/unlockStorageSpace` | 705, `unlockStorageSpace`, byte 4248 (shared header) and file component: `{id, is_delete_cos}`; successful confirmation calls `spliceUpload(..., 0)` |
| POST `work/index/files` | 705, `getFilesList`, byte 30578: `{page: pageNum, limit}` |
| POST `/work/index/getUploadStatus` | 705, `detailTo`, byte 25425: `{id}`; also called before download/preview |
| POST `/work/index/getDowdLoadUrl` | 705, `downMx`, byte 34821: `{id}`, after upload-status lookup; spelling is literal |
| POST `work/index/getUserStore` | 705, `getUserStore`, byte 29398: no body argument |
| POST `work/index/delFiles` | 705, `delFiles`, byte 26244: `{idArr}` |
| POST `work/index/renameFile` | 705, `finishInputMask`, byte 36016: `{id, name}` |
| POST `work/index/getModelFileInfo` | 223, `getModelDetailData`, byte 750944: `{id}` |
| GET `/api/work/gcode/info?id=…` or `/api/work/gcode/infoFdm?id=…` | 223, `getSliceDetailData`, byte 751636: selected by file extension, query value from `gcode_id`; paths are literal, not independently tested |

Other recovered calls are login/profile/logout, `portal/index/download`, and
`buried/report` telemetry. The AI wrapper embeds a separate AI application; that
separate application's assets were outside this inspection.

## Print-start relevance and limits

Neither string tables nor decoded call sites contain `/work/operation/sendOrder`,
`order_id`, `taskid`, `project_type`, or `printer_id`. No order-1225 constructor was
found. Incidental numeric/identifier matches are not control evidence.

The detail UI reads `slice_param.basic_control_param` and `printer_settings_id`
for displaying lift/exposure/model metadata. That is not an outbound settings
payload. Nothing recovered establishes taskid generation, project creation,
1225 prerequisites, printer readiness checks or printer acknowledgement handling.

This is a negative finding for the inspected assets, not proof about all Anycubic
applications, embedded services, other deployments or native clients. No endpoints
listed here were called to test their behavior. Further print-start investigation
needs evidence from the actual controlling client, not more file-manager UI
analysis.
