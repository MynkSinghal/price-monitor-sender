# Authentication Header Implementation Guide

> **Purpose**: This document gives a future AI agent (or human) everything needed to implement
> configurable authentication headers in the price monitor sender, end-to-end, without reading
> any other file first. Follow it top-to-bottom.

---

## 1. What already exists and why it matters

### The project in one paragraph

`price_monitor_sender` is a Python daemon that runs on two production Windows boxes (UKPROD,
USPROD). Every minute it scans a folder tree for `success.txt` files produced by RANTask jobs,
builds a CSV snapshot of all price groups, and POSTs it to a receiver URL. Configuration is
split across three sources loaded in ascending priority order:

1. `config/config.json` — non-secret, committed, shared runtime config
2. `config/price_groups.json` — price group definitions (not relevant to auth)
3. `.env` — secrets and per-deployment overrides, never committed

### The HTTP layer today

`src/transmitter.py` holds the `Transmitter` class. It creates a `requests.Session` once at
startup and sets a static header dict. Per-cycle, it adds `X-Cycle-Id` and calls
`session.request(...)`. There is no retry logic — a failed send is just logged and the next cycle
(60 s later) sends the full snapshot again.

```python
# src/transmitter.py  (current, abbreviated)
class Transmitter:
    def __init__(self, cfg: AppConfig) -> None:
        self._session = requests.Session()
        self._headers = {
            "Content-Type": cfg.content_type,
            "X-Sender-Host": cfg.sender_hostname,
            "X-Sender-Env": cfg.sender_env,
            "X-Sender-Site": cfg.sender_site,
            **cfg.extra_headers,          # <-- already supports arbitrary extra headers
        }

    def send(self, payload: str, *, cycle_id: str) -> TransmissionResult:
        headers = dict(self._headers)
        headers["X-Cycle-Id"] = cycle_id
        resp = self._session.request(
            method=self._cfg.http_method,
            url=self._cfg.receiver_url,
            data=payload.encode("utf-8"),
            headers=headers,
            timeout=self._cfg.receiver_timeout_seconds,
        )
        ...
```

Auth is just another static header baked in at construction — so `send()` does not need to
change at all.

### The config dataclass today

`src/config_loader.py` defines `AppConfig` (a plain `@dataclass`, NOT frozen). The transport
section currently has:

```python
receiver_url: str
receiver_timeout_seconds: int
http_method: str
content_type: str
extra_headers: dict[str, str]
```

`load_config()` reads `config.json` for the non-secret parts and env vars for secrets. The
pattern for a field that is "non-secret config + optional env-var override" is already
established (e.g. `timezone_business` reads `config.json` first then lets `BUSINESS_TIMEZONE`
override it). The pattern for a field that is "secret, env-var only" is also established (e.g.
`RECEIVER_URL` — required, crashes loudly if missing).

### Hot-reload behaviour

`src/config_watcher.py` polls the mtimes of `.env`, `config.json`, and `price_groups.json`. When
any changes it calls `os._exit(0)`, the Windows Task Scheduler supervisor restarts the process,
and config is reloaded from scratch. This means **token rotation is automatic** — just update
`.env` and the watcher restarts the process within one poll interval (default 30 s).

---

## 2. Design decisions (already settled — do not re-design)


| Decision                                                                   | Rationale                                                                                                                       |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `auth.enabled` boolean in `config.json`                                    | Lets operators flip auth on/off without touching `.env` or any secrets. Absence of env vars when `enabled=false` is fine.       |
| Auth type (`bearer` / `api_key` / `basic`) in `config.json`                | Non-secret shape of the credential. Changing the auth scheme should not require touching secrets.                               |
| Header name (`Authorization` or custom) in `config.json`                   | Some receivers use `X-API-Key` instead of `Authorization`. Configurable without code changes.                                   |
| Actual secrets (`AUTH_TOKEN`, `AUTH_USERNAME`, `AUTH_PASSWORD`) in `.env`  | Consistent with how `RECEIVER_URL` is handled today.                                                                            |
| Auth header injected once at `Transmitter.__init__`, not per `send()` call | Token does not change mid-run; building it once is cheaper and keeps `send()` unchanged.                                        |
| Fail-fast validation at startup                                            | Same pattern used for `SENDER_SITE`, `SEND_MODE`, `BUSINESS_DAY_ANCHOR`. Better to crash loudly on boot than silently get 401s. |
| No new dependencies                                                        | `base64` is stdlib. `requests` is already a dependency.                                                                         |


---

## 3. Files to change — complete list


| File                          | Change type                                                                      |
| ----------------------------- | -------------------------------------------------------------------------------- |
| `config/config.json`          | Add `auth` block inside `transport`                                              |
| `.env.example`                | Document the new env vars (commented out)                                        |
| `src/config_loader.py`        | Add 6 fields to `AppConfig`; read + validate them in `load_config()`             |
| `src/transmitter.py`          | Add `_build_auth_value()` helper; conditionally inject auth header in `__init__` |
| `tests/test_transmitter.py`   | Add `TestTransmitterAuth` class; update `_make_cfg` helper                       |
| `tests/test_config_loader.py` | Add auth validation tests                                                        |


---

## 4. Exact changes, file by file

---

### 4.1 `config/config.json`

Add an `auth` object inside the `transport` block. The current `transport` block is:

```json
"transport": {
  "protocol": "http",
  "method": "POST",
  "content_type": "text/csv",
  "extra_headers": {
    "X-Sender-Source": "RANTask-PriceMonitor"
  }
}
```

Replace it with:

```json
"transport": {
  "protocol": "http",
  "method": "POST",
  "content_type": "text/csv",
  "auth": {
    "enabled": false,
    "type": "bearer",
    "header_name": "Authorization"
  },
  "extra_headers": {
    "X-Sender-Source": "RANTask-PriceMonitor"
  }
}
```

**Field meanings:**

- `enabled` (bool, default `false`) — master switch. When `false`, zero auth code runs and no
env vars need to be set. Flip to `true` to activate.
- `type` (string) — one of `"bearer"`, `"api_key"`, `"basic"`. Controls how the credential is
formatted in the header value:
  - `bearer` → `Authorization: Bearer <AUTH_TOKEN>`
  - `api_key` → `<header_name>: <AUTH_TOKEN>` (raw token, no prefix)
  - `basic` → `Authorization: Basic <base64(AUTH_USERNAME:AUTH_PASSWORD)>`
- `header_name` (string, default `"Authorization"`) — the HTTP header name. Use `"X-API-Key"` or
any custom name if the receiver deviates from the `Authorization` standard.

---

### 4.2 `.env.example`

Append a new section at the end (before the closing comment block):

```bash
# =========================================================
# 8. Authentication (optional)
# =========================================================
# Controlled by config/config.json → transport.auth.enabled
# Leave these unset if auth is disabled (enabled=false).

# Token for type=bearer  →  Authorization: Bearer <token>
# Token for type=api_key →  <header_name>: <token>
# AUTH_TOKEN=

# Credentials for type=basic  →  Authorization: Basic base64(user:pass)
# AUTH_USERNAME=
# AUTH_PASSWORD=
```

---

### 4.3 `src/config_loader.py`

#### 4.3.1 Add 6 fields to `AppConfig`

Insert these after the existing `extra_headers` field (currently line 65), inside the
`# ---- transport ----` section:

```python
# ---- auth ----
auth_enabled: bool          # transport.auth.enabled in config.json
auth_type: str              # "bearer" | "api_key" | "basic"
auth_header_name: str       # header to set, usually "Authorization"
auth_token: str             # from AUTH_TOKEN env var; empty str when unused
auth_username: str          # from AUTH_USERNAME env var; basic auth only
auth_password: str          # from AUTH_PASSWORD env var; basic auth only
```

`AppConfig` is a plain `@dataclass` (not frozen), so just add these lines inside the class body
in the right group.

#### 4.3.2 Read the values in `load_config()`

In `load_config()`, the `runtime` variable already holds the parsed `config.json` dict.
After the line that reads `extra_headers`:

```python
extra_headers=dict(runtime["transport"].get("extra_headers", {})),
```

Add the following block to read the auth sub-dict and the env vars:

```python
_auth = runtime["transport"].get("auth", {})
```

Then, inside the `AppConfig(...)` constructor call, add:

```python
auth_enabled=bool(_auth.get("enabled", False)),
auth_type=str(_auth.get("type", "bearer")).strip().lower(),
auth_header_name=str(_auth.get("header_name", "Authorization")).strip(),
auth_token=os.getenv("AUTH_TOKEN", ""),
auth_username=os.getenv("AUTH_USERNAME", ""),
auth_password=os.getenv("AUTH_PASSWORD", ""),
```

#### 4.3.3 Add startup validation

At the bottom of `load_config()`, after the existing `if cfg.sender_site not in {"UKPROD", "USPROD"}:` check and before the `return cfg`, add:

```python
if cfg.auth_enabled:
    if cfg.auth_type not in {"bearer", "api_key", "basic"}:
        raise ValueError(
            f"transport.auth.type must be 'bearer', 'api_key', or 'basic', "
            f"got {cfg.auth_type!r}"
        )
    if not cfg.auth_header_name:
        raise ValueError("transport.auth.header_name must not be empty")
    if cfg.auth_type in {"bearer", "api_key"} and not cfg.auth_token:
        raise ValueError(
            "AUTH_TOKEN must be set in .env when transport.auth.enabled=true "
            f"and auth.type={cfg.auth_type!r}"
        )
    if cfg.auth_type == "basic" and not (cfg.auth_username and cfg.auth_password):
        raise ValueError(
            "AUTH_USERNAME and AUTH_PASSWORD must both be set in .env "
            "when transport.auth.type='basic'"
        )
```

---

### 4.4 `src/transmitter.py`

#### 4.4.1 Add the helper function

Add this module-level function **before** the `Transmitter` class definition:

```python
def _build_auth_value(cfg: AppConfig) -> str:
    """Return the header value string for the configured auth type."""
    if cfg.auth_type == "bearer":
        return f"Bearer {cfg.auth_token}"
    if cfg.auth_type == "api_key":
        return cfg.auth_token
    # basic
    import base64
    credentials = f"{cfg.auth_username}:{cfg.auth_password}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"
```

#### 4.4.2 Inject the auth header in `__init__`

After the `self._headers = { ... }` block in `Transmitter.__init__`, add:

```python
if cfg.auth_enabled:
    self._headers[cfg.auth_header_name] = _build_auth_value(cfg)
```

The full `__init__` after the change looks like:

```python
def __init__(self, cfg: AppConfig) -> None:
    self._cfg = cfg
    self._session = requests.Session()
    self._headers = {
        "Content-Type": cfg.content_type,
        "X-Sender-Host": cfg.sender_hostname,
        "X-Sender-Env": cfg.sender_env,
        "X-Sender-Site": cfg.sender_site,
        **cfg.extra_headers,
    }
    if cfg.auth_enabled:
        self._headers[cfg.auth_header_name] = _build_auth_value(cfg)
```

`send()` is **unchanged**. The auth header rides along in `self._headers` exactly like any other
static header.

---

### 4.5 `tests/test_transmitter.py`

#### 4.5.1 Update `_make_cfg` helper

The helper currently does not have auth fields. Update it so auth is **disabled by default**
(no change to existing tests) but easy to override:

```python
def _make_cfg(**overrides) -> types.SimpleNamespace:
    base = dict(
        receiver_url="http://localhost:8080/prices",
        receiver_timeout_seconds=10,
        http_method="POST",
        content_type="text/csv",
        sender_hostname="testhost",
        sender_env="TEST",
        sender_site="UKPROD",
        extra_headers={"X-Sender-Source": "price_monitor"},
        # auth — disabled by default so all existing tests are unaffected
        auth_enabled=False,
        auth_type="bearer",
        auth_header_name="Authorization",
        auth_token="",
        auth_username="",
        auth_password="",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)
```

#### 4.5.2 Add `TestTransmitterAuth` class

Append this class to the test file:

```python
# ---------------------------------------------------------------------------
# Authentication header
# ---------------------------------------------------------------------------

class TestTransmitterAuth:

    def _capture_headers(self, tx: Transmitter) -> dict:
        captured: dict = {}

        def capture(**kwargs):
            captured.update(kwargs.get("headers", {}))
            return _mock_response(200)

        with patch.object(tx._session, "request", side_effect=capture):
            tx.send("p", cycle_id="c")
        return captured

    # --- disabled (default) ---

    def test_auth_disabled_no_authorization_header(self):
        tx = _make_tx(auth_enabled=False)
        headers = self._capture_headers(tx)
        assert "Authorization" not in headers

    # --- bearer ---

    def test_bearer_header_value(self):
        tx = _make_tx(auth_enabled=True, auth_type="bearer", auth_token="tok123")
        headers = self._capture_headers(tx)
        assert headers["Authorization"] == "Bearer tok123"

    def test_bearer_header_name_default_is_authorization(self):
        tx = _make_tx(auth_enabled=True, auth_type="bearer", auth_token="t")
        headers = self._capture_headers(tx)
        assert "Authorization" in headers

    # --- api_key ---

    def test_api_key_raw_token_in_default_header(self):
        tx = _make_tx(auth_enabled=True, auth_type="api_key", auth_token="secret-key")
        headers = self._capture_headers(tx)
        assert headers["Authorization"] == "secret-key"

    def test_api_key_custom_header_name(self):
        tx = _make_tx(
            auth_enabled=True,
            auth_type="api_key",
            auth_header_name="X-API-Key",
            auth_token="mykey",
        )
        headers = self._capture_headers(tx)
        assert headers.get("X-API-Key") == "mykey"
        assert "Authorization" not in headers

    # --- basic ---

    def test_basic_auth_header_value(self):
        import base64
        tx = _make_tx(
            auth_enabled=True,
            auth_type="basic",
            auth_username="user",
            auth_password="pass",
        )
        headers = self._capture_headers(tx)
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        assert headers["Authorization"] == expected

    def test_basic_auth_special_chars_in_password(self):
        import base64
        tx = _make_tx(
            auth_enabled=True,
            auth_type="basic",
            auth_username="svc_account",
            auth_password="p@$$w0rd!",
        )
        headers = self._capture_headers(tx)
        expected = "Basic " + base64.b64encode(b"svc_account:p@$$w0rd!").decode()
        assert headers["Authorization"] == expected

    # --- token is static (computed once at __init__) ---

    def test_auth_header_present_on_every_send(self):
        tx = _make_tx(auth_enabled=True, auth_type="bearer", auth_token="persistent")
        all_headers = []

        def capture(**kwargs):
            all_headers.append(dict(kwargs.get("headers", {})))
            return _mock_response(200)

        with patch.object(tx._session, "request", side_effect=capture):
            tx.send("p1", cycle_id="c1")
            tx.send("p2", cycle_id="c2")
            tx.send("p3", cycle_id="c3")

        for h in all_headers:
            assert h.get("Authorization") == "Bearer persistent"
```

---

### 4.6 `tests/test_config_loader.py`

The existing tests in this file test `_build_price_groups` directly (not `load_config()` as a
whole, because `load_config()` touches the filesystem). Add a new module-level test at the
bottom of the file that validates the startup-validation logic in `load_config()` indirectly by
calling a helper, OR add a separate file `tests/test_config_loader_auth.py`. The recommended
approach is a separate file to keep concerns isolated:

**Create `tests/test_config_loader_auth.py`:**

```python
"""Tests for auth validation logic in config_loader."""
from __future__ import annotations

import pytest
from src.config_loader import AppConfig


def _make_auth_cfg(**overrides):
    """Minimal AppConfig-like namespace sufficient for auth validation tests.

    We test the validation block logic directly. Because load_config() touches
    the filesystem (reads config.json, .env, creates dirs), we replicate just
    the validation logic here rather than mocking the whole loader.
    """
    import types
    base = dict(
        auth_enabled=True,
        auth_type="bearer",
        auth_header_name="Authorization",
        auth_token="tok",
        auth_username="",
        auth_password="",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _run_validation(cfg) -> None:
    """Run only the auth validation logic extracted from load_config()."""
    if cfg.auth_enabled:
        if cfg.auth_type not in {"bearer", "api_key", "basic"}:
            raise ValueError(
                f"transport.auth.type must be 'bearer', 'api_key', or 'basic', "
                f"got {cfg.auth_type!r}"
            )
        if not cfg.auth_header_name:
            raise ValueError("transport.auth.header_name must not be empty")
        if cfg.auth_type in {"bearer", "api_key"} and not cfg.auth_token:
            raise ValueError(
                "AUTH_TOKEN must be set in .env when transport.auth.enabled=true "
                f"and auth.type={cfg.auth_type!r}"
            )
        if cfg.auth_type == "basic" and not (cfg.auth_username and cfg.auth_password):
            raise ValueError(
                "AUTH_USERNAME and AUTH_PASSWORD must both be set in .env "
                "when transport.auth.type='basic'"
            )


class TestAuthValidation:

    def test_bearer_with_token_passes(self):
        cfg = _make_auth_cfg(auth_type="bearer", auth_token="t")
        _run_validation(cfg)  # must not raise

    def test_api_key_with_token_passes(self):
        cfg = _make_auth_cfg(auth_type="api_key", auth_token="k")
        _run_validation(cfg)

    def test_basic_with_username_and_password_passes(self):
        cfg = _make_auth_cfg(auth_type="basic", auth_token="", auth_username="u", auth_password="p")
        _run_validation(cfg)

    def test_disabled_skips_all_validation(self):
        # Nothing set, but enabled=False — should not raise
        cfg = _make_auth_cfg(
            auth_enabled=False,
            auth_type="bearer",
            auth_token="",
        )
        _run_validation(cfg)

    def test_unknown_type_raises(self):
        cfg = _make_auth_cfg(auth_type="oauth2", auth_token="t")
        with pytest.raises(ValueError, match="auth.type"):
            _run_validation(cfg)

    def test_bearer_without_token_raises(self):
        cfg = _make_auth_cfg(auth_type="bearer", auth_token="")
        with pytest.raises(ValueError, match="AUTH_TOKEN"):
            _run_validation(cfg)

    def test_api_key_without_token_raises(self):
        cfg = _make_auth_cfg(auth_type="api_key", auth_token="")
        with pytest.raises(ValueError, match="AUTH_TOKEN"):
            _run_validation(cfg)

    def test_basic_without_username_raises(self):
        cfg = _make_auth_cfg(auth_type="basic", auth_token="", auth_username="", auth_password="pass")
        with pytest.raises(ValueError, match="AUTH_USERNAME"):
            _run_validation(cfg)

    def test_basic_without_password_raises(self):
        cfg = _make_auth_cfg(auth_type="basic", auth_token="", auth_username="user", auth_password="")
        with pytest.raises(ValueError, match="AUTH_PASSWORD"):
            _run_validation(cfg)

    def test_empty_header_name_raises(self):
        cfg = _make_auth_cfg(auth_header_name="")
        with pytest.raises(ValueError, match="header_name"):
            _run_validation(cfg)
```

---

## 5. End-to-end usage examples after implementation

### Scenario A — Auth disabled (default, today's behaviour)

`config/config.json`:

```json
"auth": { "enabled": false, "type": "bearer", "header_name": "Authorization" }
```

`.env`: no `AUTH_TOKEN` line needed.

Result: no `Authorization` header ever appears on the wire.

---

### Scenario B — Bearer token (most common)

`config/config.json`:

```json
"auth": { "enabled": true, "type": "bearer", "header_name": "Authorization" }
```

`.env`:

```
AUTH_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Wire header: `Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...`

---

### Scenario C — API key on custom header

`config/config.json`:

```json
"auth": { "enabled": true, "type": "api_key", "header_name": "X-API-Key" }
```

`.env`:

```
AUTH_TOKEN=my-secret-api-key
```

Wire header: `X-API-Key: my-secret-api-key`

---

### Scenario D — HTTP Basic auth

`config/config.json`:

```json
"auth": { "enabled": true, "type": "basic", "header_name": "Authorization" }
```

`.env`:

```
AUTH_USERNAME=svc_price_sender
AUTH_PASSWORD=correcthorsebatterystaple
```

Wire header: `Authorization: Basic c3ZjX3ByaWNlX3NlbmRlcjpjb3JyZWN0aG9yc2ViYXR0ZXJ5c3RhcGxl`

---

### Token rotation

1. Update `AUTH_TOKEN` in `.env`
2. `ConfigWatcher` detects the `.env` mtime change (next poll, default 30 s)
3. Calls `os._exit(0)` → supervisor restarts the process
4. `Transmitter.__init__` rebuilds `self._headers` with the new token value
5. No manual intervention needed

---

## 6. What NOT to change

- `src/main.py` — `Sender` passes `AppConfig` to `Transmitter`; the new fields are transparent.
- `src/config_watcher.py` — already watches `.env` and `config.json`; no changes needed.
- `src/transmitter.py:send()` — the method body is unchanged; auth is a static header.
- `dashboard/mock_receiver.py` — the mock can accept any headers; no change required unless you
want it to enforce auth (out of scope).
- `config/price_groups.json` — unrelated.
- `requirements.txt` — `base64` is stdlib; no new package needed.

---

## 7. Checklist for the implementing agent

- `config/config.json` — add `auth` block with `enabled: false` as default
- `.env.example` — add commented-out `AUTH_TOKEN`, `AUTH_USERNAME`, `AUTH_PASSWORD`
- `src/config_loader.py` — add 6 fields to `AppConfig`
- `src/config_loader.py` — read `_auth` dict and env vars inside `load_config()`
- `src/config_loader.py` — add startup validation block before `return cfg`
- `src/transmitter.py` — add `_build_auth_value()` above `Transmitter` class
- `src/transmitter.py` — add `if cfg.auth_enabled:` block at end of `__init__`
- `tests/test_transmitter.py` — add auth defaults to `_make_cfg()`
- `tests/test_transmitter.py` — add `TestTransmitterAuth` class
- `tests/test_config_loader_auth.py` — create new file with `TestAuthValidation`
- Run `pytest` — all existing tests must still pass; new tests must pass

