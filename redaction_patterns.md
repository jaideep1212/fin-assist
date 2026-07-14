# Shipper-level redaction — the denylist backstop

This is the **last line of defence**, applied at the log shipper on *every*
host (Pi, laptop, Azure) regardless of which language or tool produced the line.
It exists because source-side redaction (`obs_logging.py`) covers the code you
control, but not: third-party library output, stack traces, shell/`az`/terraform
stdout, or any component not yet converted. One ruleset, applied everywhere,
fails the whole pipeline safe.

**Design rules**
- This layer only ever *removes* data, so it is safe to deploy without sign-off.
- It is a **denylist** (masks known-bad shapes), so it is open-by-default and
  must never be the *only* protection — it is the backstop under the
  allowlist in `obs_logging.py`, not a replacement for it.
- Keep these patterns in sync with `_PATTERNS` in `obs_logging.py`. If you add a
  secret shape to one, add it to the other.
- Test every pattern against real sample lines before trusting it (a too-greedy
  rule can mask `row_count=123456789` — note the 9+ digit rule is deliberately
  tuned to avoid dates and small counts).

---

## The canonical pattern list (regex + replacement)

| # | What it catches | Regex | Replace with |
|---|-----------------|-------|--------------|
| 1 | DB URL w/ inline creds (`postgres://u:p@h/db`, `mysql://…`) | `\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s]+` | `***REDACTED_URL***` |
| 2 | Azure conn-string key parts | `(?i)\b(AccountKey\|SharedAccessKey\|SharedAccessSignature\|sig)=[^\s;&"']+` | `$1=***REDACTED***` |
| 3 | Storage conn-string prefix | `(?i)\bDefaultEndpointsProtocol=[^\s"']+` | `***REDACTED_CONNSTR***` |
| 4 | Relay/Service Bus conn parts | `(?i)\b(Endpoint\|EntityPath\|SharedAccessKeyName)=[^\s;"']+` | `$1=***REDACTED***` |
| 5 | SAS query params | `(?i)[?&]s(?:ig\|v\|p\|e\|t\|r)=[^\s&"']+` | `***REDACTED_SAS***` |
| 6 | Fernet key (44-char urlsafe b64) | `\b[A-Za-z0-9\-_]{43}=\b` | `***REDACTED_KEY***` |
| 7 | Generic long base64 secret | `\b[A-Za-z0-9+/]{40,}={0,2}\b` | `***REDACTED_B64***` |
| 8 | JWT | `\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+` | `***REDACTED_JWT***` |
| 9 | Named secret `KEY=value` | `(?i)\b(PGPASSWORD\|TF_VAR_ADMIN_PASSWORD\|ENCRYPTION_KEY\|BLOB_CONN_STR\|ARM_ACCESS_KEY\|ARM_CLIENT_SECRET\|password\|passwd\|secret\|token\|api[_-]?key)\s*[=:]\s*[^\s,;"']+` | `$1=***REDACTED***` |
| 10 | Email (PII) | `\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b` | `***REDACTED_EMAIL***` |
| 11 | Long digit run — account/card/phone (PII) | `\b\d{9,}\b` | `***REDACTED_NUM***` |

Project-specific note: patterns 1, 2, 4, 5, 6, 9 map directly to secrets that
exist in *this* repo — the `staging_database_url` (1), `BLOB_CONN_STR` (2/3),
the Relay LISTEN SAS in `pi_config.yml` (4/5), the tenant `ENCRYPTION_KEY` (6),
and the env secrets (9). Patterns 10–11 target the decrypted PII in the export
tools if their output is ever captured.

---

## Fluent Bit (recommended for a Pi + laptop + Azure mix — one binary everywhere)

Use the `modify`/Lua approach; Fluent Bit's built-in filters can't regex-replace
in place, so a small Lua filter is the clean way:

```lua
-- /etc/fluent-bit/scrub.lua
local patterns = {
  {"[a-zA-Z][a-zA-Z0-9+.%-]*://[^%s:/@]+:[^%s:/@]+@[^%s]+", "***REDACTED_URL***"},
  {"[Ss]hared[Aa]ccess[Ss]ignature=[^%s;&\"']+",           "***REDACTED***"},
  {"eyJ[A-Za-z0-9_%-]+%.[A-Za-z0-9_%-]+%.[A-Za-z0-9_%-]+",  "***REDACTED_JWT***"},
  {"[A-Za-z0-9._%%+%-]+@[A-Za-z0-9.%-]+%.[A-Za-z][A-Za-z]+", "***REDACTED_EMAIL***"},
  {"%d%d%d%d%d%d%d%d%d+",                                    "***REDACTED_NUM***"},
  -- add the rest; Lua patterns differ from PCRE, test each one.
}
function scrub(tag, ts, record)
  for k, v in pairs(record) do
    if type(v) == "string" then
      for _, p in ipairs(patterns) do v = v:gsub(p[1], p[2]) end
      record[k] = v
    end
  end
  return 2, ts, record
end
```
```ini
[FILTER]
    Name    lua
    Match   *
    script  /etc/fluent-bit/scrub.lua
    call    scrub
```
> Lua patterns are NOT PCRE (no alternation, different classes). For full-regex
> fidelity prefer Vector (below); Fluent Bit Lua is fine for the high-value
> shapes but test carefully.

## Vector (best regex fidelity — if you want the table above verbatim)

Vector's VRL uses real regex, so patterns transfer 1:1:

```toml
[transforms.scrub]
type = "remap"
inputs = ["in"]
source = '''
  .message = replace(string!(.message), r'[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@\S+', "***REDACTED_URL***")
  .message = replace(.message, r'(?i)(AccountKey|SharedAccessKey|SharedAccessSignature|sig)=[^\s;&"'']+', "$1=***REDACTED***")
  .message = replace(.message, r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+', "***REDACTED_JWT***")
  .message = replace(.message, r'\b[A-Za-z0-9\-_]{43}=\b', "***REDACTED_KEY***")
  .message = replace(.message, r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', "***REDACTED_EMAIL***")
  .message = replace(.message, r'\b\d{9,}\b', "***REDACTED_NUM***")
  # ...remaining patterns from the table, in order...
'''
```

## Promtail / Grafana Alloy (if you go Loki-native)

Promtail's `replace` stage is single-regex per stage, so chain one stage per
pattern:

```yaml
pipeline_stages:
  - replace:
      expression: '([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@\S+)'
      replace: '***REDACTED_URL***'
  - replace:
      expression: '(?i)(SharedAccessSignature|SharedAccessKey|AccountKey)=([^\s;&"'']+)'
      replace: '${1}=***REDACTED***'
  - replace:
      expression: '(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)'
      replace: '***REDACTED_JWT***'
  - replace:
      expression: '([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})'
      replace: '***REDACTED_EMAIL***'
  - replace:
      expression: '(\b\d{9,}\b)'
      replace: '***REDACTED_NUM***'
  # one stage per remaining pattern
```

---

## Deploy notes per host

- **Azure**: the orchestrator ships to Log Analytics *before* any shipper you
  add. So the redaction that matters for Azure is source-side (`obs_logging.py`)
  plus not echoing secrets on command lines (already done for the access key).
  If you later forward Log Analytics → Grafana via the Azure Monitor data
  source, the data stays in-tenant and this shipper layer applies to Pi/laptop.
- **Pi**: run the shipper as a service alongside azbridge. The Pi's own logs are
  low-risk (relay status), but `pi_config.yml` contains the LISTEN SAS — make
  sure the shipper never tails that file, only the app/service logs.
- **Laptop**: the decrypt/export tools run here. Per the plan they are EXCLUDED
  from shipping — do not point the shipper at their stdout or the `decrypted/`,
  `C:\Scripts\*` output dirs. The shipper here should tail only the watchdog/dev
  logs you actually want in Grafana.

## Verify before trusting

Pipe known-bad sample lines through the shipper and confirm each is masked:

```
postgres://svc:hunter2@db.internal:5432/staging?sslmode=require
DefaultEndpointsProtocol=https;AccountName=finassistdata;AccountKey=abc123==;
Endpoint=sb://financial-relay.servicebus.windows.net/;SharedAccessKeyName=pi-listen;SharedAccessKey=Zm9vYmFy==
ENCRYPTION_KEY=dGhpcy1pcy1hLTMyLWJ5dGUtZmVybmV0LWtleS0xMjM0NT0=
user_email=jane.doe@example.com account=1234567890123456
```
None of those substrings should survive to the destination. Keep this file as
the test fixture.
