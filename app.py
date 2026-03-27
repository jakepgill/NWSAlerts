import os
import threading
import time
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as futures_wait
from typing import Any, Dict, List, Optional, Tuple
from functools import wraps
from datetime import datetime, timedelta

import json
import requests
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, Response, request, session, redirect, url_for

import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(minutes=config.ADMIN_SESSION_TIMEOUT_MINUTES)

# ── Log buffer ─────────────────────────────────────────────────────────────
_log_buffer = collections.deque(maxlen=200)
_log_lock = threading.Lock()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _log_lock:
        _log_buffer.append(entry)
    print(entry)

DEFAULT_SELECTED_EVENTS = [
    "Air Quality Alert","Avalanche Advisory","Avalanche Watch","Avalanche Warning",
    "Blizzard Warning","Civil Danger Warning","Civil Emergency Message",
    "Coastal Flood Advisory","Coastal Flood Warning","Coastal Flood Watch","Coastal Flood Statement",
    "Cold Weather Advisory","Dense Fog Advisory","Dense Smoke Advisory","Dust Storm Warning",
    "Earthquake Warning","Evacuation Immediate","Extreme Cold Warning","Extreme Cold Watch",
    "Extreme Fire Danger","Extreme Heat Warning","Extreme Heat Watch","Extreme Wind Warning",
    "Fire Warning","Flash Flood Warning","Flash Flood Watch","Flash Flood Statement",
    "Flood Advisory","Flood Warning","Flood Watch","Flood Statement",
    "Freeze Warning","Freeze Watch","Freezing Fog Advisory","Frost Advisory",
    "Hazardous Weather Outlook","Heat Advisory","High Wind Warning","High Wind Watch",
    "Hurricane Warning","Hurricane Watch","Hurricane Force Wind Warning","Hurricane Force Wind Watch",
    "Ice Storm Warning","Local Area Emergency","Red Flag Warning",
    "Severe Thunderstorm Warning","Severe Thunderstorm Watch","Severe Weather Statement",
    "Shelter In Place Warning","Snow Squall Warning","Special Weather Statement",
    "Storm Warning","Storm Watch","Storm Surge Warning","Storm Surge Watch",
    "Tornado Watch","Tornado Warning","Tropical Storm Warning","Tropical Storm Watch",
    "Tsunami Warning","Tsunami Watch","Tsunami Advisory","Typhoon Warning","Volcano Warning",
    "Wind Advisory","Winter Storm Warning","Winter Storm Watch","Winter Weather Advisory",
]

FETCH_INTERVAL_SECONDS = 15
EXCLUDE_MARINE_ZONES = True
EXCLUDE_TERRITORIES = True
TERRITORY_PREFIXES = ("GU", "AS", "MP", "PR", "VI")
ZONE_CACHE_TTL_SECONDS = 6 * 60 * 60
_zone_geom_cache: Dict[str, Dict[str, Any]] = {}
_zone_cache_lock = threading.Lock()
ZONE_FETCH_MAX_WORKERS = 20
MAX_ZONES_PER_ALERT = 60
ALERT_TYPES_REFRESH_SECONDS = 24 * 60 * 60

ALERTS_ETAG: Optional[str] = None
all_event_types: List[str] = []
all_event_types_ts: int = 0
active_alerts_all: List[Dict[str, Any]] = []
active_event_names: List[str] = []

fetch_status: Dict[str, Any] = {
    "ok": False, "last_ok_ts": 0, "last_try_ts": 0,
    "last_http_status": None, "last_error": "",
    "nws_total_features": 0, "nws_actual_count": 0,
    "after_filter_count": 0, "nws_has_features_key": False,
    "nws_response_snippet": "", "nws_title": "", "nws_type": "",
}

_state_lock = threading.Lock()

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "NWSAlerts/1.5 (https://nwsalerts.net)",
    "Accept": "application/geo+json, application/ld+json, application/json;q=0.9, */*;q=0.8",
})

_server_start_time: float = time.time()

# ── Response time tracking ──────────────────────────────────────────────────
_response_times: collections.deque = collections.deque(maxlen=20)
_response_times_lock = threading.Lock()

# ── Active user tracking (rolling 5-min window of /data_shown hits) ─────────
_active_user_ts: collections.deque = collections.deque(maxlen=2000)
_active_user_lock = threading.Lock()

# ── Rate limit tracking (per-IP, rolling 60 s window on proxy endpoints) ────
_rate_limit_data: Dict[str, collections.deque] = {}
_rate_limit_lock = threading.Lock()

# ── Peak alert counts ────────────────────────────────────────────────────────
_peak_today: int = 0
_peak_today_date: str = ""
_peak_week: int = 0
_peak_week_num: str = ""
_peaks_lock = threading.Lock()

# ── Previous alert IDs for webhook new-alert detection ──────────────────────
_prev_alert_ids: set = set()
_prev_alert_ids_lock = threading.Lock()


def _zone_id_from_url(zone_url: str) -> str:
    return zone_url.rstrip("/").split("/")[-1].upper()

def _is_marine_zone(zone_url: str) -> bool:
    return ("/zones/marine/" in zone_url) or ("/zones/offshore/" in zone_url)

def _is_territory_zone(zone_url: str) -> bool:
    return _zone_id_from_url(zone_url).startswith(TERRITORY_PREFIXES)

def _should_exclude_alert(affected_zones: List[str]) -> bool:
    zones = affected_zones or []
    if not zones:
        return False
    if EXCLUDE_MARINE_ZONES and all(_is_marine_zone(z) for z in zones):
        return True
    if EXCLUDE_TERRITORIES and all(_is_territory_zone(z) for z in zones):
        return True
    # Geo filter: exclude if all zones are in the excluded-states list
    try:
        excl = list(_geo_filter.get("excluded_states", []))
        if excl:
            def _zstate(z):
                zid = _zone_id_from_url(z)
                return zid[:2] if len(zid) >= 2 else ""
            if all(_zstate(z) in excl for z in zones):
                return True
    except Exception:
        pass
    return False

def _get_zone_geometry(zone_url: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _zone_cache_lock:
        cached = _zone_geom_cache.get(zone_url)
        if cached and (now - cached["ts"] < ZONE_CACHE_TTL_SECONDS):
            return cached["geometry"]
    try:
        r = SESSION.get(zone_url, timeout=12)
        r.raise_for_status()
        data = r.json()
        geom = data.get("geometry") if isinstance(data, dict) else None
    except Exception:
        geom = None
    with _zone_cache_lock:
        _zone_geom_cache[zone_url] = {"ts": time.time(), "geometry": geom}
    return geom

def _merge_geometries_as_multipolygon(geoms):
    multipolys = []
    for g in geoms:
        if not isinstance(g, dict) or not g.get("type") or not g.get("coordinates"):
            continue
        if g["type"] == "Polygon":
            multipolys.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            for poly in g["coordinates"]:
                multipolys.append(poly)
    if not multipolys:
        return None
    return {"type": "MultiPolygon", "coordinates": multipolys}

def _centroid_from_geometry(geom):
    if not isinstance(geom, dict) or not geom.get("type") or not geom.get("coordinates"):
        return None
    pts = []
    def add_ring(ring):
        for p in ring:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append((float(p[1]), float(p[0])))
    try:
        if geom["type"] == "Polygon":
            if geom["coordinates"] and geom["coordinates"][0]:
                add_ring(geom["coordinates"][0])
        elif geom["type"] == "MultiPolygon":
            for poly in geom["coordinates"]:
                if poly and poly[0]:
                    add_ring(poly[0])
    except Exception:
        return None
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

# Fetch all unique zones for active alerts in one background pass per cycle.
# Uses 8 workers — fast enough to load polygons in ~30s, light enough not to hammer CPU.
_zone_prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zone_prefetch")

def _prefetch_zones_batch(zone_urls):
    """Fetch a batch of zones with 8 parallel workers. Called once per alert cycle."""
    if not zone_urls:
        return
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_get_zone_geometry, z): z for z in zone_urls}
            for f in as_completed(futures, timeout=90):
                try:
                    f.result()
                except Exception:
                    pass
    except Exception:
        pass

def _simplify_coords(coords, tolerance=0.01):
    """Douglas-Peucker simplification on a ring of [lon,lat] pairs."""
    if len(coords) <= 4:
        return coords
    def perp_dist(p, a, b):
        dx, dy = b[0]-a[0], b[1]-a[1]
        if dx == 0 and dy == 0:
            return ((p[0]-a[0])**2 + (p[1]-a[1])**2) ** 0.5
        t = ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / (dx*dx + dy*dy)
        t = max(0, min(1, t))
        return (((p[0]-a[0]-t*dx)**2 + (p[1]-a[1]-t*dy)**2)) ** 0.5
    def rdp(pts):
        if len(pts) <= 2:
            return pts
        dmax, idx = 0, 0
        for i in range(1, len(pts)-1):
            d = perp_dist(pts[i], pts[0], pts[-1])
            if d > dmax:
                dmax, idx = d, i
        if dmax > tolerance:
            return rdp(pts[:idx+1])[:-1] + rdp(pts[idx:])
        return [pts[0], pts[-1]]
    result = rdp(list(coords))
    if len(result) < 4:
        return list(coords)
    # Ensure ring is closed
    if result[0] != result[-1]:
        result.append(result[0])
    return result


def _simplify_geometry(geom, tolerance=0.01):
    """Simplify a GeoJSON geometry's coordinates to reduce point count."""
    if not isinstance(geom, dict) or not geom.get("coordinates"):
        return geom
    try:
        t = geom["type"]
        if t == "Polygon":
            return {**geom, "coordinates": [_simplify_coords(ring, tolerance) for ring in geom["coordinates"]]}
        elif t == "MultiPolygon":
            return {**geom, "coordinates": [
                [_simplify_coords(ring, tolerance) for ring in poly]
                for poly in geom["coordinates"]
            ]}
    except Exception:
        pass
    return geom


def _build_polygon_from_zones_parallel(affected_zones, max_zones):
    """Return cached geometries immediately (non-blocking).
    Uncached zones are collected and fetched as a batch after the main loop."""
    zones = [z for z in (affected_zones or []) if isinstance(z, str) and z.strip()]
    if EXCLUDE_MARINE_ZONES:
        zones = [z for z in zones if not _is_marine_zone(z)]
    if EXCLUDE_TERRITORIES:
        zones = [z for z in zones if not _is_territory_zone(z)]
    zones = zones[:max_zones]
    if not zones:
        return None

    zone_geoms = []
    now = time.time()
    with _zone_cache_lock:
        for z in zones:
            cached = _zone_geom_cache.get(z)
            if cached and (now - cached["ts"] < ZONE_CACHE_TTL_SECONDS):
                if isinstance(cached["geometry"], dict) and cached["geometry"].get("coordinates"):
                    zone_geoms.append(cached["geometry"])

    return _merge_geometries_as_multipolygon(zone_geoms)

def _safe_features_list(data):
    if not isinstance(data, dict):
        return []
    feats = data.get("features", [])
    if not isinstance(feats, list):
        return []
    return [f for f in feats if isinstance(f, dict)]

def _safe_str(v, default=""):
    return str(v) if isinstance(v, str) else default


def _track_rate_limit(ip: str) -> None:
    now = time.time()
    with _rate_limit_lock:
        if ip not in _rate_limit_data:
            # Cap per-IP history to 500 hits; prune IPs idle for >1h periodically
            _rate_limit_data[ip] = collections.deque(maxlen=500)
            if len(_rate_limit_data) > 2000:
                cutoff = now - 3600
                stale = [k for k, v in _rate_limit_data.items() if not v or v[-1] < cutoff]
                for k in stale:
                    del _rate_limit_data[k]
        _rate_limit_data[ip].append(now)


def _update_peaks(count: int) -> None:
    global _peak_today, _peak_today_date, _peak_week, _peak_week_num
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_str  = datetime.now().strftime("%Y-W%W")
    with _peaks_lock:
        if today_str != _peak_today_date:
            _peak_today = 0
            _peak_today_date = today_str
        if count > _peak_today:
            _peak_today = count
        if week_str != _peak_week_num:
            _peak_week = 0
            _peak_week_num = week_str
        if count > _peak_week:
            _peak_week = count


def _fire_webhook_post(url: str, payload: dict) -> None:
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        log(f"[webhook] POST failed: {e}")


def _maybe_fire_webhook(alerts: list) -> None:
    global _prev_alert_ids
    with _prev_alert_ids_lock:
        current_ids = {a["id"] for a in alerts if a.get("id")}
        new_ids = current_ids - _prev_alert_ids
        _prev_alert_ids = current_ids
    if not new_ids:
        return
    with _tag_overrides_lock:
        wh = dict(_webhook_config)
    if not (wh.get("enabled") and wh.get("url")):
        return
    sev_order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
    threshold = sev_order.get(wh.get("min_severity", "Extreme"), 0)
    for alert in alerts:
        if alert.get("id") not in new_ids:
            continue
        sev = alert.get("severity", "Unknown")
        if sev_order.get(sev, 99) > threshold:
            continue
        payload = {
            "username": "NWS Alerts",
            "embeds": [{
                "title": alert.get("event", "Alert"),
                "description": (alert.get("headline") or alert.get("area", ""))[:512],
                "color": 0xFF2929 if sev == "Extreme" else 0xFF8C00,
                "fields": [
                    {"name": "Severity", "value": sev, "inline": True},
                    {"name": "Area", "value": alert.get("area", "—")[:200], "inline": True},
                ],
                "footer": {"text": f"NWSAlerts \u2022 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
            }]
        }
        threading.Thread(target=_fire_webhook_post, args=(wh["url"], payload), daemon=True).start()
        log(f"[webhook] Fired for {alert.get('event')} \u2014 {alert.get('id','')[:40]}")


def fetch_active_alerts_loop():
    global active_alerts_all, active_event_names, fetch_status, ALERTS_ETAG
    log("[fetch_active_alerts_loop] thread started")

    while True:
        local_status = dict(fetch_status)
        local_status["last_try_ts"] = int(time.time())
        local_status["last_error"] = ""
        local_status["last_http_status"] = None
        local_status["nws_response_snippet"] = ""
        local_status["nws_title"] = ""
        local_status["nws_has_features_key"] = False
        local_status["nws_type"] = ""

        try:
            log("[fetch_active_alerts_loop] fetching NWS...")
            url = "https://api.weather.gov/alerts/active?status=actual"
            request_headers = {
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
            }
            if ALERTS_ETAG:
                request_headers["If-None-Match"] = ALERTS_ETAG

            _fetch_start = time.time()
            r = SESSION.get(url, timeout=15, headers=request_headers)
            local_status["last_http_status"] = r.status_code
            log(f"[fetch_active_alerts_loop] HTTP {r.status_code}")

            if r.status_code == 304:
                with _state_lock:
                    fetch_status["ok"] = True
                    fetch_status["last_try_ts"] = int(time.time())
                    fetch_status["last_http_status"] = 304
                time.sleep(FETCH_INTERVAL_SECONDS)
                continue

            r.raise_for_status()
            ALERTS_ETAG = r.headers.get("ETag")

            text = r.text or ""
            local_status["nws_response_snippet"] = text[:400].replace("\n", " ").replace("\r", " ")

            data = r.json()
            if isinstance(data, dict):
                local_status["nws_title"] = str(data.get("title", ""))[:120]
                local_status["nws_has_features_key"] = "features" in data
                local_status["nws_type"] = str(data.get("type", ""))[:60]

            features = _safe_features_list(data)
            local_status["nws_total_features"] = len(features)
            log(f"[fetch_active_alerts_loop] {len(features)} features")

            # Pre-fetch zone geometries for alerts that lack their own polygon so that
            # every alert has geometry on the SAME cycle it first appears.
            needed_zones: set = set()
            for _f in features:
                _g = _f.get("geometry")
                if isinstance(_g, dict) and _g.get("coordinates"):
                    continue  # alert already has polygon
                _aff = _f.get("properties", {}).get("affectedZones", [])
                if isinstance(_aff, list):
                    for _z in _aff:
                        if not (isinstance(_z, str) and _z.strip()):
                            continue
                        if EXCLUDE_MARINE_ZONES and _is_marine_zone(_z):
                            continue
                        if EXCLUDE_TERRITORIES and _is_territory_zone(_z):
                            continue
                        needed_zones.add(_z)
            if needed_zones:
                _now_t = time.time()
                uncached_now = []
                with _zone_cache_lock:
                    for _z in needed_zones:
                        _c = _zone_geom_cache.get(_z)
                        if not _c or (_now_t - _c["ts"] >= ZONE_CACHE_TTL_SECONDS):
                            uncached_now.append(_z)
                if uncached_now:
                    log(f"[fetch_active_alerts_loop] pre-fetching {len(uncached_now)} zones synchronously...")
                    _pf_ex = ThreadPoolExecutor(max_workers=8)
                    _pf_futs = [_pf_ex.submit(_get_zone_geometry, _z) for _z in uncached_now]
                    futures_wait(_pf_futs, timeout=10)
                    _pf_ex.shutdown(wait=False)

            alerts = []
            events_set = set()
            actual_count = 0
            all_uncached_zones = set()  # collect any remaining zones for follow-up prefetch

            for feature in features:
                props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
                geom  = feature.get("geometry")  if isinstance(feature.get("geometry"), dict)  else None

                msg_type = props.get("messageType", "")
                if isinstance(msg_type, str) and msg_type.lower() == "cancel":
                    continue
                if props.get("status") != "Actual":
                    continue
                actual_count += 1

                affected = props.get("affectedZones", [])
                if not isinstance(affected, list):
                    affected = []
                affected = [z for z in affected if isinstance(z, str)]

                if _should_exclude_alert(affected):
                    continue

                event = props.get("event", "Unknown Alert")
                if not isinstance(event, str):
                    event = "Unknown Alert"
                events_set.add(event)

                feature_id = ""
                for id_src in [feature.get("id"), props.get("@id"), props.get("id")]:
                    if isinstance(id_src, str) and id_src:
                        feature_id = id_src
                        break

                zone_ids = []
                try:
                    zone_ids = [_zone_id_from_url(z) for z in affected if isinstance(z, str) and z.strip()]
                except Exception:
                    zone_ids = []

                alert_obj = {
                    "id": feature_id,
                    "event": event,
                    "severity":    _safe_str(props.get("severity")),
                    "urgency":     _safe_str(props.get("urgency")),
                    "certainty":   _safe_str(props.get("certainty")),
                    "area":        _safe_str(props.get("areaDesc"), "Unknown"),
                    "headline":    _safe_str(props.get("headline")),
                    "description": _safe_str(props.get("description")),
                    "instruction": _safe_str(props.get("instruction")),
                    "sender":      _safe_str(props.get("senderName")),
                    "sent":        _safe_str(props.get("sent")),
                    "updated":     _safe_str(props.get("updated")),
                    "effective":   _safe_str(props.get("effective")),
                    "onset":       _safe_str(props.get("onset")),
                    "expires":     _safe_str(props.get("expires")),
                    "geometry": None,
                    "centroid": None,
                    "zone_ids": zone_ids,
                }

                if isinstance(geom, dict) and geom.get("coordinates"):
                    alert_obj["geometry"] = _simplify_geometry(geom)
                else:
                    built = _build_polygon_from_zones_parallel(affected, MAX_ZONES_PER_ALERT)
                    if built:
                        alert_obj["geometry"] = _simplify_geometry(built)
                    else:
                        # Queue uncached zones for batch prefetch
                        now_t = time.time()
                        for z in (affected or []):
                            if isinstance(z, str) and z.strip():
                                if EXCLUDE_MARINE_ZONES and _is_marine_zone(z): continue
                                if EXCLUDE_TERRITORIES and _is_territory_zone(z): continue
                                with _zone_cache_lock:
                                    cached = _zone_geom_cache.get(z)
                                    if not cached or (now_t - cached["ts"] >= ZONE_CACHE_TTL_SECONDS):
                                        all_uncached_zones.add(z)

                c = _centroid_from_geometry(alert_obj["geometry"]) if alert_obj["geometry"] else None
                if c:
                    alert_obj["centroid"] = {"lat": c[0], "lon": c[1]}

                alerts.append(alert_obj)

            # Fire one batch prefetch for all uncached zones
            if all_uncached_zones:
                log(f"[fetch_active_alerts_loop] prefetching {len(all_uncached_zones)} zones in background...")
                _zone_prefetch_executor.submit(_prefetch_zones_batch, list(all_uncached_zones))

            log(f"[fetch_active_alerts_loop] {len(alerts)} alerts saved")
            with _state_lock:
                active_alerts_all = alerts
                active_event_names = sorted(events_set)
                local_status["nws_actual_count"] = actual_count
                local_status["after_filter_count"] = len(alerts)
                local_status["ok"] = True
                local_status["last_ok_ts"] = int(time.time())
                fetch_status = local_status
            # ── Record response time, update peaks, fire webhook ──────────────
            with _response_times_lock:
                _response_times.append(round(time.time() - _fetch_start, 3))
            _update_peaks(len(alerts))
            _maybe_fire_webhook(alerts)

        except Exception as e:
            log(f"[fetch_active_alerts_loop] error: {repr(e)}")
            err_msg = "NWS timeout (No Action Needed)" if "ReadTimeout" in repr(e) else repr(e)
            with _state_lock:
                local_status["ok"] = False
                local_status["last_error"] = err_msg
                fetch_status = local_status

        time.sleep(FETCH_INTERVAL_SECONDS)


def fetch_alert_types_once():
    global all_event_types, all_event_types_ts
    try:
        r = SESSION.get("https://api.weather.gov/alerts/types", timeout=12)
        r.raise_for_status()
        data = r.json()
        types = []
        if isinstance(data, dict):
            for key in ("eventTypes", "types"):
                val = data.get(key)
                if isinstance(val, list):
                    types = val
                    break
        types = [t for t in types if isinstance(t, str) and t.strip()]
        merged = sorted(set(types).union(DEFAULT_SELECTED_EVENTS))
        with _state_lock:
            all_event_types = merged
            all_event_types_ts = int(time.time())
        return merged
    except Exception as e:
        log(f"[fetch_alert_types_once] error: {repr(e)}")
        return []

def fetch_alert_types_loop():
    while True:
        fetch_alert_types_once()
        time.sleep(ALERT_TYPES_REFRESH_SECONDS)


_threads_started = False

def start_background_threads():
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    log("[start_background_threads] starting threads...")
    threading.Thread(target=fetch_active_alerts_loop, daemon=True).start()
    threading.Thread(target=fetch_alert_types_loop, daemon=True).start()

@app.before_request
def _ensure_background_threads():
    start_background_threads()


# ── API endpoints ──────────────────────────────

@app.get("/data_all")
def data_all():
    with _state_lock:
        alerts = list(active_alerts_all)
    with _tag_overrides_lock:
        suppressed = dict(_suppressed_alerts)
        injected = [a for a in _injected_alerts if a.get("enabled", True)]
    if suppressed:
        alerts = [a for a in alerts if a.get("id") not in suppressed]
    resp = jsonify({"alerts": injected + alerts})
    resp.headers["Cache-Control"] = "public, max-age=14"
    return resp

@app.get("/data_shown")
def data_shown():
    # Track active user estimate
    with _active_user_lock:
        _active_user_ts.append(time.time())
    with _state_lock:
        alerts = list(active_alerts_all)
        st     = dict(fetch_status)
    with _tag_overrides_lock:
        overrides = dict(_tag_overrides)
        user_override_enabled = _user_override_enabled
        announcements = list(_announcements)
        suppressed = dict(_suppressed_alerts)
        motd = dict(_motd)
        injected = [a for a in _injected_alerts if a.get("enabled", True)]
    if suppressed:
        alerts = [a for a in alerts if a.get("id") not in suppressed]
    resp = jsonify({
        "alerts": injected + alerts, "status": st,
        "tag_overrides": overrides, "user_override_enabled": user_override_enabled,
        "announcements": announcements, "motd": motd,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/status")
def status():
    with _state_lock:
        st = dict(fetch_status)
    resp = jsonify(st)
    resp.headers["Cache-Control"] = "public, max-age=14"
    return resp

@app.get("/alert_types_all")
def alert_types_all():
    now = int(time.time())
    with _state_lock:
        have_types = bool(all_event_types)
        ts = all_event_types_ts
        active_names_snapshot = list(active_event_names)
        types_snapshot = list(all_event_types)
    if (not have_types) or (now - ts > ALERT_TYPES_REFRESH_SECONDS):
        fetch_alert_types_once()
        with _state_lock:
            types_snapshot = list(all_event_types)
    merged = types_snapshot[:] if types_snapshot else sorted(set(DEFAULT_SELECTED_EVENTS).union(active_names_snapshot))
    return jsonify({"events": merged})


# ── Proxy routes ────────────────────────────────
# /imgproxy  — image fetches (NWS WWA + WPC surface)
# /dataproxy — all API/data fetches (SPC, NWS API, NHC, WPC, etc.)

ALLOWED_DOMAINS = {
    'forecast.weather.gov',
    'www.wpc.ncep.noaa.gov',
    'api.weather.gov',
    'www.spc.noaa.gov',
    'www.nhc.noaa.gov',
    'mesonet.agron.iastate.edu',
    'tgftp.nws.noaa.gov',
}

# In-memory image cache — stores (content, content_type, timestamp)
_img_cache: Dict[str, Any] = {}
_img_cache_lock = threading.Lock()
IMG_CACHE_TTLS = {
    'mesonet.agron.iastate.edu': 60,
    'forecast.weather.gov':       120,
    'www.wpc.ncep.noaa.gov':      300,
    'www.spc.noaa.gov':           300,
    'www.nhc.noaa.gov':           300,
    'tgftp.nws.noaa.gov':         120,
}
IMG_CACHE_TTL_DEFAULT = 60

# Per-domain cache TTLs for data/API responses (in seconds)
_data_cache: Dict[str, Any] = {}
_data_cache_lock = threading.Lock()
DATA_CACHE_TTLS = {
    'www.spc.noaa.gov':           300,   # SPC outlooks — updates a few times a day
    'www.nhc.noaa.gov':           300,   # NHC data — updates infrequently
    'www.wpc.ncep.noaa.gov':      300,   # WPC data — updates every few hours
    'forecast.weather.gov':        60,   # NWS forecasts
    'api.weather.gov':            300,   # NWS API
    'mesonet.agron.iastate.edu':   60,   # IEM data
    'tgftp.nws.noaa.gov':         120,   # NWS FTP data
}
DATA_CACHE_TTL_DEFAULT = 60

def _prune_cache(cache, ttls, default_ttl):
    now = time.time()
    expired = [k for k, v in cache.items() if now - v['ts'] > ttls.get(urlparse(k).netloc, default_ttl)]
    for k in expired:
        del cache[k]

@app.route('/imgproxy')
def img_proxy():
    if request.args.get('ping'):
        return 'ok', 200
    _track_rate_limit(request.remote_addr)
    url = request.args.get('url', '')
    try:
        parsed = urlparse(url)
        if not url or parsed.netloc not in ALLOWED_DOMAINS:
            return 'Forbidden', 403

        # Strip cache-busting timestamp before using URL as cache key
        from urllib.parse import parse_qs, urlencode, urlunparse
        parsed_key = urlparse(url)
        qs = {k: v for k, v in parse_qs(parsed_key.query).items() if k != 't'}
        cache_key = urlunparse(parsed_key._replace(query=urlencode(qs, doseq=True)))

        # Check cache first
        now = time.time()
        with _img_cache_lock:
            cached = _img_cache.get(cache_key)
            ttl = IMG_CACHE_TTLS.get(parsed.netloc, IMG_CACHE_TTL_DEFAULT)
            if cached and (now - cached['ts'] < ttl):
                resp = Response(cached['content'], status=200, content_type=cached['ct'])
                resp.headers['Cache-Control'] = f'public, max-age={ttl}'
                resp.headers['Access-Control-Allow-Origin'] = '*'
                resp.headers['X-Cache'] = 'HIT'
                return resp

        # Not cached — fetch it
        r = requests.get(url, timeout=15, allow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/gif,image/png,image/jpeg,image/webp,image/*,*/*;q=0.8',
            'Referer': f'https://{parsed.netloc}/',
            'Cache-Control': 'no-cache',
        })
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if 'html' in ct:
            log(f"[imgproxy] Got HTML instead of image: {url}")
            return 'Not an image', 502

        # Store in cache (only if not already stored by a concurrent request)
        with _img_cache_lock:
            if cache_key not in _img_cache:
                _img_cache[cache_key] = {'content': r.content, 'ct': ct or 'image/gif', 'ts': time.time()}
            _prune_cache(_img_cache, IMG_CACHE_TTLS, IMG_CACHE_TTL_DEFAULT)

        log(f"[imgproxy] MISS {r.status_code} {ct[:40]} {len(r.content)}b: {parsed.path[:60]}")
        resp = Response(r.content, status=200, content_type=ct or 'image/gif')
        resp.headers['Cache-Control'] = f'public, max-age={ttl}'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['X-Cache'] = 'MISS'
        return resp
    except requests.exceptions.HTTPError as e:
        log(f"[imgproxy] HTTP {e.response.status_code} for {url}")
        return f'HTTP {e.response.status_code}', 502
    except Exception as e:
        log(f"[imgproxy] ERROR {type(e).__name__}: {e} for {url}")
        return 'Error', 502

@app.route('/dataproxy')
def data_proxy():
    _track_rate_limit(request.remote_addr)
    url = request.args.get('url', '')
    try:
        parsed = urlparse(url)
        if not url or parsed.netloc not in ALLOWED_DOMAINS:
            log(f"[dataproxy] BLOCKED domain: {parsed.netloc}")
            return 'Forbidden', 403

        # Check cache first
        now = time.time()
        with _data_cache_lock:
            cached = _data_cache.get(url)
            ttl = DATA_CACHE_TTLS.get(parsed.netloc, DATA_CACHE_TTL_DEFAULT)
            if cached and (now - cached['ts'] < ttl):
                resp = Response(cached['content'], status=200, content_type=cached['ct'])
                resp.headers['Cache-Control'] = f'public, max-age={ttl}'
                resp.headers['Access-Control-Allow-Origin'] = '*'
                resp.headers['X-Cache'] = 'HIT'
                return resp

        # Not cached — fetch it
        r = requests.get(url, timeout=20, allow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json,application/ld+json,text/html,text/plain,text/csv,application/xml,text/xml,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
        })
        r.raise_for_status()
        ct = r.headers.get('Content-Type', 'text/plain')

        # Store in cache (only if not already stored by a concurrent request)
        with _data_cache_lock:
            if url not in _data_cache:
                _data_cache[url] = {'content': r.content, 'ct': ct, 'ts': time.time()}
            _prune_cache(_data_cache, DATA_CACHE_TTLS, DATA_CACHE_TTL_DEFAULT)

        log(f"[dataproxy] MISS {r.status_code} {ct[:40]} {len(r.content)}b: {parsed.path[:60]}")
        resp = Response(r.content, status=200, content_type=ct)
        resp.headers['Cache-Control'] = f'public, max-age={ttl}'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['X-Cache'] = 'MISS'
        return resp
    except requests.exceptions.HTTPError as e:
        log(f"[dataproxy] HTTP {e.response.status_code} for {url}")
        return f'HTTP {e.response.status_code}', 502
    except Exception as e:
        log(f"[dataproxy] ERROR {type(e).__name__}: {e} for {url}")
        return 'Error', 502


# ── Settings store ─────────────────────────────────────────────────────────
_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "admin_settings.json")
_tag_overrides: Dict[str, Dict] = {}
_tag_overrides_lock = threading.Lock()
_user_override_enabled = True
_announcements: List[Dict] = []
_suppressed_alerts: Dict[str, str] = {}
_geo_filter: Dict[str, Any] = {"excluded_states": []}
_motd: Dict[str, Any] = {"message": "", "enabled": False, "style": "info"}
_webhook_config: Dict[str, Any] = {"url": "", "enabled": False, "min_severity": "Extreme"}
_injected_alerts: List[Dict] = []

def _load_admin_settings():
    global _tag_overrides, _user_override_enabled, _announcements, \
           _suppressed_alerts, _geo_filter, _motd, _webhook_config, _injected_alerts
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE, "r") as f:
                data = json.load(f)
            _tag_overrides = data.get("tag_overrides", {})
            _user_override_enabled = data.get("user_override_enabled", True)
            _announcements = data.get("announcements", [])
            _suppressed_alerts = data.get("suppressed_alerts", {})
            _geo_filter = data.get("geo_filter", {"excluded_states": []})
            _motd = data.get("motd", {"message": "", "enabled": False, "style": "info"})
            _webhook_config = data.get("webhook_config", {"url": "", "enabled": False, "min_severity": "Extreme"})
            _injected_alerts = data.get("injected_alerts", [])
            log(f"[admin] Loaded settings from {_SETTINGS_FILE}")
    except Exception as e:
        log(f"[admin] Failed to load settings: {e}")

def _save_admin_settings():
    try:
        with _tag_overrides_lock:
            data = {
                "tag_overrides": _tag_overrides,
                "user_override_enabled": _user_override_enabled,
                "announcements": _announcements,
                "suppressed_alerts": _suppressed_alerts,
                "geo_filter": _geo_filter,
                "motd": _motd,
                "webhook_config": _webhook_config,
                "injected_alerts": _injected_alerts,
            }
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"[admin] Failed to save settings: {e}")

_load_admin_settings()

# ── Page routes ────────────────────────────────

# ── Admin helpers ──────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        session.modified = True  # reset inactivity timer
        return f(*args, **kwargs)
    return decorated

# ── Admin routes ───────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
            session.permanent = True
            session["admin_logged_in"] = True
            log(f"[admin] Login successful from {request.remote_addr}")
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password."
            log(f"[admin] Failed login attempt from {request.remote_addr}")
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    with _state_lock:
        st = dict(fetch_status)
    with _img_cache_lock:
        img_count = len(_img_cache)
        img_urls  = list(_img_cache.keys())
    with _data_cache_lock:
        data_count = len(_data_cache)
        data_urls  = list(_data_cache.keys())
    with _log_lock:
        logs = list(_log_buffer)
    templates = sorted([f for f in os.listdir(app.template_folder) if f.endswith(".html")])
    try:
        with open(_SETTINGS_FILE, "r") as f:
            sdata = json.load(f)
    except Exception:
        sdata = {}
    return render_template("admin.html",
        status=st,
        img_count=img_count, img_urls=img_urls,
        data_count=data_count, data_urls=data_urls,
        logs=logs,
        templates=templates,
        suppressed=sdata.get("suppressed_alerts", {}),
        geo_filter=sdata.get("geo_filter", {"excluded_states": []}),
        motd=sdata.get("motd", {"message": "", "enabled": False, "style": "info"}),
        webhook=sdata.get("webhook_config", {"url": "", "enabled": False, "min_severity": "Extreme"}),
    )

@app.route("/admin/clear_cache", methods=["POST"])
@admin_required
def admin_clear_cache():
    target = request.form.get("target", "both")
    if target in ("img", "both"):
        with _img_cache_lock:
            _img_cache.clear()
        log("[admin] Image cache cleared")
    if target in ("data", "both"):
        with _data_cache_lock:
            _data_cache.clear()
        log("[admin] Data cache cleared")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/restart_threads", methods=["POST"])
@admin_required
def admin_restart_threads():
    global _threads_started
    log("[admin] Restarting background threads...")
    _threads_started = False
    start_background_threads()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/upload", methods=["POST"])
@admin_required
def admin_upload():
    files = request.files.getlist("files")
    results = []
    for f in files:
        if f and f.filename.endswith(".html"):
            import os
            path = os.path.join(app.template_folder, f.filename)
            f.save(path)
            log(f"[admin] Uploaded template: {f.filename}")
            results.append(f.filename)
    return jsonify({"uploaded": results})

@app.route("/admin/logs")
@admin_required
def admin_logs():
    with _log_lock:
        logs = list(_log_buffer)
    return jsonify({"logs": logs})

@app.route("/admin/status_json")
@admin_required
def admin_status_json():
    with _state_lock:
        st = dict(fetch_status)
    with _img_cache_lock:
        img_count = len(_img_cache)
    with _data_cache_lock:
        data_count = len(_data_cache)
    now_t = time.time()
    with _active_user_lock:
        active_users = sum(1 for t in _active_user_ts if now_t - t < 300)
    with _response_times_lock:
        rtimes = list(_response_times)
    avg_ms = round(sum(rtimes) / len(rtimes) * 1000) if rtimes else 0
    with _peaks_lock:
        peak_today = _peak_today
        peak_week  = _peak_week
    return jsonify({
        "status": st,
        "img_cache_count": img_count,
        "data_cache_count": data_count,
        "uptime_s": int(now_t - _server_start_time),
        "active_users": active_users,
        "avg_response_ms": avg_ms,
        "last_response_ms": round(rtimes[-1] * 1000) if rtimes else 0,
        "peak_today": peak_today,
        "peak_week": peak_week,
    })

@app.route("/admin/tag_overrides", methods=["GET","POST","DELETE"])
@admin_required
def admin_tag_overrides():
    global _tag_overrides
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {"tag_overrides": {}, "user_override_enabled": True, "announcements": []}

        if request.method == "POST":
            data = request.get_json(force=True)
            aid  = data.get("alert_id","").strip()
            tags = data.get("tags", [])
            if aid and isinstance(tags, list):
                if tags:
                    current["tag_overrides"][aid] = {"tags": tags, "by": "admin"}
                else:
                    current["tag_overrides"].pop(aid, None)
                _tag_overrides = current["tag_overrides"]
                with open(_SETTINGS_FILE, "w") as f:
                    json.dump(current, f)
                log(f"[admin] Tag override set for {aid}: {tags}")
            return jsonify({"ok": True})

        if request.method == "DELETE":
            data = request.get_json(force=True)
            aid = data.get("alert_id","").strip()
            current["tag_overrides"].pop(aid, None)
            _tag_overrides = current["tag_overrides"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Tag override cleared for {aid}")
            return jsonify({"ok": True})

        return jsonify({"overrides": current.get("tag_overrides", {}), "user_override_enabled": current.get("user_override_enabled", True)})

@app.route("/admin/announcements", methods=["GET", "POST", "DELETE"])
@admin_required
def admin_announcements():
    global _announcements
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {"tag_overrides": {}, "user_override_enabled": True, "announcements": []}

        if request.method == "POST":
            data = request.get_json(force=True)
            announcement = {
                "id": str(int(time.time() * 1000)),
                "type": data.get("type", "banner"),
                "message": data.get("message", "").strip(),
                "frequency": data.get("frequency", "once"),
                "start_date": data.get("start_date", ""),
                "end_date": data.get("end_date", ""),
                "enabled": True,
            }
            if not announcement["message"]:
                return jsonify({"ok": False, "error": "Message required"})
            current.setdefault("announcements", []).append(announcement)
            _announcements = current["announcements"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Announcement added: {announcement['message'][:50]}")
            return jsonify({"ok": True, "announcement": announcement})

        if request.method == "DELETE":
            data = request.get_json(force=True)
            aid = data.get("id", "")
            current["announcements"] = [a for a in current.get("announcements", []) if a.get("id") != aid]
            _announcements = current["announcements"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Announcement deleted: {aid}")
            return jsonify({"ok": True})

        return jsonify({"announcements": current.get("announcements", [])})

@app.route("/admin/announcements/toggle", methods=["POST"])
@admin_required
def admin_announcements_toggle():
    global _announcements
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {"tag_overrides": {}, "user_override_enabled": True, "announcements": []}
        data = request.get_json(force=True)
        aid = data.get("id", "")
        for a in current.get("announcements", []):
            if a.get("id") == aid:
                a["enabled"] = not a.get("enabled", True)
                break
        _announcements = current.get("announcements", [])
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(current, f)
    return jsonify({"ok": True})

@app.route("/admin/tag_settings", methods=["POST"])
@admin_required
def admin_tag_settings():
    global _user_override_enabled
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {"tag_overrides": {}, "user_override_enabled": True, "announcements": []}
        data = request.get_json(force=True)
        _user_override_enabled = bool(data.get("user_override_enabled", True))
        current["user_override_enabled"] = _user_override_enabled
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(current, f)
    log(f"[admin] User tag override {'enabled' if _user_override_enabled else 'disabled'}")
    return jsonify({"ok": True, "user_override_enabled": _user_override_enabled})

# ── Admin metrics endpoint ────────────────────────────────────────────────
@app.route("/admin/metrics")
@admin_required
def admin_metrics():
    now_t = time.time()
    uptime_s = int(now_t - _server_start_time)
    with _active_user_lock:
        active_users = sum(1 for t in _active_user_ts if now_t - t < 300)
    with _response_times_lock:
        rtimes = list(_response_times)
    avg_ms  = round(sum(rtimes) / len(rtimes) * 1000) if rtimes else 0
    last_ms = round(rtimes[-1] * 1000) if rtimes else 0
    with _peaks_lock:
        peak_today = _peak_today
        peak_week  = _peak_week
    with _state_lock:
        st = dict(fetch_status)
    with _img_cache_lock:
        img_count = len(_img_cache)
    with _data_cache_lock:
        data_count = len(_data_cache)
    with _rate_limit_lock:
        rl = {ip: sum(1 for t in dq if now_t - t < 60) for ip, dq in _rate_limit_data.items()}
    top_ips = sorted(rl.items(), key=lambda x: -x[1])[:20]
    return jsonify({
        "uptime_s": uptime_s,
        "active_users": active_users,
        "avg_response_ms": avg_ms,
        "last_response_ms": last_ms,
        "response_times": [round(t * 1000) for t in rtimes[-10:]],
        "peak_today": peak_today,
        "peak_week": peak_week,
        "status": st,
        "img_cache_count": img_count,
        "data_cache_count": data_count,
        "rate_limits": [{"ip": ip, "hits_60s": h} for ip, h in top_ips],
    })


# ── Alert suppression ─────────────────────────────────────────────────────
@app.route("/admin/suppress", methods=["GET", "POST", "DELETE"])
@admin_required
def admin_suppress():
    global _suppressed_alerts
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {}
        if request.method == "POST":
            data = request.get_json(force=True)
            aid    = data.get("alert_id", "").strip()
            reason = data.get("reason", "").strip()
            if aid:
                current.setdefault("suppressed_alerts", {})[aid] = reason or "admin"
                _suppressed_alerts = current["suppressed_alerts"]
                with open(_SETTINGS_FILE, "w") as f:
                    json.dump(current, f)
                log(f"[admin] Suppressed alert: {aid}")
            return jsonify({"ok": True})
        if request.method == "DELETE":
            data = request.get_json(force=True)
            aid = data.get("alert_id", "").strip()
            current.setdefault("suppressed_alerts", {}).pop(aid, None)
            _suppressed_alerts = current.get("suppressed_alerts", {})
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Unsuppressed alert: {aid}")
            return jsonify({"ok": True})
        return jsonify({"suppressed": current.get("suppressed_alerts", {})})


# ── Geo filter ────────────────────────────────────────────────────────────
@app.route("/admin/geo_filter", methods=["GET", "POST"])
@admin_required
def admin_geo_filter():
    global _geo_filter
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {}
        if request.method == "POST":
            data   = request.get_json(force=True)
            states = data.get("excluded_states", [])
            if isinstance(states, list):
                current["geo_filter"] = {"excluded_states": [s.upper()[:2] for s in states if isinstance(s, str)]}
                _geo_filter = current["geo_filter"]
                with open(_SETTINGS_FILE, "w") as f:
                    json.dump(current, f)
                log(f"[admin] Geo filter updated: {current['geo_filter']['excluded_states']}")
            return jsonify({"ok": True, "geo_filter": current.get("geo_filter", {})})
        return jsonify({"geo_filter": current.get("geo_filter", {"excluded_states": []})})


# ── Custom alert injector ─────────────────────────────────────────────────
@app.route("/admin/inject_alert", methods=["GET", "POST", "DELETE", "PATCH"])
@admin_required
def admin_inject_alert():
    global _injected_alerts
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {}
        if request.method == "POST":
            data     = request.get_json(force=True)
            alert_id = f"injected-{int(time.time() * 1000)}"
            now_iso  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
            injected = {
                "id": alert_id,
                "event": data.get("event", "Test Alert"),
                "severity": data.get("severity", "Moderate"),
                "urgency": data.get("urgency", "Expected"),
                "certainty": data.get("certainty", "Likely"),
                "area": data.get("area", "Test Area"),
                "headline": data.get("headline", ""),
                "description": data.get("description", ""),
                "instruction": data.get("instruction", ""),
                "sender": "Admin Injected",
                "sent": now_iso, "updated": now_iso, "effective": now_iso, "onset": now_iso,
                "expires": data.get("expires", ""),
                "geometry": None, "centroid": None, "zone_ids": [],
                "enabled": True, "_injected": True,
            }
            current.setdefault("injected_alerts", []).append(injected)
            _injected_alerts = current["injected_alerts"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Injected alert: {injected['event']} \u2014 {alert_id}")
            return jsonify({"ok": True, "alert": injected})
        if request.method == "DELETE":
            data = request.get_json(force=True)
            aid  = data.get("alert_id", "")
            current["injected_alerts"] = [a for a in current.get("injected_alerts", []) if a.get("id") != aid]
            _injected_alerts = current["injected_alerts"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Removed injected alert: {aid}")
            return jsonify({"ok": True})
        if request.method == "PATCH":
            data = request.get_json(force=True)
            aid  = data.get("alert_id", "")
            for a in current.get("injected_alerts", []):
                if a.get("id") == aid:
                    a["enabled"] = not a.get("enabled", True)
                    break
            _injected_alerts = current.get("injected_alerts", [])
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            return jsonify({"ok": True})
        return jsonify({"injected_alerts": current.get("injected_alerts", [])})


# ── MOTD ──────────────────────────────────────────────────────────────────
@app.route("/admin/motd", methods=["GET", "POST"])
@admin_required
def admin_motd():
    global _motd
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {}
        if request.method == "POST":
            data = request.get_json(force=True)
            current["motd"] = {
                "message": data.get("message", "").strip(),
                "enabled": bool(data.get("enabled", False)),
                "style": data.get("style", "info"),
            }
            _motd = current["motd"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] MOTD {'enabled' if _motd['enabled'] else 'disabled'}: {_motd['message'][:50]}")
            return jsonify({"ok": True, "motd": _motd})
        return jsonify({"motd": current.get("motd", {"message": "", "enabled": False, "style": "info"})})


# ── Static file manager ───────────────────────────────────────────────────
@app.route("/admin/static_files", methods=["GET", "DELETE", "POST"])
@admin_required
def admin_static_files():
    static_dir = app.static_folder
    if request.method == "POST":
        uploaded = []
        for f in request.files.getlist("files"):
            if f and f.filename:
                safe_name = os.path.basename(f.filename)
                path = os.path.join(static_dir, safe_name)
                f.save(path)
                log(f"[admin] Static file uploaded: {safe_name}")
                uploaded.append(safe_name)
        return jsonify({"ok": True, "uploaded": uploaded})
    if request.method == "DELETE":
        data  = request.get_json(force=True)
        fname = os.path.basename(data.get("filename", ""))
        if fname:
            path = os.path.join(static_dir, fname)
            if os.path.exists(path):
                os.remove(path)
                log(f"[admin] Static file deleted: {fname}")
                return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "File not found"})
    try:
        files = []
        for fname in sorted(os.listdir(static_dir)):
            fpath = os.path.join(static_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                files.append({"name": fname, "size": stat.st_size, "mtime": int(stat.st_mtime)})
    except Exception:
        files = []
    return jsonify({"files": files})


# ── Webhook config ────────────────────────────────────────────────────────
@app.route("/admin/webhook", methods=["GET", "POST"])
@admin_required
def admin_webhook():
    global _webhook_config
    with _tag_overrides_lock:
        try:
            with open(_SETTINGS_FILE, "r") as f:
                current = json.load(f)
        except Exception:
            current = {}
        if request.method == "POST":
            data = request.get_json(force=True)
            current["webhook_config"] = {
                "url": data.get("url", "").strip(),
                "enabled": bool(data.get("enabled", False)),
                "min_severity": data.get("min_severity", "Extreme"),
            }
            _webhook_config = current["webhook_config"]
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(current, f)
            log(f"[admin] Webhook {'enabled' if _webhook_config['enabled'] else 'disabled'}")
            return jsonify({"ok": True})
        return jsonify({"webhook_config": current.get("webhook_config", {"url": "", "enabled": False, "min_severity": "Extreme"})})


# ── Rate limit monitor ────────────────────────────────────────────────────
@app.route("/admin/rate_limits")
@admin_required
def admin_rate_limits():
    now_t = time.time()
    with _rate_limit_lock:
        data = {ip: {
            "hits_60s": sum(1 for t in dq if now_t - t <   60),
            "hits_5m":  sum(1 for t in dq if now_t - t <  300),
            "hits_1h":  sum(1 for t in dq if now_t - t < 3600),
        } for ip, dq in _rate_limit_data.items()}
    sorted_ips = sorted(data.items(), key=lambda x: -x[1]["hits_60s"])[:30]
    return jsonify({"rate_limits": [{"ip": ip, **v} for ip, v in sorted_ips]})


# ── Export data ───────────────────────────────────────────────────────────
@app.route("/admin/export")
@admin_required
def admin_export():
    import csv, io as _io
    fmt = request.args.get("format", "json")
    with _state_lock:
        alerts = list(active_alerts_all)
    if fmt == "csv":
        out    = _io.StringIO()
        fields = ["id", "event", "severity", "urgency", "certainty", "area", "headline", "sent", "expires", "sender"]
        w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(alerts)
        resp = Response(out.getvalue(), content_type="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="alerts_{int(time.time())}.csv"'
        return resp
    resp = Response(
        json.dumps({"alerts": alerts, "exported_at": datetime.utcnow().isoformat()}, indent=2),
        content_type="application/json",
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="alerts_{int(time.time())}.json"'
    return resp


@app.route("/stats")
def stats():
    return render_template("stats.html")

@app.route("/swcc")
def swcc():
    return render_template("swcc.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/dsa")
def dsa():
    return render_template("dsa.html")

@app.route("/download")
def download():
    return render_template("download.html")

@app.route("/changelog")
def changelog():
    return render_template("changelog.html")

@app.route("/")
def dashboard():
    return render_template("index.html", default_events=DEFAULT_SELECTED_EVENTS)

@app.route("/sitemap.xml")
def sitemap():
    return app.send_static_file("sitemap.xml")

@app.route("/robots.txt")
def robots():
    return app.send_static_file("robots.txt")

@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


# Start background threads immediately on startup
start_background_threads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
