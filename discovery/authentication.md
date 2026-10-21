# Authentication investigation: native password login is blocked

## Outcome

**Native username/password login is not established and was not implemented.**
This APK's first-party login UI is a hosted Casdoor page in a WebView. The DEX
contains a complete authorization-code/PKCE exchange and the subsequent QCloud
session exchange, but not the hosted page's password submission protocol.
Implementing email/password, choosing a password hash, or assuming a resource-owner
password grant from these findings would invent the missing step. Browser
automation, QR login and OAuth alternatives are outside this task's boundaries.

Consequently `photonctl.py` is unchanged: `login` still imports the existing
`PHOTONCTL_TOKEN` / `PHOTONCTL_API_BASE` pair, stores a session atomically at
`~/.config/photonctl/session.json` with mode 0600, and performs no authentication
request. No native login test command is available. Native login is **not ready
for live verification**; static analysis alone would not validate it even after
implementation.

No credentials were used/requested, no authentication or printer-control endpoint
was called, no APK code was executed, and nothing was committed. New regression
tests protect the existing import-only fallback and failed-login session safety.

## Inputs and method

Read the existing `discovery/` reports and the current hierarchical CLI/tests.
APK: `discovery/apks/base.apk` (moved from the earlier `discovery/apk/` location),
package `ac.cloud.com`, versionName **2.0.0**, versionCode **204**.
SHA-256: `c992b653e1eb3bbdf935d875e495aaa1b89aac39a1afc7de355a48f873114986`.
This matches the prior eight-DEX decompilation's input.

Reused JADX 1.5.6 output at `/tmp/photon-android-jadx/sources/`, which covers all
8 DEX files. Searched all generated sources for authentication Retrofit methods,
legacy password-login paths, password grants, token refresh and relevant callers.
The previous decompilation reported 89 errors globally; absence findings are
limited to recoverable static code, not proof about every possible remote/runtime
implementation. No native password login was found in the app login module or
Casdoor SDK. Third-party AWS/social-account libraries are not evidence for an
Anycubic email/password-to-QCloud flow.

Decoded APK resources only with:

```bash
jadx -s -d /tmp/photon-auth-res discovery/apks/base.apk
```

Resources and generated sources stay outside Git. Read the previously decoded
public web application bundle `/tmp/photon-web-app.982cbff9.decoded.txt`; no new
network retrieval was needed. Relevant source URL:
https://cloud-universe.anycubic.com/js/app.982cbff9.js

## Actual Android sequence and source locations

Java paths below are relative to `/tmp/photon-android-jadx/sources/`. They preserve
obfuscated class names and reconstructed method names for reproducibility.

| DEX | Class / method | Evidence |
|---|---|---|
| 2 | `ac.cloud.workbench.login.i18n.activity.LoginVideoActivityI18N.u0` | Navigates to `/login/LoginActivity` |
| 2 | `ac.cloud.workbench.login.i18n.activity.LoginForCasdoorActivity` | Hosted login activity extending H5CommonDisplayActivity; `t1` constructs sign-in URL, `L0` consumes callback code, `r1` exchanges code, `p1` starts QCloud exchange |
| 1 | `e.a.a`, `e.b.c` (ConfigDerived/ConfigTools) | Loads Casdoor resource configuration; selects release/test environment |
| 5 | `mg.b` (Casdoor.kt), `a`, `c` | Authorization URL and code-to-access-token HTTP implementation |
| 5 | `mg.CodeRequestQuery`, `mg.AccessTokenRequest` | Exact authorize/code-exchange query fields and defaults |
| 5 | `mg.e` (Utils.kt) | PKCE verifier, nonce, SHA-256 challenge generation |
| 2 | `ac.cloud.workbench.login.i18n.viewmodel.LoginViewModel`, `LoginRepository` (`a.java`, coroutine C0135a) | Wraps loginForCasdoor; repository calls `n0.a.b.a` with default device type |
| 2 | `n0.a` (LoginApiService.kt), method `a`, lines 51–53 | Form-urlencoded `POST api/v3/public/loginWithAccessToken` |
| 2 | `n0.b` (LoginNetworkApi.kt) | Uses normal signed NetworkApi client |
| 2 | `ac.cloud.workbench.login.i18n.data.response.UserInfoResponse` | QCloud `token: String`, `login_status: int`, `user: User` |
| 6 | `m4.b.E/q`, `y/b`, `n` | Separate QCloud/access-token storage, push-registration key |
| 6 | `p4.b.intercept` (MyHeadInterceptor.kt) | Reads stored QCloud token into XX-Token and signs requests |
| 6 | `p4.c.intercept`, `m4.j` | Session-invalid response handling; no refresh call |

### 1. Hosted login, not a native credentials POST

`LoginForCasdoorActivity.t1` (303–319) obtains release configuration and constructs
`mg.b`; `initView` (253–263) clears old app tokens and browser cookies before
loading the hosted page. This Android behavior is **not** adopted by photonctl:
a failed login must preserve its existing session.

Release resource values in `/tmp/photon-auth-res/resources/res/values/strings.xml`:
`release_cas_door_endpoint = https://cas.anycubic.com`, organization `anycubic`,
app name `ac_android`, redirect `anycubic-i18n://cloud.anycubic.com:8088`.
The configured client ID is a public application identifier, not a password or
an XX-Token. It is not necessary to copy it into production while this flow is
blocked.

`mg.b.a` / CodeRequestQuery constructs this public authorization URL:

```text
GET https://cas.anycubic.com/login/oauth/authorize
    ?client_id=<configured Android client ID>
    &response_type=code
    &scope=profile
    &state=ac_android
    &nonce=<generated nonce>
    &code_challenge_method=S256
    &code_challenge=<PKCE challenge>
    &redirect_uri=<configured Android callback>
```

The activity additionally appends analytics identifiers. `mg.e` generates an
84-character alphanumeric verifier and 10-character nonce and derives the challenge
using SHA-256/Base64 URL-safe encoding. These transform the **PKCE verifier**, not
the account password. This is not evidence that Anycubic passwords use SHA-256,
MD5, RSA or any other transformation.

**Missing:** the hosted page's email/phone credential endpoint, HTTP body,
password treatment, CSRF/cookie/challenge requirements, country-code handling,
CAPTCHA/MFA behavior and account eligibility. No native request in this chain
contains email, phone number or password. There is no supported distinction to
implement between email and phone login from this evidence.

### 2. Exchange the callback code (not implemented)

`LoginForCasdoorActivity.L0` (225–240) intercepts the URL's `code` query parameter,
then `r1` (184–205) invokes `mg.b.c(code)` on a worker thread.
`mg.CasdoorConfig` normalizes the endpoint and sets API endpoint `<endpoint>/api/`.
The SDK constructs:

```text
POST https://cas.anycubic.com/api/login/oauth/access_token
     ?grant_type=authorization_code
     &client_id=<configured Android client ID>
     &code=<callback authorization code>
     &code_verifier=<the original verifier>
```

The parameters are **URL query parameters**, not a credentials JSON or form body.
`mg.b.c` calls OkHttp builder `i(ag.d.f12332d)`: `okhttp3.b0.a.i` (228–230) means
POST, and `ag.d` (89–93) defines an empty RequestBody with no content type. This
uses a separate plain OkHttp client, not the QCloud signed client. None was sent.

`org.casdoor.AccessTokenResponse` (classes5.dex) maps JSON fields `access_token`,
`error`, `expires_in`, `id_token`, `refresh_token`, `scope`, `token_type`.
The SDK requires a non-null refresh token before accepting this response; that
requirement does not demonstrate an actual refresh implementation. The activity
passes only `access_token` to its QCloud exchange.

### 3. Exchange access token for the distinct QCloud session

`LoginForCasdoorActivity.p1` (294–299) passes the OAuth token and `m4.b.n()` to
LoginViewModel; LoginRepository → `n0.a.b.a` selects default `device_type=android`.
Retrofit `n0.a.a` declares:

```text
POST <Android configured API base>/api/v3/public/loginWithAccessToken
Content-Type: application/x-www-form-urlencoded

access_token=<OAuth access token>&message_key=<push registration ID>&device_type=android
```

`message_key` comes from MMKV `KEY_PUBLIC_PUSH_ID`, default empty string; it is not
a password, task ID or the request-signing nonce. Whether empty is accepted by
the server on a Linux client is untested. The Android base is supplied by
`e.b.y` / ConfigConstants; the working CLI proxy base already ends in `/api`.
The same API path exists in the web proxy, but Android field/header acceptance
there has not been tested. Do not duplicate `/api` when building CLI paths.

Expected response model (placeholders only):

```jsonc
{
  "code": <API_CODE>,
  "msg": "<SERVER_MESSAGE>",
  "data": {
    "token": "<QCLOUD_SESSION_TOKEN>",
    "login_status": <INTEGER>,
    "user": { "<user metadata>": "<values>" }
  }
}
```

`LoginForCasdoorActivity.a.b` (89–109) stores `UserInfoResponse.token` using
`m4.b.E` → `KEY_PUBLIC_TOKEN`; it stores the original access token separately via
`m4.b.y` → `KEY_PUBLIC_ACCESS_TOKEN`. `p4.b.intercept` reads `m4.b.q()`
(`KEY_PUBLIC_TOKEN`) for XX-Token. **This proves the exchange token type needed by
existing cloud APIs. It does not prove that an arbitrary Makeronline JWT is
accepted as either this exchange's input or the resulting XX-Token.** The activity
stores user metadata as well; `login_status` account-state meanings are not mapped
by this callback.

### Headers, identity and signing

`p4.b` (classes6.dex, 21–60), used by `n0.b` through `m4.i`, supplies:

- XX-Token: the stored QCloud session, initially empty on this Android login page.
- XX-Device-Type: `android`; XX-Version: runtime app version (this APK 2.0.0).
- XX-Timestamp: Unix milliseconds; XX-Nonce: `m4.l.a()` generated nonce.
- XX-Signature: uppercase MD5 over the established app-key + timestamp + version
  + app-secret + nonce + app-key material. Keys are loaded from packaged resources
  by `e.a.a`, not derived from the account password.
- XX-Device-Id: `com.blankj.utilcode.util.k.f()` persistent UDID. DeviceUtils
  derives/persists it using Android ID when available, otherwise a UUID. A Linux
  substitute and any server-side binding requirement are **not established**.
- XX-IS-CN: `0`; XX-LANGUAGE: selected language, default `US`.

The working CLI's PC/version/signing behavior is unchanged. Its working use for
file/printer APIs does not by itself verify a native credential endpoint or prove
that Android's login metadata can be omitted/substituted.

## Token lifecycle, errors and logout

- QCloud response model has no established expiry timestamp or refresh credential.
  OAuth `expires_in` is not a QCloud token lifetime. No lifetime is assumed.
- `p4.c.TokenInterceptor.intercept` checks body codes against
  `m4.j.f31802d = [10001,31011,31012,31013,31014,31015,31016]` and routes to the
  app's translucent login-related UI. It does not refresh or replay the request.
  These are grouped reauthentication-related codes, not an independently verified
  per-code account/password error taxonomy.
- No QCloud refresh endpoint or complete OAuth refresh call was found in the
  inspected authentication/SDK code. Merely having a `refresh_token` property in a
  response is insufficient. No refresh implementation was added.
- Incorrect-password, unsupported-account, email/phone eligibility and challenge
  errors belong to the hosted credentials stage; exact codes and schemas are
  **unknown**. The QCloud exchange error callback (LoginForCasdoorActivity.b)
  shows the repository error, but does not establish those credentials errors.
- `m2.b.j` (classes2.dex, 74–75) declares `POST api/user/public/logout` with no
  explicit body. UserRepository coroutine `l.invokeSuspend` (599–628) invokes it.
  Its server-side revocation scope, invalidation timing, cross-client impact and
  effect on the OAuth session are not established statically. No logout was sent.
- Existing photonctl HTTP errors remain sanitized; failed API requests do not
  imply offline status or cause automatic authentication retries. Reimport a
  valid session when it expires. No valid cache is replaced on a failed import.

## Previously analyzed web-client evidence

The file-manager app's decoded bootstrap (app.982cbff9.js, user store
`LOG_IN` / `GET_OAUTH_TOKEN`) redirects to
`https://uc.makeronline.com/login/oauth/authorize`, receives a code, then calls
`GET /v3/public/getoauthToken?code=…`. It sends JSON
`{"access_token":"<OAUTH_TOKEN>","device_type":"web"}` to
`POST /v3/public/loginWithAccessToken` and stores returned `data.token` as
localStorage `XX-Token`. This independently corroborates the distinct QCloud
exchange, **not a native email/password POST**. Android uses a different configured
identity host and explicit PKCE; the two initial authorization flows must not be
silently interchanged.

## What is needed before native implementation

Obtain authoritative documentation or public first-party client code establishing
a **supported direct credentials flow** for the relevant Anycubic account system:
endpoint, exact serialization, email/phone semantics, password treatment,
challenge/CSRF/device requirements, response/errors, and the supported transition
to the QCloud token. If the only supported credential entry remains hosted OAuth,
that is an unresolved scope constraint, not permission to implement browser or
OAuth alternatives. A resource-owner password grant must not be invented.

Once complete evidence exists: implement a separately tested native handler with
getpass, bounded timeouts/no automatic retry, sanitized failures, strict nonempty
QCloud token validation, and the existing atomic 0600 persistence only after
success. No passwords, cookies, password-derived material, or speculative refresh
credentials should be cached. Only then would `python3 photonctl.py login` become
a native login candidate, explicitly **UNVERIFIED** pending a separately authorized
live test. Current import-only behavior remains usable by file/printer commands.

## Tests and current user command

`python3 -B -m unittest discover -s tests -v`: **34 tests pass**. Existing manual
import success/0600/cache reuse, CLI compatibility and all working operations
remain covered. New mocked/offline regression cases verify missing native support
and invalid manual imports preserve a valid existing session, expose no synthetic
secrets, and send no HTTP requests. No mocked native success/credential-payload
cases are claimed: the required protocol is not known.

There is **no command to test native password login yet**. The existing supported
fallback remains the documented secure environment import followed by:

```bash
python3 photonctl.py login
```

This does not prompt for email/password and does not validate the imported token
with the server. No real login test was attempted.
