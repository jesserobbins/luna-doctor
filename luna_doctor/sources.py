#!/usr/bin/env python3
"""One data-collection layer for luna-doctor.

Every fact a command can quote comes from one of these states of the world:

  measured    what the machine actually did - the local log stores
  declared    what the machine is configured to do - harness config files
  catalog     what the gateway actually offers - GET /v1/models, cached on disk
  account     what the account actually has - GET /v1/capacity on
              api.lunaroute.com (plan, priority concurrency, occupancy,
              admission), cached on disk

plus `configured` for documented constants and `unknown` for everything else.
Nothing here guesses: a value that cannot be established is `unknown` with a
reason, and every value carries its provenance (which file and key, or which
endpoint, or "absent → default N" with the citation for the default).

The network is never touched unless a caller passes allow_network=True
(`--online`). The default serves the cached catalog when one exists and
reports unknown otherwise; a network failure degrades to unknown, never to
an error. `catalog` and `limits` are plain GETs - no inference request is
ever made from here.

cli.py imports this module at import time. Where this module needs cli's
Request and its log-reading helpers it imports cli inside the function that
uses them, so the import cycle exists only at call time, never at import
time.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# ---- provenance states ------------------------------------------------------
# One vocabulary for where every fact in a snapshot came from. The six words
# are the whole contract: a consumer renders anything unknown as unknown and
# never substitutes a default of its own.

PROV_MEASURED = "measured"        # reconstructed from the local log stores
PROV_DECLARED = "declared"       # read from a harness or repo config file
PROV_CATALOG = "catalog"         # GET /v1/models on the LunaRoute gateway
PROV_ACCOUNT = "account"         # GET /v1/capacity on the LunaRoute account
                                 # host, served from the disk cache
PROV_CONFIGURED = "configured"   # a documented constant
PROV_UNKNOWN = "unknown"         # not established; carried with a reason

PROVENANCE_LEGEND = {
    "measured": "reconstructed from the local log stores",
    "declared": "read from a harness or repo config file",
    "catalog": "GET /v1/models on the gateway, served from disk cache",
    "account": "fetched from api.lunaroute.com, served from disk cache",
    "configured": "a documented constant, not a guess",
    "unknown": "not established; reported with a reason, never defaulted",
}

# ---- where the facts live ---------------------------------------------------
# The harness store locations are defined here once; cli.py imports them.

PI_STORE = "~/.pi/agent/sessions"
CLAUDE_STORE = "~/.claude/projects"
CODEX_STORE = "~/.codex/sessions"
GATEWAY_HOST = "gw.lunaroute.com"
ACCOUNT_HOST = "api.lunaroute.com"   # the account surface, not the inference gateway

PI_SETTINGS = "~/.pi/agent/settings.json"
PI_SUBAGENT_CFG = "~/.pi/agent/extensions/subagent/config.json"
# The pi model-definition stores, most specific first: models-store.json holds
# the live provider definitions (including lunaroute), models.json any local
# ones. Both are pi's *declared* sizing state.
PI_MODEL_STORES = ("~/.pi/agent/models-store.json", "~/.pi/agent/models.json")
PI_AUTH = "~/.pi/agent/auth.json"
CLAUDE_SETTINGS = "~/.claude/settings.json"
CODEX_CONFIG = "~/.codex/config.toml"

SWAMP_ENV_FILE = "~/.swamp.yaml"
SWAMP_ENV_GLOBAL_SOURCES = "~/.swamp-sources.yaml"

MODELS_URL = "https://" + GATEWAY_HOST + "/v1/models"
LIMITS_URL = "https://" + GATEWAY_HOST + "/v1/limits"
# The account's effective capacity: plan, priority/background concurrency,
# live priority-pool occupancy, 7d/30d occupancy rollups and per-complete-
# UTC-day admission outcomes. /v1/cli/capacity is the same handler (a legacy
# alias); this is the canonical path. Verified live 2026-10-23: the account
# host answers with the lr_ routing key under the documented
# LUNAROUTE-API-KEY header (Bearer also works; the doc form is canonical),
# rate limit 60/min, observed latency 4-17s - so it is never fetched
# per-render, it is cached like the catalog.
CAPACITY_URL = "https://" + ACCOUNT_HOST + "/v1/capacity"

# The catalog cache sits beside the store (~/.luna-doctor/store) so a report
# can name real model limits without ever blocking on the network. The TTL is
# 24h: model rosters change slowly, a daily refresh while online is plenty,
# and an expired cache still serves (marked stale) when offline.
CATALOG_CACHE = "~/.luna-doctor/catalog.json"
CATALOG_TTL_S = 24 * 3600
HTTP_TIMEOUT_S = 10.0

# The capacity cache sits beside the catalog cache. The 5-minute TTL matches
# the server's own history cadence (history is rolled up ~5-minutely server
# side; `now` and `admission` are computed per call, and `now.sampled_at`
# surfaces how fresh the occupancy sample really is). The endpoint answers in
# 4-17s in practice, so its timeout is deliberately longer than the shared
# HTTP_TIMEOUT_S - and a timeout still degrades to None (a stale cache serves,
# marked stale; no cache means unknown), never a blocked report.
CAPACITY_CACHE = "~/.luna-doctor/capacity.json"
CAPACITY_TTL_S = 5 * 60
CAPACITY_TIMEOUT_S = 30.0

# Swamp knob defaults are documented constants from the extension source.
SWAMP_CITATION_DEFAULT = "extensions/models/obsidian_ideas.ts"
SWAMP_EXTRACT_CONCURRENCY_DEFAULT = 12   # extensions/models/obsidian_ideas.ts phase extractMany
SWAMP_LLM_CONCURRENCY_DEFAULT = 8       # extensions/models/obsidian_ideas.ts runVerifyPass
SWAMP_LLM_MODEL_DEFAULT = "deepseek-4.1-flash-background"
SWAMP_LLM_PROVIDER_DEFAULT = "lunaroute"
SWAMP_KNOB_KEYS = (
    ("extractConcurrency", "SWAMP_EXTRACT_CONCURRENCY", SWAMP_EXTRACT_CONCURRENCY_DEFAULT),
    ("llmConcurrency", "SWAMP_LLM_CONCURRENCY", SWAMP_LLM_CONCURRENCY_DEFAULT),
    ("model", "SWAMP_LLM_MODEL", SWAMP_LLM_MODEL_DEFAULT),
    ("provider", "SWAMP_LLM_PROVIDER", SWAMP_LLM_PROVIDER_DEFAULT),
)


def catalog_cache_path() -> str:
    """Where the cached catalog lives. LUNA_DOCTOR_CATALOG overrides, so tests
    and sandboxes never touch a real cache."""
    override = os.environ.get("LUNA_DOCTOR_CATALOG", "").strip()
    if override:
        return override
    return os.path.expanduser(CATALOG_CACHE)


def capacity_cache_path() -> str:
    """Where the cached capacity payload lives. LUNA_DOCTOR_CAPACITY
    overrides, same contract as the catalog cache."""
    override = os.environ.get("LUNA_DOCTOR_CAPACITY", "").strip()
    if override:
        return override
    return os.path.expanduser(CAPACITY_CACHE)


# ---- Fact -------------------------------------------------------------------
# The atom of the snapshot: one value, its provenance state, and a source
# string precise enough to audit (file + key path, endpoint, or the citation
# behind an "absent → default N").


@dataclass
class Fact:
    value: object = None
    provenance: str = PROV_UNKNOWN
    source: str = ""
    reason: str = ""          # why it is unknown, when it is

    def to_dict(self) -> dict:
        d = {"value": self.value, "provenance": self.provenance, "source": self.source}
        if self.reason:
            d["reason"] = self.reason
        return d


def as_json(node) -> object:
    """Recursively turn Fact-bearing structures into plain JSON data."""
    if isinstance(node, Fact):
        return node.to_dict()
    if isinstance(node, dict):
        return {k: as_json(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [as_json(v) for v in node]
    return node


def declared_value(fact: Fact | None):
    """The value a config file actually declares, or None.

    A documented default (provenance `configured`) reads as unset here: callers
    that ask want to know what the file says, and "absent → default N" is a
    fact about the docs, not about the machine's file. The fix command uses
    this so its rendered "from" keeps distinguishing a declared value from
    "unset (default N)" exactly as it always has.
    """
    if fact is not None and fact.provenance == PROV_DECLARED:
        return fact.value
    return None


# ---- catalog (the live endpoint) --------------------------------------------


@dataclass
class ModelEntry:
    """One model as the gateway describes it. Only fields the endpoint really
    sends are exposed; nothing is invented. `raw` keeps the full record so a
    field not yet named here is not lost."""

    id: str
    owned_by: object = None
    context_window: object = None
    max_input_tokens: object = None
    max_output_tokens: object = None
    capabilities: dict = field(default_factory=dict)
    client_compat: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "owned_by": self.owned_by,
            "context_window": self.context_window,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "capabilities": self.capabilities,
            "client_compat": self.client_compat,
        }


@dataclass
class Catalog:
    models: dict = field(default_factory=dict)
    fetched_at: float | None = None
    served_from: str = "cache"       # "network" | "cache"
    stale: bool = False
    age_s: float = 0.0
    ttl_s: float = CATALOG_TTL_S
    note: str = ""

    def ids(self) -> list:
        return sorted(self.models)

    def describe(self) -> str:
        return f"GET {MODELS_URL} ({self.served_from}; fetched {_age_str(self.age_s)} ago)"

    def to_dict(self) -> dict:
        return {
            "provenance": PROV_CATALOG,
            "served_from": self.served_from,
            "fetched_at": _iso(self.fetched_at) if self.fetched_at else None,
            "age_s": int(self.age_s),
            "ttl_s": int(self.ttl_s),
            "stale": self.stale,
            "note": self.note,
            "models": {mid: m.to_dict() for mid, m in sorted(self.models.items())},
        }


@dataclass
class Limits:
    # SEAM (sizing rules): GET /v1/limits is not deployed yet (404 today,
    # verified with and without a key). When it deploys it is expected to
    # answer the account's effective priority/background/embedding concurrency,
    # and the --plan-style thresholds should be sourced from here rather than
    # re-measured. Until then this is None and nothing fabricates a limit.
    raw: dict = field(default_factory=dict)
    fetched_at: float | None = None

    def to_dict(self) -> dict:
        return {"provenance": PROV_CATALOG,
                "fetched_at": _iso(self.fetched_at) if self.fetched_at else None,
                "raw": self.raw}


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _age_str(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{round(s / 60)}m"
    if s < 36 * 3600:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def catalog_key(auth_path: str | None = None) -> str | None:
    """The routing key for catalog GETs, or None.

    Read the way the tool's other LunaRoute access does: LUNAROUTE_API_KEY in
    the environment, then ~/.pi/agent/auth.json under `lunaroute` (the `key`
    entry, else the `access` entry). No key is a degraded state, never an
    error: the caller reports the catalog as unknown.
    """
    env = os.environ.get("LUNAROUTE_API_KEY", "").strip()
    if env:
        return env
    path = os.path.expanduser(auth_path or PI_AUTH)
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    lr = doc.get("lunaroute")
    if not isinstance(lr, dict):
        return None
    for name in ("key", "access"):
        v = lr.get(name)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# The one network seam in the package. Every catalog/limits test patches this
# attribute; no other code path can open a socket.
_urlopen = urllib.request.urlopen


def _http_get_json(url: str, key: str, timeout: float = HTTP_TIMEOUT_S,
                    auth: tuple = None):
    """One authenticated GET. Returns (data, None) on success, (None, reason)
    on any failure - HTTP status, transport error or malformed JSON.

    `auth` overrides the default ("Authorization", "Bearer <key>") header
    pair: the capacity endpoint's documented form is LUNAROUTE-API-KEY, so
    that call site passes ("LUNAROUTE-API-KEY", key)."""
    from luna_doctor import cli       # VERSION lives in cli (cycle, call time)
    name, value = auth if auth is not None else ("Authorization", "Bearer " + key)
    req = urllib.request.Request(url, headers={
        name: value,
        "User-Agent": "luna-doctor/" + cli.VERSION,
    })
    try:
        with _urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:                      # timeout, DNS, TLS, reset
        return None, type(e).__name__
    if status != 200:
        return None, f"http {status}"
    try:
        return json.loads(body), None
    except Exception:
        return None, "malformed json"


def _parse_models_payload(data) -> tuple:
    """The endpoint answers OpenAI-style: {"object": "list", "data": [...]}.
    Verified per model: id, object, owned_by, context_window, max_input_tokens,
    max_output_tokens, capabilities{...}, client_compat{...}. Only what it
    really sends is kept."""
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}, "unexpected payload shape"
    models: dict = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        models[mid] = ModelEntry(
            id=mid,
            owned_by=raw.get("owned_by"),
            context_window=raw.get("context_window"),
            max_input_tokens=raw.get("max_input_tokens"),
            max_output_tokens=raw.get("max_output_tokens"),
            capabilities=raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {},
            client_compat=raw.get("client_compat") if isinstance(raw.get("client_compat"), dict) else {},
            raw=raw,
        )
    if not models:
        return {}, "payload held no models"
    return models, ""


def _read_catalog_cache(path: str, now: float, ttl: float) -> tuple:
    if not os.path.exists(path):
        # The cache being absent is not an error: it simply means there is
        # nothing to serve without the network.
        return None, "no cached catalog"
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except Exception as e:
        return None, f"cache present but unreadable ({type(e).__name__})"
    fetched = doc.get("fetched_at") if isinstance(doc, dict) else None
    response = doc.get("response") if isinstance(doc, dict) else None
    models, err = _parse_models_payload(response)
    if err or not isinstance(fetched, (int, float)):
        return None, "cache unreadable (no usable models or timestamp)"
    age = max(0.0, now - float(fetched))
    return Catalog(models=models, fetched_at=float(fetched), served_from="cache",
                   stale=age > ttl, age_s=age, ttl_s=ttl), ""


def _write_catalog_cache(path: str, response, now: float, url: str = MODELS_URL) -> None:
    """Best effort: a cache write failure never fails the fetch that produced
    the data. The endpoint's response is stored verbatim beside the fetch
    timestamp; atomic (tempfile + replace), so a reader never sees a partial
    file. The capacity cache uses the same writer with its own URL."""
    doc = {"fetched_at": now, "fetched_at_iso": _iso(now),
           "url": url, "response": response}
    tmp = None
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def fetch_catalog(allow_network: bool, *, cache_path: str | None = None,
                  now: float | None = None, ttl: float | None = None,
                  key: str | None = None, timeout: float | None = None):
    """The live model catalog, or None when it cannot be established.

    A cached catalog serves without network (a stale one serves too, marked
    stale). The network is attempted only when allow_network and the cache is
    stale or missing. No key, 401, timeout or malformed JSON all degrade to
    None - the catalog being absent is never an error, and consumers must
    render None as unknown, never as a default.
    """
    catalog, _ = _catalog_with_note(allow_network, cache_path=cache_path, now=now,
                                    ttl=ttl, key=key, timeout=timeout)
    return catalog


def _catalog_with_note(allow_network: bool, *, cache_path: str | None = None,
                       now: float | None = None, ttl: float | None = None,
                       key: str | None = None, timeout: float | None = None) -> tuple:
    now = time.time() if now is None else now
    ttl = CATALOG_TTL_S if ttl is None else ttl
    path = cache_path or catalog_cache_path()
    cached, cache_err = _read_catalog_cache(path, now, ttl)
    if cached is not None and not cached.stale:
        cached.note = f"served from cache; fetched {_age_str(cached.age_s)} ago"
        return cached, cached.note
    if not allow_network:
        if cached is not None:
            cached.note = (f"cache expired (fetched {_age_str(cached.age_s)} ago); "
                           f"offline, so served stale")
            return cached, cached.note
        return None, cache_err or "no cached catalog; network not allowed (offline)"
    k = catalog_key() if key is None else key
    if not k:
        if cached is not None:
            cached.note = "no routing key; served stale cache"
            return cached, cached.note
        return None, ("no routing key (LUNAROUTE_API_KEY, or "
                       "~/.pi/agent/auth.json lunaroute.key/.access)")
    data, err = _http_get_json(MODELS_URL, k, timeout or HTTP_TIMEOUT_S)
    if data is None:
        models, parse_err = {}, err
    else:
        models, parse_err = _parse_models_payload(data)
    if parse_err:
        if cached is not None:
            cached.note = f"fetch failed ({parse_err}); served stale cache"
            return cached, cached.note
        return None, f"fetch failed ({parse_err})"
    _write_catalog_cache(path, data, now)
    catalog = Catalog(models=models, fetched_at=now, served_from="network",
                      stale=False, age_s=0.0, ttl_s=ttl,
                      note=f"fetched from {MODELS_URL}")
    return catalog, catalog.note


def fetch_limits(allow_network: bool, *, key: str | None = None,
                 timeout: float | None = None, now: float | None = None):
    """The account's effective limits, or None. GET /v1/limits is not
    deployed yet (404, verified with and without a key): that is "not
    deployed", reported unknown, never fabricated."""
    limits, _ = _limits_with_note(allow_network, key=key, timeout=timeout, now=now)
    return limits


def _limits_with_note(allow_network: bool, *, key: str | None = None,
                      timeout: float | None = None, now: float | None = None) -> tuple:
    if not allow_network:
        return None, "limits are fetched live only (offline; no disk cache)"
    now = time.time() if now is None else now
    k = catalog_key() if key is None else key
    if not k:
        return None, ("no routing key (LUNAROUTE_API_KEY, or "
                      "~/.pi/agent/auth.json lunaroute.key/.access)")
    data, err = _http_get_json(LIMITS_URL, k, timeout or HTTP_TIMEOUT_S)
    if data is None:
        if err == "http 404":
            return None, "GET /v1/limits is not deployed (http 404)"
        return None, f"fetch failed ({err})"
    return Limits(raw=data if isinstance(data, dict) else {"payload": data},
                  fetched_at=now), "fetched"


# ---- capacity (the account host) --------------------------------------------
# GET /v1/capacity on api.lunaroute.com: the account's effective capacity
# (plan, priority and background concurrency, overflow routing), live
# priority-pool occupancy, 7d/30d occupancy rollups and per-complete-UTC-day
# admission outcomes. THE NULL CONTRACT IS LOAD-BEARING: a degraded block
# arrives HTTP 200 with `null` - `config: null`, `now` fields null with
# `ledger: "unknown"`, `history.tail: null`, `avg_queue_wait_ms: null` on
# days nobody waited. `priority_lanes: null` means unknown, NOT zero;
# `background_lanes: 0` is a real value (entitled to no overflow). Every
# field below is preserved verbatim and None-preserving - nothing here
# defaults a null to 0, and only a rolled-history failure is a hard 500.


@dataclass
class CapacityNow:
    """The live priority-pool occupancy. Covers the priority pool only:
    overflow traffic does not appear in `in_use` (it does appear in the
    admission counts). `raw` keeps the block exactly as the API sent it."""

    limit: object = None
    in_use: object = None
    available: object = None
    saturated: object = None
    ledger: object = None
    sampled_at: object = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


@dataclass
class CapacityConfig:
    """The account's effective capacity settings, fields verbatim.
    `background_lanes: 0` is real (no overflow entitled); any `None` is
    unknown and must never be rendered as a number."""

    plan: object = None
    priority_lanes: object = None
    background_lanes: object = None
    overflow_routing: object = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


@dataclass
class CapacityHistory:
    """The occupancy rollups. `days_active` (days with traffic) and
    `days_covered` (days with data) are different facts and are never
    combined into a rate; when `tail` is null, `days_covered` already
    excludes it, so a smaller `days_covered` never means "fewer days had
    traffic". `raw` keeps the block verbatim."""

    as_of: object = None
    last_7d: object = None
    last_30d: object = None
    tail: object = None
    rollup_computed_at: object = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


@dataclass
class CapacityAdmission:
    """Per-complete-UTC-day admission outcomes. One entry per complete day:
    the in-progress day is never included (as_of names the last day
    covered), so a bursty afternoon must not read as a collapse. The day
    lists stay exactly as the API sent them."""

    as_of: object = None
    last_7d: object = None
    last_30d: object = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


@dataclass
class Capacity:
    """One fetched capacity payload. Sub-blocks are the API's own fields,
    None-preserving; a block the API sent as `null` is None here, and an
    out-of-contract block is kept verbatim rather than guessed. `raw` keeps
    the whole `data` block so no unnamed field is lost."""

    now: object = None
    config: object = None
    history: object = None
    admission: object = None
    fetched_at: float | None = None
    served_from: str = "cache"       # "network" | "cache"
    stale: bool = False
    age_s: float = 0.0
    ttl_s: float = CAPACITY_TTL_S
    note: str = ""
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        return (f"GET {CAPACITY_URL} ({self.served_from}; "
                f"fetched {_age_str(self.age_s)} ago)")

    def to_dict(self) -> dict:
        def block(b):
            if b is None:
                return None
            if hasattr(b, "to_dict"):
                return b.to_dict()
            return b       # out-of-contract block: verbatim, never a guess

        # every data-block key this model does not name rides along verbatim
        out = {k: v for k, v in (self.raw or {}).items()
               if k not in ("now", "config", "history", "admission")}
        out.update({
            "provenance": PROV_ACCOUNT,
            "served_from": self.served_from,
            "fetched_at": _iso(self.fetched_at) if self.fetched_at else None,
            "age_s": int(self.age_s),
            "ttl_s": int(self.ttl_s),
            "stale": self.stale,
            "note": self.note,
            "now": block(self.now),
            "config": block(self.config),
            "history": block(self.history),
            "admission": block(self.admission),
        })
        return out


def _capacity_block(data: dict, name: str, cls):
    """One sub-block as a dataclass when the API sent an object, None when it
    sent null, or the verbatim value when it sent something out of contract.
    Named fields default to None - a missing key is unknown, never zero."""
    val = data.get(name)
    if not isinstance(val, dict):
        return val
    kwargs = {f: val.get(f) for f in getattr(cls, "__dataclass_fields__", {})
              if f != "raw"}
    return cls(raw=dict(val), **kwargs)


def _parse_capacity_payload(response) -> tuple:
    """(Capacity kwargs, "") on a usable payload, (None, reason) otherwise.
    The envelope is {success, generated_at, data}; only the `data` block is
    modelled, and every block inside it may legitimately be null."""
    if not isinstance(response, dict):
        return None, "unexpected payload shape"
    data = response.get("data")
    if not isinstance(data, dict):
        return None, "payload held no data block"
    return {
        "now": _capacity_block(data, "now", CapacityNow),
        "config": _capacity_block(data, "config", CapacityConfig),
        "history": _capacity_block(data, "history", CapacityHistory),
        "admission": _capacity_block(data, "admission", CapacityAdmission),
        "raw": dict(data),
    }, ""


def _read_capacity_cache(path: str, now: float, ttl: float) -> tuple:
    if not os.path.exists(path):
        # The cache being absent is not an error: it means there is nothing
        # to serve without the network, and the caller renders unknown.
        return None, "no cached capacity"
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except Exception as e:
        return None, f"cache present but unreadable ({type(e).__name__})"
    fetched = doc.get("fetched_at") if isinstance(doc, dict) else None
    response = doc.get("response") if isinstance(doc, dict) else None
    fields, err = _parse_capacity_payload(response)
    if err or not isinstance(fetched, (int, float)):
        return None, "cache unreadable (no usable payload or timestamp)"
    age = max(0.0, now - float(fetched))
    return Capacity(fetched_at=float(fetched), served_from="cache",
                    stale=age > ttl, age_s=age, ttl_s=ttl, **fields), ""


def capacity_admission_days(cap) -> list:
    """The admission block as an ascending [(day, entry), ...] list.

    The API sends two windows (last_7d, last_30d) of the same day shape;
    this returns the union, one entry per day, so a consumer asking "what
    does the server say about these days" never double-counts. Entries are
    verbatim - nothing is summed or defaulted here."""
    adm = getattr(cap, "admission", None) if cap is not None else None
    out: dict = {}
    for window in ("last_30d", "last_7d"):
        entries = getattr(adm, window, None) if adm is not None else None
        if not isinstance(entries, list):
            continue
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("day"), str):
                out[e["day"]] = e
    return sorted(out.items())


def fetch_capacity(allow_network: bool, *, cache_path: str | None = None,
                   now: float | None = None, ttl: float | None = None,
                   key: str | None = None, timeout: float | None = None):
    """The account's capacity payload, or None when it cannot be established.

    Mirrors fetch_catalog's cache-first shape: a cached payload serves
    without network (a stale one serves too, marked stale); the network is
    attempted only when allow_network and the cache is stale or missing. No
    key, 401, the endpoint's 4-17s latency or a timeout all degrade to None
    or to the stale cache - capacity being absent is never an error, and
    consumers must render None as unknown, never as a default."""
    capacity, _ = _capacity_with_note(allow_network, cache_path=cache_path,
                                      now=now, ttl=ttl, key=key, timeout=timeout)
    return capacity


def _capacity_with_note(allow_network: bool, *, cache_path: str | None = None,
                        now: float | None = None, ttl: float | None = None,
                        key: str | None = None,
                        timeout: float | None = None) -> tuple:
    now = time.time() if now is None else now
    ttl = CAPACITY_TTL_S if ttl is None else ttl
    path = cache_path or capacity_cache_path()
    cached, cache_err = _read_capacity_cache(path, now, ttl)
    if cached is not None and not cached.stale:
        cached.note = f"served from cache; fetched {_age_str(cached.age_s)} ago"
        return cached, cached.note
    if not allow_network:
        if cached is not None:
            cached.note = (f"cache expired (fetched {_age_str(cached.age_s)} ago); "
                           "offline, so served stale")
            return cached, cached.note
        return None, cache_err or "no cached capacity; network not allowed (offline)"
    k = catalog_key() if key is None else key
    if not k:
        if cached is not None:
            cached.note = "no routing key; served stale cache"
            return cached, cached.note
        return None, ("no routing key (LUNAROUTE_API_KEY, or "
                       "~/.pi/agent/auth.json lunaroute.key/.access)")
    data, err = _http_get_json(CAPACITY_URL, k, timeout or CAPACITY_TIMEOUT_S,
                               auth=("LUNAROUTE-API-KEY", k))
    fields, parse_err = (None, err) if data is None else _parse_capacity_payload(data)
    if parse_err:
        if cached is not None:
            cached.note = f"fetch failed ({parse_err}); served stale cache"
            return cached, cached.note
        return None, f"fetch failed ({parse_err})"
    _write_catalog_cache(path, data, now, url=CAPACITY_URL)
    capacity = Capacity(fetched_at=now, served_from="network", stale=False,
                        age_s=0.0, ttl_s=ttl, note=f"fetched from {CAPACITY_URL}",
                        **fields)
    return capacity, capacity.note


# ---- configs (declared state) -----------------------------------------------
# The ad-hoc readers (`_read_json` / `_read_text` and friends) fold into this
# one place. The keys consulted are exactly the keys the fix command and the
# snapshot need; each value carries provenance: the file and key path it came
# from, or "absent → default N" with the citation behind N. An unparsable
# file is reported unchecked, never clean.

_NO_DEFAULT = object()

_PI_SETTINGS_KEYS = (
    ("defaultProvider", _NO_DEFAULT, "", "absent; no documented default"),
    ("defaultModel", _NO_DEFAULT, "", "absent; no documented default"),
    ("httpIdleTimeoutMs", 300000, "pi DEFAULT_HTTP_IDLE_TIMEOUT_MS "
     "(luna-doctor CONTRACT.client_idle_timeout_s)", "absent"),
    ("subagents.defaultModel", _NO_DEFAULT, "", "absent; children inherit defaultModel"),
)
_PI_SUBAGENT_KEYS = (
    ("globalConcurrencyLimit", 20, "pi-subagents default", "absent"),
)
_CLAUDE_ENV_KEYS = (
    ("env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", 20, "claude-code default", "absent"),
    ("env.CLAUDE_STREAM_IDLE_TIMEOUT_MS", 300000, "claude-code stream idle floor", "absent"),
    ("env.API_TIMEOUT_MS", _NO_DEFAULT, "", "absent; no documented default"),
    ("env.CLAUDE_CODE_SUBAGENT_MODEL", _NO_DEFAULT, "",
     "absent; subagents inherit the parent model"),
    ("env.ANTHROPIC_BASE_URL", _NO_DEFAULT, "",
     "absent; claude-code may not be routed through LunaRoute"),
    # The sizing keys (the 2026-10-23 family, judged in luna_doctor/sizing.py):
    # the key names are V - both are read through claude-code's per-model
    # default+upperLimit helper - but the numeric defaults were never
    # captured, so these Facts carry no default and sizing renders them as
    # informational lines, never findings.
    ("env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", _NO_DEFAULT, "",
     "absent; the shipped default is unverified"),
    ("env.CLAUDE_CODE_MAX_CONTEXT_TOKENS", _NO_DEFAULT, "",
     "absent; the shipped default is unverified"),
)
_CODEX_KEYS = (
    ("model", _NO_DEFAULT, "", "absent; no documented default"),
    ("agents.max_concurrent_threads_per_session", _NO_DEFAULT, "",
     "absent; no documented default"),
    ("agents.default_subagent_model", _NO_DEFAULT, "",
     "absent; no documented default"),
    # V by observation (`model_context_window = 262144` in a real config);
    # its default and semantics are U, so sizing judges declared values only.
    ("model_context_window", _NO_DEFAULT, "",
     "absent; the shipped default is unverified"),
)


@dataclass
class Configs:
    pi: dict = field(default_factory=dict)
    pi_models: dict = field(default_factory=dict)
    claude_code: dict = field(default_factory=dict)
    codex: dict = field(default_factory=dict)
    swamp: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "pi": as_json(self.pi),
            "pi_models": as_json(self.pi_models),
            "claude-code": as_json(self.claude_code),
            "codex": as_json(self.codex),
            "swamp": as_json(self.swamp),
        }


def _record_file(files: dict, path: str, present: bool, parsable: bool, note: str) -> None:
    files.setdefault(path, {"present": present, "parsable": parsable, "note": note})


def _load_json(path: str, files: dict) -> tuple:
    """Read a JSON config. Returns (data-or-None, state) where state is
    "ok" | "absent" | "unparsable". Never raises."""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        _record_file(files, path, False, False, "absent")
        return None, "absent"
    try:
        with open(p) as fh:
            data = json.load(fh)
    except Exception as e:
        _record_file(files, path, True, False,
                     f"present but unparsable ({type(e).__name__}); reported unchecked")
        return None, "unparsable"
    if not isinstance(data, dict):
        _record_file(files, path, True, False, "not a JSON object; reported unchecked")
        return None, "unparsable"
    _record_file(files, path, True, True, "")
    return data, "ok"


def _json_value(data, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _fill_facts(target: dict, spec: tuple, data, path: str, state: str) -> None:
    for dotted, default, citation, absent_reason in spec:
        if state == "unparsable":
            target[dotted] = Fact(
                provenance=PROV_UNKNOWN, source=f"file {path} key {dotted}",
                reason="config file present but unparsable; reported unchecked, not clean")
            continue
        value = _json_value(data, dotted) if state == "ok" else None
        if value is not None:
            target[dotted] = Fact(value=value, provenance=PROV_DECLARED,
                                  source=f"file {path} key {dotted}")
        elif default is not _NO_DEFAULT:
            target[dotted] = Fact(value=default, provenance=PROV_CONFIGURED,
                                  source=f"absent → default {default} ({citation})")
        else:
            reason = "config file absent" if state == "absent" else absent_reason
            target[dotted] = Fact(provenance=PROV_UNKNOWN,
                                 source=f"file {path} key {dotted}", reason=reason)


def _parse_toml_lite(text: str) -> dict:
    """A line-wise TOML reader for the few keys codex consults.

    Tracks [section] headers and `key = value` lines; string values lose
    their quotes, everything else keeps its raw text. Lines that are not
    assignments (arrays, comments) are skipped. It is a scan, not a validator:
    a broken file yields what it can, and the fact's source names the file.
    """
    out: dict = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("\"", "'"):
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        out[f"{section}.{key}" if section else key] = val
    return out


def _load_codex_config(path: str, files: dict) -> tuple:
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        _record_file(files, path, False, False, "absent")
        return None, "absent"
    try:
        with open(p, errors="replace") as fh:
            text = fh.read()
    except OSError:
        _record_file(files, path, True, False, "unreadable; reported unchecked")
        return None, "unparsable"
    _record_file(files, path, True, True, "readable; scanned line-wise")
    return _parse_toml_lite(text), "ok"


def _codex_value(flat: dict, dotted: str):
    """The value for a `section.key`, tolerating the key living in another
    section (the old reader matched the bare name anywhere in the file).

    Returns (value, actual-dotted-key) or (None, dotted)."""
    if dotted in flat:
        return flat[dotted], dotted
    tail = dotted.rsplit(".", 1)[-1]
    for k in sorted(flat):
        if k.endswith("." + tail):
            return flat[k], k
    return None, dotted


def _fill_pi_models(pi_models: dict, files: dict) -> None:
    """pi's declared per-model sizing from its model-definition stores.

    Both store shapes are seen in the wild: providers under a `providers` key
    (models.json) and as top-level maps (models-store.json). First store wins
    for a given model id."""
    for store in PI_MODEL_STORES:
        data, state = _load_json(store, files)
        if state != "ok":
            continue
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else data
        if not isinstance(providers, dict):
            continue
        for pname in sorted(providers):
            pval = providers.get(pname)
            models = pval.get("models") if isinstance(pval, dict) else None
            if not isinstance(models, list):
                continue
            for entry in models:
                if not isinstance(entry, dict):
                    continue
                mid = entry.get("id") or entry.get("name")
                if not isinstance(mid, str) or not mid or mid in pi_models:
                    continue
                where = f"file {store} providers.{pname}.models"
                pi_models[mid] = {
                    "provider": pname,
                    "source": where,
                    "contextWindow": _declared_size(entry.get("contextWindow"), where, "contextWindow"),
                    "maxTokens": _declared_size(entry.get("maxTokens"), where, "maxTokens"),
                }


def _declared_size(value, where: str, key: str) -> Fact:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Fact(provenance=PROV_UNKNOWN, source=f"{where} {key}",
                    reason="not declared")
    return Fact(value=value, provenance=PROV_DECLARED, source=f"{where} {key}")


def _read_dotenv(path: str) -> dict:
    out: dict = {}
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def _read_swamp_yaml_pairs(path: str) -> dict:
    out: dict = {}
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        return out
    try:
        with open(p, errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                if k in {key for _, key, _ in SWAMP_KNOB_KEYS}:
                    out[k] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def swamp_knobs(repo_dir: str | None = None) -> dict:
    """Effective swamp concurrency knobs with provenance.

    Precedence (the one the rendered fix advice has always used): env var →
    ~/.swamp.yaml (+ ~/.swamp-sources.yaml) → the repo's .env.swamp → the
    documented default from extensions/models/obsidian_ideas.ts. Returns the
    {knob: {"value", "source", "raw"}} shape the fix command renders, where
    source is "env" | "~/.swamp.yaml" | ".env.swamp" | "default".
    """
    env_swamp: dict = {}
    if repo_dir:
        env_swamp = _read_dotenv(os.path.join(repo_dir, ".env.swamp"))
    yaml_vals: dict = {}
    for yaml_path in (SWAMP_ENV_FILE, SWAMP_ENV_GLOBAL_SOURCES):
        yaml_vals.update(_read_swamp_yaml_pairs(yaml_path))

    def resolve(key: str, default) -> dict:
        for source, table in (("env", os.environ), ("~/.swamp.yaml", yaml_vals),
                              (".env.swamp", env_swamp)):
            if key in table:
                raw = str(table[key]).strip()
                try:
                    value = int(raw) if isinstance(default, int) else raw
                except ValueError:
                    value = raw
                return {"value": value, "source": source, "raw": raw}
        return {"value": default, "source": "default", "raw": str(default)}

    return {name: resolve(key, default) for name, key, default in SWAMP_KNOB_KEYS}


def _swamp_facts(repo_dir: str | None, files: dict) -> dict:
    knobs = swamp_knobs(repo_dir)
    out: dict = {}
    for name, key, _default in SWAMP_KNOB_KEYS:
        knob = knobs[name]
        if knob["source"] == "default":
            out[key] = Fact(value=knob["value"], provenance=PROV_CONFIGURED,
                            source=f"absent → default {knob['value']} ({SWAMP_CITATION_DEFAULT})")
            continue
        if knob["source"] == "env":
            where = f"env var {key}"
        elif knob["source"] == "~/.swamp.yaml":
            where = f"file ~/.swamp.yaml key {key}"
        else:
            where = f"file {repo_dir}/.env.swamp key {key}"
        out[key] = Fact(value=knob["value"], provenance=PROV_DECLARED, source=where)
    for path in (SWAMP_ENV_FILE, SWAMP_ENV_GLOBAL_SOURCES):
        _record_file(files, path, os.path.isfile(os.path.expanduser(path)), True, "")
    if repo_dir:
        dotenv = os.path.join(repo_dir, ".env.swamp")
        _record_file(files, f"{repo_dir}/.env.swamp", os.path.isfile(dotenv), True, "")
    return out


def read_configs(*, swamp_repo_dir: str | None = None) -> Configs:
    """Every harness config the tool consults, one place, provenance per value.

    Which keys are read is unchanged from the ad-hoc readers this replaces;
    what is new is that each value says where it came from, that an absent
    key with a documented default says "absent → default N" with the citation,
    and that an unparsable file is reported unchecked, never clean.
    """
    cfg = Configs()
    files = cfg.files

    settings, state = _load_json(PI_SETTINGS, files)
    _fill_facts(cfg.pi, _PI_SETTINGS_KEYS, settings, PI_SETTINGS, state)
    subcfg, state = _load_json(PI_SUBAGENT_CFG, files)
    _fill_facts(cfg.pi, _PI_SUBAGENT_KEYS, subcfg, PI_SUBAGENT_CFG, state)
    _fill_pi_models(cfg.pi_models, files)

    claude, state = _load_json(CLAUDE_SETTINGS, files)
    _fill_facts(cfg.claude_code, _CLAUDE_ENV_KEYS, claude, CLAUDE_SETTINGS, state)

    flat, state = _load_codex_config(CODEX_CONFIG, files)
    for dotted, default, citation, absent_reason in _CODEX_KEYS:
        if state == "unparsable":
            cfg.codex[dotted] = Fact(
                provenance=PROV_UNKNOWN, source=f"file {CODEX_CONFIG} key {dotted}",
                reason="config file present but unreadable; reported unchecked, not clean")
            continue
        value, actual = _codex_value(flat or {}, dotted) if state == "ok" else (None, dotted)
        if value is not None:
            cfg.codex[dotted] = Fact(value=value, provenance=PROV_DECLARED,
                                     source=f"file {CODEX_CONFIG} key {actual}")
        elif default is not _NO_DEFAULT:
            cfg.codex[dotted] = Fact(value=default, provenance=PROV_CONFIGURED,
                                     source=f"absent → default {default} ({citation})")
        else:
            reason = "config file absent" if state == "absent" else absent_reason
            cfg.codex[dotted] = Fact(provenance=PROV_UNKNOWN,
                                     source=f"file {CODEX_CONFIG} key {dotted}", reason=reason)

    if swamp_repo_dir is None:
        from luna_doctor import cli       # swamp repo discovery lives in cli
        swamp_repo_dir = cli._swamp_repo_dir()
    cfg.swamp = _swamp_facts(swamp_repo_dir, files)
    return cfg


# ---- requests (measured state): codex ---------------------------------------
# pi and claude-code stay in cli.py (iter_pi / iter_claude) - they are heavily
# tested and this task does not regress them. codex is new, so it lives here.


def iter_codex(roots: list, since: float, until: float | None = None,
               prune_mtime: bool = True) -> list:
    """codex rollout files → one Request per model response.

    Verified rollout shape (rollout-*.jsonl, one JSON object per line):
    `turn_context` records carry the turn's model and timestamp; several
    `token_usage_record` records share one response_id with growing counts,
    so the last record per response is the final usage; `turn_aborted` marks
    a turn the user interrupted. Compressed rollouts (*.jsonl.zst) are
    skipped: the floor is Python 3.9 and the stdlib has no zstd there.

    Token fields are carried as-is on Request.usage_raw and are never merged
    with pi's or claude-code's: codex's input_tokens INCLUDES
    cached_input_tokens, while pi's input EXCLUDES cacheRead - one merged
    "input tokens" number would be a silent wrong answer. The snapshot
    exposes one view per harness.
    """
    from luna_doctor import cli       # Request + the shared log helpers (cycle)
    out = []
    seen: set = set()
    top = until if until is not None else time.time()
    for root in sorted(roots):        # argv order must not decide which copy survives
        pattern = os.path.join(glob.escape(root), "**", "*.jsonl")
        for path in sorted(glob.glob(pattern, recursive=True)):
            if prune_mtime:            # see cli.iter_pi: explicit roots never prune
                try:
                    if os.path.getmtime(path) < since - 86400:
                        continue
                except OSError:
                    continue
            session = cli._session_name(path)
            run = cli._run_of(path, root, qualify=len(roots) > 1, harness="codex")
            turn_model: dict = {}
            turn_start: dict = {}
            aborted: set = set()
            final: dict = {}          # response_id -> {"ts", "usage", "turn"}
            order: list = []
            for obj in cli.iter_jsonl(path):
                typ = obj.get("type")
                payload = obj.get("payload") or {}
                if typ == "turn_context":
                    tid = payload.get("turn_id")
                    if isinstance(tid, str) and tid:
                        turn_model[tid] = payload.get("model") or ""
                        turn_start[tid] = cli._iso_to_epoch(obj.get("timestamp"))
                elif typ == "event_msg":
                    if (payload.get("type") == "turn_aborted"
                            and isinstance(payload.get("turn_id"), str)):
                        aborted.add(payload["turn_id"])
                elif typ == "token_usage_record":
                    rid = payload.get("response_id")
                    usage = payload.get("usage")
                    if not isinstance(rid, str) or not rid or not isinstance(usage, dict):
                        continue
                    ts = cli._iso_to_epoch(obj.get("timestamp"))
                    rec = final.get(rid)
                    if rec is None:
                        final[rid] = {"ts": ts, "usage": usage, "turn": payload.get("turn_id")}
                        order.append(rid)
                    elif ts is None or rec["ts"] is None or ts >= rec["ts"]:
                        # progressive updates: the last record for a response wins
                        if ts is not None:
                            rec["ts"] = ts
                        rec["usage"] = usage
                        if payload.get("turn_id"):
                            rec["turn"] = payload["turn_id"]
            for rid in order:
                if rid in seen:
                    continue
                rec = final[rid]
                end = rec["ts"]
                if end is None or end < since or end > top:
                    continue
                seen.add(rid)
                tid = rec["turn"] or ""
                usage = rec["usage"]
                req = cli.Request(
                    harness="codex",
                    session=session,
                    model=turn_model.get(tid, ""),
                    run=run,
                    provider="",
                    start=turn_start.get(tid),
                    end=end,
                    out_tokens=int(usage.get("output_tokens") or 0),
                    in_tokens=int(usage.get("input_tokens") or 0),
                    stop_reason="aborted" if tid in aborted else None,
                    error_message=None,
                    response_id=rid,
                    usage_raw=dict(usage),
                )
                req.tier, req.demoted, req.reason = cli.tier_of(req.model)
                req.outcome = cli.classify(asdict(req))
                out.append(req)
    return out


def token_totals(requests: list) -> dict:
    """Per-harness sums of each raw usage field, never merged across harnesses.

    The keys are whatever each harness's own usage records carried - pi logs
    input/cacheRead/cacheWrite/output/totalTokens, claude-code logs
    input_tokens/cache_read_input_tokens/..., codex logs
    input_tokens/cached_input_tokens/... - and they keep those names here.
    Summing across harnesses would be the silent wrong answer, so nothing
    does: one sub-dict per harness, raw field names as logged.
    """
    out: dict = {}
    for req in requests:
        raw = getattr(req, "usage_raw", None) or {}
        per = out.setdefault(req.harness, {})
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            per[key] = per.get(key, 0) + value
    return out


# ---- the combined snapshot ---------------------------------------------------


def _bare_model_id(value) -> str:
    """The catalog's id for a declared model name. Configs may carry the
    provider-qualified form (pi writes `lunaroute/glm-5.3`); the catalog ids
    are bare, so the provider prefix is dropped for the join only."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return value.strip().rsplit("/", 1)[-1]


def _catalog_fact_for(model_id: str, catalog, note: str) -> Fact:
    """The catalog's limits for one model id, or unknown with the reason.

    Never a default: a model missing from the catalog, or a catalog that
    could not be fetched, is an explicit unknown the consumer must render."""
    if not model_id:
        return Fact(provenance=PROV_UNKNOWN, source="", reason="no model declared")
    if catalog is None:
        return Fact(provenance=PROV_UNKNOWN, source=MODELS_URL,
                    reason=f"catalog unknown ({note})")
    entry = catalog.models.get(model_id)
    if entry is None:
        return Fact(provenance=PROV_UNKNOWN,
                    source=catalog.describe(),
                    reason="model not in the live catalog")
    return Fact(value=entry.to_dict(), provenance=PROV_CATALOG,
                source=catalog.describe())


def _routing_entry(fact: Fact, catalog, note: str) -> dict:
    model_id = _bare_model_id(fact.value)
    return {
        "value": fact.value,
        "provenance": fact.provenance,
        "source": fact.source,
        "reason": fact.reason,
        "model_id": model_id or None,
        "catalog": _catalog_fact_for(model_id, catalog, note),
    }


def _join_routing(configs: Configs, catalog, note: str) -> dict:
    """Each harness's declared model keys → the model it is actually routed
    to → that model's real catalog limits."""
    routing: dict = {}

    default_model = configs.pi.get("defaultModel")
    subagents = configs.pi.get("subagents.defaultModel")
    if subagents is not None and subagents.value is not None:
        sub_fact = subagents
    elif default_model is not None and default_model.value is not None:
        sub_fact = Fact(value=default_model.value, provenance=default_model.provenance,
                        source=(default_model.source
                                + " (subagents.defaultModel absent; children inherit the default)"))
    else:
        sub_fact = Fact(provenance=PROV_UNKNOWN,
                        source=(subagents.source if subagents else ""),
                        reason="absent; no default model declared either")
    routing["pi"] = {
        "defaultModel": _routing_entry(default_model, catalog, note) if default_model
        else _routing_entry(Fact(), catalog, note),
        "subagents.defaultModel": _routing_entry(sub_fact, catalog, note),
    }

    claude_model = configs.claude_code.get("env.CLAUDE_CODE_SUBAGENT_MODEL")
    routing["claude-code"] = {
        "env.CLAUDE_CODE_SUBAGENT_MODEL": _routing_entry(
            claude_model or Fact(), catalog, note),
    }

    codex_model = configs.codex.get("model")
    routing["codex"] = {
        "model": _routing_entry(codex_model or Fact(), catalog, note),
    }

    swamp_model = configs.swamp.get("SWAMP_LLM_MODEL")
    routing["swamp"] = {
        "SWAMP_LLM_MODEL": _routing_entry(swamp_model or Fact(), catalog, note),
    }
    return routing


def build_snapshot(requests: list, *, since: float | None = None,
                   until: float | None = None, allow_network: bool = False,
                   include_codex: bool = True, codex_roots: list | None = None,
                   now: float | None = None, cache_path: str | None = None,
                   key: str | None = None, ttl: float | None = None,
                   configs: Configs | None = None) -> "Snapshot":
    """The one object commands consume: measured requests, declared configs
    and the live catalog, joined per model.

    `requests` are the requests the command already gathered (pi and
    claude-code). When `since` is given, the codex store behind the existing
    discovery is also read and its requests join the snapshot - they never
    enter analyse()'s metrics, which stay harness-gated as before.
    """
    now = time.time() if now is None else now
    if configs is None:
        configs = read_configs()
    catalog, catalog_note = _catalog_with_note(allow_network, cache_path=cache_path,
                                               now=now, ttl=ttl, key=key)
    limits, limits_note = _limits_with_note(allow_network, now=now, key=key)

    joined = list(requests)
    if include_codex and since is not None:
        roots = codex_roots if codex_roots is not None else [os.path.expanduser(CODEX_STORE)]
        joined += iter_codex(roots, since, until, prune_mtime=True)

    counts: dict = {}
    harness_counts: dict = {}
    for req in joined:
        mid = req.model or "(unknown)"
        counts[mid] = counts.get(mid, 0) + 1
        harness_counts.setdefault(mid, {})
        harness_counts[mid][req.harness] = harness_counts[mid].get(req.harness, 0) + 1

    routing = _join_routing(configs, catalog, catalog_note)
    ids = set(counts) | set(configs.pi_models)
    for section in routing.values():
        for entry in section.values():
            if entry["model_id"]:
                ids.add(entry["model_id"])

    models: dict = {}
    for mid in sorted(ids):
        models[mid] = {
            "requests": Fact(value=counts.get(mid, 0), provenance=PROV_MEASURED,
                             source="log records in the window"),
            "harnesses": harness_counts.get(mid, {}),
            "in_catalog": bool(catalog is not None and mid in catalog.models),
            "catalog": _catalog_fact_for(mid, catalog, catalog_note),
            "declared": configs.pi_models.get(mid),
        }

    findings: list = []
    if catalog is None:
        findings.append({"kind": "catalog_unknown", "reason": catalog_note})
    missing: dict = {}
    for mid, row in models.items():
        # Only a model the machine actually used, or one a routing key points
        # at, is a finding when the live catalog does not list it: a local
        # model declared in pi's store and never sent anywhere is a row, not
        # an event.
        if catalog is None or row["in_catalog"]:
            continue
        if (row["requests"].value or 0) > 0:
            missing.setdefault(mid, {"kind": "model_not_in_catalog", "model": mid,
                                     "requests": counts.get(mid, 0), "declared_in": []})
    for harness, section in routing.items():
        for key, entry in section.items():
            mid = entry["model_id"]
            if mid and catalog is not None and mid not in catalog.models:
                missing.setdefault(mid, {"kind": "model_not_in_catalog", "model": mid,
                                         "requests": counts.get(mid, 0),
                                         "declared_in": []})["declared_in"].append(
                    f"{harness} {key}")
    findings.extend(sorted(missing.values(), key=lambda f: f["model"]))

    tokens = token_totals(joined)
    return Snapshot(
        requests=joined,
        catalog=catalog,
        catalog_note=catalog_note,
        limits=limits,
        limits_note=limits_note,
        configs=configs,
        models=models,
        routing=routing,
        tokens=tokens,
        findings=findings,
    )


@dataclass
class Snapshot:
    """Measured + declared + catalog, joined. Every leaf is a Fact or a plain
    structure whose keys name their own provenance; nothing in here is a
    guessed default, and a missing value is unknown with a reason."""

    requests: list = field(default_factory=list)
    catalog: object = None
    catalog_note: str = ""
    limits: object = None
    limits_note: str = ""
    configs: object = None
    models: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "catalog": self.catalog.to_dict() if self.catalog is not None
            else {"provenance": PROV_UNKNOWN, "reason": self.catalog_note},
            "limits": self.limits.to_dict() if self.limits is not None
            else {"provenance": PROV_UNKNOWN, "reason": self.limits_note},
            "configs": self.configs.to_dict() if self.configs is not None
            else {"provenance": PROV_UNKNOWN, "reason": "configs unread"},
            "tokens": self.tokens,
            "tokens_note": ("per harness, raw field names as logged, never merged: "
                            "codex input_tokens includes cached_input_tokens, "
                            "pi input excludes cacheRead"),
            "models": as_json(self.models),
            "routing": as_json(self.routing),
            "findings": self.findings,
            "provenance": PROVENANCE_LEGEND,
        }


# ---- agentsview: a second, read-only view of session history ---------------
# agentsview indexes the session files of several agent harnesses into SQLite
# and can report what it holds. It is optional, and luna-doctor treats it as a
# second opinion about session history, never as a log store: the reports keep
# reading the harness logs directly.
#
# Read-only means here: one `projects --json` query, no daemon, no serve, no
# sync. A machine where agentsview is installed but not answering is reported
# as unreadable with a reason, never as absent and never as a count.

AGENTSVIEW_TIMEOUT_S = 5.0


def agentsview_status(timeout: float = AGENTSVIEW_TIMEOUT_S) -> dict:
    """What a local agentsview holds, or why we cannot say.

    Never starts a daemon, never syncs, never writes. `present` says the binary
    is on PATH; `readable` says the query answered; `sessions` and `projects`
    are only set when the answer parsed.
    """
    exe = shutil.which("agentsview")
    out = {"present": exe is not None, "path": exe, "readable": False,
           "sessions": None, "projects": None, "reason": None,
           "query": "agentsview projects --json"}
    if exe is None:
        out["reason"] = "not installed"
        return out
    try:
        proc = subprocess.run([exe, "projects", "--json"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        out["reason"] = f"query failed: {e.__class__.__name__}"
        return out
    if proc.returncode != 0:
        out["reason"] = f"query exited {proc.returncode}"
        return out
    try:
        payload = json.loads(proc.stdout or "[]")
    except ValueError:
        out["reason"] = "query output was not JSON"
        return out
    if not isinstance(payload, list):
        out["reason"] = "query output was not a list"
        return out
    sessions = 0
    for entry in payload:
        if isinstance(entry, dict) and isinstance(entry.get("session_count"), int):
            sessions += entry["session_count"]
    out["readable"] = True
    out["projects"] = len(payload)
    out["sessions"] = sessions
    return out
