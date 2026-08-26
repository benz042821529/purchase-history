#!/usr/bin/env python3
import os, json, time, threading, smtplib
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

API_KEY     = os.environ.get("API_KEY", "")
UNIVERSE_ID = os.environ.get("UNIVERSE_ID", "")
PASSWORD    = os.environ.get("PASSWORD", "admin")
PORT        = int(os.environ.get("PORT", 8080))

# --- Email digest (สรุปยอดขายรายวัน) ---
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "s5703052412021@gmail.com")
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", 587))
DIGEST_HOUR       = int(os.environ.get("DIGEST_HOUR", 7))  # ส่งทุกวันตามเวลาเครื่อง server

BASE    = f"https://apis.roblox.com/datastores/v1/universes/{UNIVERSE_ID}"
DS_NAME = "PurchaseLog_v1"

# --- simple in-memory TTL cache ---
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key, ttl):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < ttl:
            return entry[1]
    return None

def cache_set(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)

def roblox_get(path, params=None):
    url = path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def resolve_username(username):
    try:
        body = json.dumps({"usernames": [username], "excludeBannedUsers": False}).encode()
        req  = urllib.request.Request(
            "https://users.roblox.com/v1/usernames/users",
            data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
            if data.get("data"):
                return data["data"][0]["id"], data["data"][0]["name"]
    except Exception as e:
        print(f"[resolve_username {username}] {e}")
    return None, None

def get_display_name(uid):
    try:
        req = urllib.request.Request(f"https://users.roblox.com/v1/users/{uid}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("name", str(uid))
    except Exception as e:
        print(f"[get_display_name {uid}] {e}")
        return str(uid)

def fetch_history(uid, from_ts=None, to_ts=None):
    try:
        data = roblox_get(
            f"{BASE}/standard-datastores/datastore/entries/entry",
            {"datastoreName": DS_NAME, "entryKey": f"P_{uid}"}
        )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[fetch_history {uid}] HTTP {e.code}")
        return [] if e.code == 404 else None
    except Exception as e:
        print(f"[fetch_history {uid}] {e}")
        return None
    if not isinstance(data, list): return []
    result = []
    for e in data:
        ts = e.get("ts", 0)
        if from_ts and ts < from_ts: continue
        if to_ts   and ts > to_ts:   continue
        result.append(e)
    result.sort(key=lambda x: x["ts"], reverse=True)
    return result

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

class RateLimited(Exception):
    """โดน 429 — พลาดชั่วคราว ควรลองใหม่ (ไม่ใช่ข้อมูลไม่มีจริง)"""
    pass

_catalog_csrf = ""
_catalog_csrf_lock = threading.Lock()

def catalog_item_detail(iid, tp):
    global _catalog_csrf
    item_type = "Bundle" if tp == "B" else "Asset"
    body = json.dumps({"items": [{"itemType": item_type, "id": iid}]}).encode()
    url  = "https://catalog.roblox.com/v1/catalog/items/details"
    rate_limited = False
    for attempt in range(2):
        headers = {"Content-Type": "application/json", "User-Agent": UA}
        with _catalog_csrf_lock:
            if _catalog_csrf:
                headers["X-CSRF-Token"] = _catalog_csrf
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.loads(r.read().decode()).get("data", [])
                return items[0] if items else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                rate_limited = True
            token = e.headers.get("x-csrf-token")
            if token:
                with _catalog_csrf_lock:
                    _catalog_csrf = token
            if attempt == 0 and (token or e.code == 429):
                continue
        except Exception as e:
            print(f"[catalog {tp}_{iid}] {e}")
            break
    if rate_limited:
        raise RateLimited()
    return None

def roblox_public(url, data=None, method="GET"):
    headers = {"User-Agent": UA}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited()
        raise

def _thumb_batch(ids, kind):
    """ดึง thumbnail เป็นชุด → {id: url} เฉพาะที่ state=Completed. kind = 'asset' | 'bundle'"""
    out = {}
    if kind == "asset":
        base = "https://thumbnails.roblox.com/v1/assets?assetIds={}&size=150x150&format=Png"
    else:
        base = "https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={}&size=150x150&format=Png"
    for i in range(0, len(ids), 100):
        chunk = ",".join(str(x) for x in ids[i:i+100])
        try:
            d = roblox_public(base.format(chunk))
            for x in d.get("data", []):
                if x.get("state") == "Completed" and x.get("imageUrl"):
                    out[x["targetId"]] = x["imageUrl"]
        except Exception as e:
            print(f"[thumb {kind}] {e}")
    return out


def _asset_details(iid):
    """ดึงแบบ Asset (economy v2 → fallback catalog) → (name, creator, price). อาจ raise RateLimited"""
    name, creator, price = "", "", 0
    # v2 รองรับทั้ง asset ปกติและ Collectible (UGC limited) — v1 ดึง collectible ไม่ได้
    d = roblox_public(f"https://economy.roblox.com/v2/assets/{iid}/details")
    if "errors" not in d:
        name    = d.get("Name", "")
        creator = d.get("Creator", {}).get("Name", "")
        price   = d.get("PriceInRobux") or 0
    if not name or not creator or not price:
        ci = catalog_item_detail(iid, "A")
        if ci:
            name    = name    or ci.get("name", "")
            creator = creator or ci.get("creatorName", "")
            price   = price   or (ci.get("price") or 0)
    return name, creator, price


def _bundle_details(iid):
    """ดึงแบบ Bundle (catalog) → (name, creator, price). อาจ raise RateLimited"""
    ci = catalog_item_detail(iid, "B")
    if ci:
        return ci.get("name", ""), ci.get("creatorName", ""), ci.get("price") or 0
    return "", "", 0


def fetch_item_details(items):
    result = {}
    unique = list({f"{e['tp']}_{e['id']}": e for e in items}.values())
    assets  = [e["id"] for e in unique if e["tp"] != "B"]
    bundles = [e["id"] for e in unique if e["tp"] == "B"]
    all_ids = [e["id"] for e in unique]

    # Thumbnails — ลองตามชนิดที่ log ไว้ก่อน แล้ว cross-type สำหรับตัวที่ยังไม่ได้ (log ผิดชนิด)
    thumb_map = {}
    thumb_map.update(_thumb_batch(assets, "asset"))
    thumb_map.update(_thumb_batch(bundles, "bundle"))
    missing = [i for i in all_ids if i not in thumb_map]
    if missing:
        thumb_map.update(_thumb_batch(missing, "bundle"))
        missing = [i for i in all_ids if i not in thumb_map]
    if missing:
        thumb_map.update(_thumb_batch(missing, "asset"))

    # Creator names + price + item name (ลองทั้ง Asset และ Bundle ไม่ว่า log จะบอกชนิดไหน)
    # คืน (key, data, transient):
    #   data = {"name","creator","price"} เมื่อได้ข้อมูล
    #   data = None + transient=True  → โดน 429 พลาดชั่วคราว ควรลองใหม่
    #   data = None + transient=False → ไม่มีข้อมูลจริง (ถูกลบ) เลิกลอง
    def get_details(item):
        iid, tp = item["id"], item["tp"]
        key = f"{tp}_{iid}"
        # ลองชนิดที่ log ไว้ก่อน ถ้าไม่เจอค่อยลองอีกชนิด (กัน tp ที่ log มาผิด)
        order = ["B", "A"] if tp == "B" else ["A", "B"]
        name = creator = ""
        price = 0
        transient = False
        for kind in order:
            try:
                n, c, p = _asset_details(iid) if kind == "A" else _bundle_details(iid)
            except RateLimited:
                transient = True
                continue
            except Exception as e:
                print(f"[details {kind}_{iid}] {e}")
                continue
            name    = name    or n
            creator = creator or c
            price   = price   or p
            if name:   # เจอชนิดที่ถูกแล้ว (มีชื่อ) ไม่ต้องลองอีกชนิด
                break
        if name or creator or price:
            return key, {"name": name, "creator": creator, "price": price}, False
        return key, None, transient

    # ให้ทุก key มีที่อยู่ก่อน + ใส่ thumbnail + ดึงจาก cache รายชิ้น (เก็บเฉพาะที่เคยสำเร็จ)
    pending = {}
    for it in unique:
        key = f"{it['tp']}_{it['id']}"
        result[key] = {"thumb": thumb_map.get(it["id"], ""), "name": "", "creator": "", "price": 0}
        cached = cache_get(("item", key), 3600)
        if cached is not None:
            if cached["name"]:  result[key]["name"] = cached["name"]
            result[key]["creator"] = cached["creator"]
            if cached["price"]: result[key]["price"] = cached["price"]
        else:
            pending[key] = it

    # retry จนครบ: ลองซ้ำเฉพาะอันที่โดน 429 พร้อม backoff, หยุดเมื่อครบหรือครบ 6 รอบ
    MAX_ROUNDS = 6
    for rnd in range(MAX_ROUNDS):
        if not pending:
            break
        retry_next = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for key, data, transient in ex.map(get_details, list(pending.values())):
                if data is not None:
                    if data["name"]:
                        result[key]["name"] = data["name"]
                    result[key]["creator"] = data["creator"]
                    if data["price"]:
                        result[key]["price"] = data["price"]
                    cache_set(("item", key), data)
                elif transient:
                    retry_next[key] = pending[key]
                # ไม่มีข้อมูลจริง → ปล่อยไว้ ไม่ลองซ้ำ
        pending = retry_next
        if pending and rnd < MAX_ROUNDS - 1:
            wait = min(2 ** rnd, 8)
            print(f"[item-details] เหลือ {len(pending)} ชิ้นโดน rate limit — รอ {wait}s แล้วลองใหม่ (รอบ {rnd+2})")
            time.sleep(wait)

    if pending:
        print(f"[item-details] ยังเหลือ {len(pending)} ชิ้นดึงไม่ครบหลัง {MAX_ROUNDS} รอบ")

    return result


def list_all_keys():
    keys = []
    cursor = None
    while True:
        params = {"datastoreName": DS_NAME, "limit": 100, "prefix": "P_"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = roblox_get(f"{BASE}/standard-datastores/datastore/entries", params)
            keys.extend([k["key"] for k in data.get("keys", [])])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        except Exception as e:
            print(f"[list_keys] {e}")
            break
    return keys


def batch_usernames(uids):
    result = {}
    for i in range(0, len(uids), 100):
        batch = uids[i:i+100]
        try:
            body = json.dumps({"userIds": batch, "excludeBannedUsers": False}).encode()
            req  = urllib.request.Request(
                "https://users.roblox.com/v1/users",
                data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
                for u in data.get("data", []):
                    result[u["id"]] = u["name"]
        except Exception as e:
            print(f"[batch_usernames] {e}")
    return result


def fetch_all_history(from_ts=None, to_ts=None):
    ck = ("all", from_ts, to_ts)
    cached = cache_get(ck, 300)
    if cached is not None:
        return cached

    keys = list_all_keys()

    def fetch_for_key(key):
        if not key.startswith("P_"):
            return []
        try:
            uid = int(key[2:])
        except:
            return []
        entries = fetch_history(uid, from_ts, to_ts)
        if not entries:
            return []
        for e in entries:
            e["uid"] = uid
        return entries

    all_entries = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for result in ex.map(fetch_for_key, keys):
            if result:
                all_entries.extend(result)

    uids = list({e["uid"] for e in all_entries})
    uid_names = batch_usernames(uids)
    for e in all_entries:
        e["username"] = uid_names.get(e["uid"], str(e["uid"]))

    all_entries.sort(key=lambda x: x["ts"], reverse=True)
    cache_set(ck, all_entries)
    return all_entries


def send_email(subject, html_body):
    if not (SMTP_USER and SMTP_APP_PASSWORD and NOTIFY_EMAIL):
        raise RuntimeError("ไม่ได้ตั้งค่า SMTP_USER / SMTP_APP_PASSWORD / NOTIFY_EMAIL")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_APP_PASSWORD)
        s.sendmail(SMTP_USER, [NOTIFY_EMAIL], msg.as_string())


def build_digest_html(date_str, entries, item_map):
    total   = len(entries)
    revenue = sum(e.get("p", 0) for e in entries)
    rows = ""
    for e in sorted(entries, key=lambda x: x.get("p", 0), reverse=True):
        key  = f"{e['tp']}_{e['id']}"
        name = item_map.get(key, {}).get("name") or f"ID:{e['id']}"
        rows += (
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee">{e.get("username","")}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{name}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">R$ {e.get("p",0):,}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="3" style="padding:14px;text-align:center;color:#999">ไม่มีการซื้อในวันนี้</td></tr>'
    return f"""<html><body style="font-family:'Segoe UI',sans-serif;color:#1a1a2e">
<h2 style="margin-bottom:4px">สรุปยอดขาย {date_str}</h2>
<p style="color:#666;margin-top:0">รายการทั้งหมด: <b>{total}</b> &nbsp;|&nbsp; รายได้รวม: <b>R$ {revenue:,}</b></p>
<table style="border-collapse:collapse;width:100%;max-width:640px">
<tr style="background:#f7f8fc"><th style="padding:6px 10px;text-align:left">ผู้เล่น</th><th style="padding:6px 10px;text-align:left">สินค้า</th><th style="padding:6px 10px;text-align:right">ราคา</th></tr>
{rows}
</table>
</body></html>"""


def run_daily_digest():
    y = datetime.now() - timedelta(days=1)
    start = datetime(y.year, y.month, y.day, 0, 0, 0)
    end   = datetime(y.year, y.month, y.day, 23, 59, 59)
    date_str = start.strftime("%Y-%m-%d")
    try:
        entries  = fetch_all_history(start.timestamp(), end.timestamp())
        item_map = fetch_item_details(entries) if entries else {}
        html     = build_digest_html(date_str, entries, item_map)
        send_email(f"[Purchase History] สรุปยอดขาย {date_str} — {len(entries)} รายการ", html)
        print(f"[digest] ส่งอีเมลสรุปยอด {date_str} สำเร็จ ({len(entries)} รายการ)")
    except Exception as e:
        print(f"[digest] ส่งอีเมลล้มเหลว: {e}")


def digest_scheduler():
    while True:
        now    = datetime.now()
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        run_daily_digest()


HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Purchase History</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f0f2f7;color:#1a1a2e;font-family:'Segoe UI',sans-serif;padding:28px 16px}
h1{color:#1a1a2e;font-size:22px;font-weight:700;margin-bottom:4px}
.sub{color:#888;font-size:12px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e4e6ef;border-radius:14px;padding:20px;max-width:780px;margin:0 auto 14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card-title{font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;font-weight:600}
.search-row{display:flex;gap:8px}
.search-row input{flex:1;padding:10px 14px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:14px;outline:none}
.search-row input:focus{border-color:#4f8ef7;background:#fff}
.search-row input::placeholder{color:#bbb}
.btn{padding:10px 24px;background:#4f8ef7;border:none;border-radius:9px;color:#fff;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#3a7de8}.btn:disabled{background:#ccc;cursor:default}
.date-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:14px}
.fg{display:flex;flex-direction:column;gap:4px}
.fg label{font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
input[type=date]{padding:8px 10px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:13px;outline:none}
input[type=date]:focus{border-color:#4f8ef7}
.qrow{display:flex;gap:6px;align-items:flex-end}
.qbtn{padding:8px 14px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#888;font-size:12px;cursor:pointer;font-weight:600}
.qbtn:hover{border-color:#4f8ef7;color:#4f8ef7}
.qbtn.active{background:#4f8ef7;color:#fff;border-color:#4f8ef7}
.player-info{max-width:780px;margin:0 auto 8px;font-size:13px;color:#4f8ef7;font-weight:600;min-height:18px}
.status{max-width:780px;margin:0 auto 10px;font-size:13px;color:#aaa;min-height:16px}
.status.err{color:#ef4444}.status.ok{color:#22c55e}
.stats{max-width:780px;margin:0 auto 14px;display:flex;gap:10px}
.stat{flex:1;background:#fff;border:1px solid #e4e6ef;border-radius:12px;padding:14px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.stat .val{font-size:22px;font-weight:700;color:#4f8ef7}
.stat .lbl{font-size:11px;color:#aaa;margin-top:3px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.tbl-wrap{max-width:780px;margin:0 auto;overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0 5px;font-size:13px}
thead th{padding:6px 14px;color:#bbb;font-size:11px;text-transform:uppercase;letter-spacing:.5px;text-align:left;font-weight:600}
tbody tr{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.05)}
tbody tr:hover{box-shadow:0 2px 8px rgba(79,142,247,.15)}
td{padding:11px 14px;border-top:1px solid #f0f2f7;border-bottom:1px solid #f0f2f7}
td:first-child{border-left:1px solid #f0f2f7;border-radius:10px 0 0 10px}
td:last-child{border-right:1px solid #f0f2f7;border-radius:0 10px 10px 0}
.thumb{width:54px;height:54px;border-radius:8px;object-fit:cover;background:#f0f2f7;display:block}
.item-name{color:#1a1a2e;font-weight:600}
.price{color:#f59e0b;font-weight:700}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700}
.ba{background:#eff6ff;color:#3b82f6}.bb{background:#f5f3ff;color:#7c3aed}
.date-cell{color:#1a1a2e;font-size:13px;font-weight:500}
.creator{color:#888;font-size:12px}
.time-cell{color:#aaa;font-size:12px;margin-top:2px}
.player-col{color:#4f8ef7;font-size:12px;font-weight:600}

/* Login overlay */
#loginOverlay{position:fixed;inset:0;background:#f0f2f7;display:flex;align-items:center;justify-content:center;z-index:999}
.login-card{background:#fff;border:1px solid #e4e6ef;border-radius:16px;padding:32px;width:320px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center}
.login-card h2{font-size:18px;color:#1a1a2e;margin-bottom:6px}
.login-card p{font-size:12px;color:#aaa;margin-bottom:20px}
.login-card input{width:100%;padding:10px 14px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:14px;outline:none;margin-bottom:12px;text-align:center;letter-spacing:2px}
.login-card input:focus{border-color:#4f8ef7}
.login-btn{width:100%;padding:11px;background:#4f8ef7;border:none;border-radius:9px;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
.login-btn:hover{background:#3a7de8}
.login-err{color:#ef4444;font-size:12px;margin-top:8px;min-height:16px}
</style>
</head>
<body>

<!-- Login -->
<div id="loginOverlay">
  <div class="login-card">
    <h2>Purchase History</h2>
    <p>กรอกรหัสผ่านเพื่อเข้าใช้งาน</p>
    <input type="password" id="pwInput" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
    <button class="login-btn" onclick="doLogin()">เข้าสู่ระบบ</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>

<div style="max-width:780px;margin:0 auto 18px">
  <h1>Purchase History</h1>
  <div class="sub">ค้นหาประวัติการซื้อรายผู้เล่น</div>
</div>

<div class="card">
  <div class="card-title">ค้นหาผู้เล่น</div>
  <div class="search-row">
    <input id="query" placeholder="ชื่อ Player หรือ User ID..." onkeydown="if(event.key==='Enter')search()">
    <button class="btn" id="searchBtn" onclick="search()">ค้นหา</button>
  </div>
  <div class="date-row">
    <div class="fg"><label>จากวันที่</label><input type="date" id="fromDate"></div>
    <div class="fg"><label>ถึงวันที่</label><input type="date" id="toDate"></div>
    <div class="qrow">
      <button class="qbtn" onclick="quickFilter(1)">วันนี้</button>
      <button class="qbtn" onclick="quickFilter(7)">7 วัน</button>
      <button class="qbtn" onclick="quickFilter(30)">30 วัน</button>
      <button class="qbtn active" onclick="quickFilter(0)">ทั้งหมด</button>
    </div>
    <div class="qrow">
      <button class="qbtn" id="sortHighBtn" onclick="sortByPrice('desc')">ราคา: สูง → ต่ำ</button>
      <button class="qbtn" id="sortLowBtn" onclick="sortByPrice('asc')">ราคา: ต่ำ → สูง</button>
    </div>
  </div>
</div>

<div class="player-info" id="playerInfo"></div>
<div class="status" id="status"></div>
<div class="stats" id="statsRow" style="display:none">
  <div class="stat"><div class="val" id="sTotal">0</div><div class="lbl">รายการ</div></div>
  <div class="stat"><div class="val" id="sRevenue">0</div><div class="lbl">Robux รวม</div></div>
</div>
<div class="tbl-wrap">
  <table id="tbl" style="display:none">
    <thead><tr><th style="width:66px"></th><th>ผู้เล่น</th><th>สินค้า</th><th>ผู้สร้าง</th><th>ราคา</th><th>ประเภท</th><th>วันที่</th><th>เวลา</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<script>
let _pw = ''
let _items = []
let _itemMap = {}
let _defaultUsername = ''
let _sortMode = null
function pad(n){return String(n).padStart(2,'0')}
function fmtParts(ts){
  const d=new Date(ts*1000)
  return {
    date:`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`,
    time:`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  }
}
function toISO(ts){const d=new Date(ts*1000);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`}
function dateToTs(s,end=false){
  if(!s)return ''
  const[y,m,d]=s.split('-').map(Number)
  return Math.floor(new Date(y,m-1,d,end?23:0,end?59:0,end?59:0).getTime()/1000)
}

async function doLogin(){
  const pw=document.getElementById('pwInput').value
  const res=await fetch('/api/auth',{headers:{'X-Password':pw}})
  if(res.ok){_pw=pw;document.getElementById('loginOverlay').style.display='none';loadAll()}
  else{document.getElementById('loginErr').textContent='รหัสผ่านไม่ถูกต้อง'}
}

function quickFilter(days){
  document.querySelectorAll('.qbtn').forEach(b=>b.classList.remove('active'))
  event.target.classList.add('active')
  if(!days){document.getElementById('fromDate').value='';document.getElementById('toDate').value='';return}
  const now=new Date(),from=new Date(now)
  from.setDate(now.getDate()-days+1);from.setHours(0,0,0,0)
  document.getElementById('fromDate').value=toISO(Math.floor(from.getTime()/1000))
  document.getElementById('toDate').value=toISO(Math.floor(now.getTime()/1000))
}

function resetSortUI(){
  _sortMode=null
  document.getElementById('sortHighBtn').classList.remove('active')
  document.getElementById('sortLowBtn').classList.remove('active')
}

function sortByPrice(mode){
  const btn=document.getElementById(mode==='desc'?'sortHighBtn':'sortLowBtn')
  if(_sortMode===mode){
    resetSortUI()   // กดปุ่มเดิมซ้ำ = ยกเลิกการเรียง กลับไปเรียงตามวันที่เดิม
  }else{
    _sortMode=mode
    document.getElementById('sortHighBtn').classList.remove('active')
    document.getElementById('sortLowBtn').classList.remove('active')
    btn.classList.add('active')
  }
  renderSorted()
}

function renderSorted(){
  let items=_items
  if(_sortMode){
    items=[..._items].sort((a,b)=>_sortMode==='desc' ? (b.p||0)-(a.p||0) : (a.p||0)-(b.p||0))
  }
  document.getElementById('tbody').innerHTML=renderRows(items,_defaultUsername)
  applyDetails(items,_itemMap)
}

function renderRows(items, defaultUsername){
  return items.map(e=>{
    const isB=e.tp==='B',{date,time}=fmtParts(e.ts)
    const player=e.username||defaultUsername||''
    return `<tr><td><img class="thumb" data-id="${e.id}" data-tp="${e.tp}" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></td><td class="player-col">${player}</td><td class="item-name" data-id="${e.id}" data-tp="${e.tp}">${e.n||'ID:'+e.id}</td><td><span class="creator" data-id="${e.id}" data-tp="${e.tp}">${e.cr||'...'}</span></td><td class="price" data-id="${e.id}" data-tp="${e.tp}">${e.p?'R$ '+e.p:'ฟรี'}</td><td><span class="badge ${isB?'bb':'ba'}">${isB?'Bundle':'Asset'}</span></td><td><div class="date-cell">${date}</div></td><td><div class="time-cell">${time}</div></td></tr>`
  }).join('')
}

async function loadAll(){
  const status=document.getElementById('status')
  status.className='status';status.textContent='กำลังโหลดรายการทั้งหมด...'
  document.getElementById('playerInfo').textContent=''
  document.getElementById('statsRow').style.display='none'
  document.getElementById('tbl').style.display='none'
  document.getElementById('tbody').innerHTML=''
  const from=dateToTs(document.getElementById('fromDate').value,false)
  const to=dateToTs(document.getElementById('toDate').value,true)
  const params=new URLSearchParams({from:from||'',to:to||''})
  try{
    const res=await fetch('/api/all-history?'+params,{headers:{'X-Password':_pw}})
    const data=await res.json()
    if(!res.ok){status.className='status err';status.textContent='Error: '+(data.message||res.status);return}
    const items=data.entries||[]
    if(!items.length){status.className='status';status.textContent='ไม่มีประวัติการซื้อ';return}
    status.className='status ok';status.textContent=`ทั้งหมด ${items.length} รายการจากทุกผู้เล่น`
    document.getElementById('sTotal').textContent=items.length.toLocaleString()
    document.getElementById('sRevenue').textContent='R$ '+items.reduce((s,e)=>s+(e.p||0),0).toLocaleString()
    document.getElementById('statsRow').style.display='flex'
    _items=items;_defaultUsername='';resetSortUI()
    document.getElementById('tbody').innerHTML=renderRows(items,'')
    document.getElementById('tbl').style.display='table'
    loadThumbnails(items)
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
}

async function search(){
  const q=document.getElementById('query').value.trim()
  if(!q){loadAll();return}
  const btn=document.getElementById('searchBtn')
  const status=document.getElementById('status')
  btn.disabled=true;status.className='status';status.textContent='กำลังค้นหา...'
  document.getElementById('playerInfo').textContent=''
  document.getElementById('statsRow').style.display='none'
  document.getElementById('tbl').style.display='none'
  document.getElementById('tbody').innerHTML=''

  const from=dateToTs(document.getElementById('fromDate').value,false)
  const to=dateToTs(document.getElementById('toDate').value,true)
  const params=new URLSearchParams({q,from:from||'',to:to||''})

  try{
    const res=await fetch('/api/history?'+params,{headers:{'X-Password':_pw}})
    const data=await res.json()
    if(!res.ok){status.className='status err';status.textContent='Error: '+(data.message||res.status);return}
    document.getElementById('playerInfo').textContent=`ผู้เล่น: ${data.username}  (ID: ${data.userId})`
    const items=data.entries||[]
    if(!items.length){status.className='status';status.textContent='ไม่พบประวัติการซื้อ';return}
    status.className='status ok';status.textContent=`พบ ${items.length} รายการ`
    document.getElementById('sTotal').textContent=items.length.toLocaleString()
    document.getElementById('sRevenue').textContent='R$ '+items.reduce((s,e)=>s+(e.p||0),0).toLocaleString()
    document.getElementById('statsRow').style.display='flex'
    _items=items;_defaultUsername=data.username;resetSortUI()
    document.getElementById('tbody').innerHTML=renderRows(items,data.username)
    document.getElementById('tbl').style.display='table'
    loadThumbnails(items)
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
  finally{btn.disabled=false}
}

function applyDetails(items, map){
  document.querySelectorAll('img.thumb').forEach(img=>{
    const key=`${img.dataset.tp}_${img.dataset.id}`
    if(map[key]?.thumb) img.src=map[key].thumb
  })
  document.querySelectorAll('td.item-name[data-id]').forEach(el=>{
    const key=`${el.dataset.tp}_${el.dataset.id}`
    const n=map[key]?.name
    if(n) el.textContent=n
  })
  document.querySelectorAll('span.creator').forEach(el=>{
    const key=`${el.dataset.tp}_${el.dataset.id}`
    const c=map[key]?.creator
    if(c) el.textContent=c
    else if(el.textContent==='...') el.textContent='—'
  })
  // ราคาไม่แตะ — คอลัมน์ราคาต้องคงเป็นราคาที่จ่ายจริงตอนซื้อ (e.p จาก DataStore) เสมอ
  // ไม่ใช่ราคาปัจจุบันของไอเทม ซึ่งอาจเปลี่ยนไปแล้วโดยเฉพาะไอเทม Limited
}

async function loadThumbnails(items){
  try{
    const unique=[...new Map(items.map(e=>[`${e.tp}_${e.id}`,{id:e.id,tp:e.tp}])).values()]
    const r=await fetch('/api/item-details',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-Password':_pw},
      body:JSON.stringify(unique)
    })
    _itemMap=await r.json()
    applyDetails(items,_itemMap)
  }catch{}
}
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[0]} {args[1]}")

    def _check_auth(self):
        return self.headers.get("X-Password", "") == PASSWORD

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/item-details":
            if not self._check_auth():
                self._json(401, {"message": "Unauthorized"}); return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                items  = json.loads(body.decode())
                self._json(200, fetch_item_details(items))
            except Exception as e:
                self._json(500, {"message": str(e)})
        else:
            self._json(404, {"message": "Not found"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/debug-creator":
            p   = urllib.parse.parse_qs(parsed.query)
            iid = int((p.get("id") or ["0"])[0])
            tp  = (p.get("tp") or ["A"])[0]
            out = {}
            # Economy API
            try:
                url = f"https://economy.roblox.com/v2/assets/{iid}/details"
                d   = roblox_public(url)
                out["economy"] = {"ok": True, "Name": d.get("Name"), "PriceInRobux": d.get("PriceInRobux"), "Creator": d.get("Creator")}
            except Exception as e:
                out["economy"] = {"ok": False, "error": str(e)}
            # Catalog items API (with CSRF)
            try:
                ci = catalog_item_detail(iid, tp)
                if ci:
                    out["catalog"] = {"ok": True, "name": ci.get("name"), "price": ci.get("price"), "creatorName": ci.get("creatorName")}
                else:
                    out["catalog"] = {"ok": False, "error": "no data returned"}
            except Exception as e:
                out["catalog"] = {"ok": False, "error": str(e)}
            self._json(200, out)
            return
        if parsed.path == "/api/auth":
            if self._check_auth():
                self._json(200, {"ok": True})
            else:
                self._json(401, {"message": "Unauthorized"})
        elif parsed.path == "/api/history":
            if not self._check_auth():
                self._json(401, {"message": "Unauthorized"}); return
            self._api(parsed.query)
        elif parsed.path == "/api/all-history":
            if not self._check_auth():
                self._json(401, {"message": "Unauthorized"}); return
            self._api_all(parsed.query)
        elif parsed.path == "/api/test-digest":
            if not self._check_auth():
                self._json(401, {"message": "Unauthorized"}); return
            try:
                run_daily_digest()
                self._json(200, {"ok": True, "message": f"ส่งไปที่ {NOTIFY_EMAIL} แล้ว (ดูผลจริงใน log ของ server)"})
            except Exception as e:
                self._json(500, {"message": str(e)})
        else:
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    def _api(self, qs):
        p      = urllib.parse.parse_qs(qs)
        query  = (p.get("q") or [""])[0].strip()
        from_s = (p.get("from") or [""])[0]
        to_s   = (p.get("to")   or [""])[0]
        from_ts = float(from_s) if from_s else None
        to_ts   = float(to_s)   if to_s   else None

        if not query:
            self._json(400, {"message": "กรุณากรอกชื่อหรือ ID"}); return

        if query.isdigit():
            uid = int(query)
            username = get_display_name(uid)
        else:
            uid, username = resolve_username(query)
            if not uid:
                self._json(404, {"message": f"ไม่พบผู้เล่น: {query}"}); return

        entries = fetch_history(uid, from_ts, to_ts)
        if entries is None:
            self._json(500, {"message": "DataStore error"}); return

        self._json(200, {"userId": uid, "username": username, "entries": entries})

    def _api_all(self, qs):
        p      = urllib.parse.parse_qs(qs)
        from_s = (p.get("from") or [""])[0]
        to_s   = (p.get("to")   or [""])[0]
        from_ts = float(from_s) if from_s else None
        to_ts   = float(to_s)   if to_s   else None
        try:
            entries = fetch_all_history(from_ts, to_ts)
            self._json(200, {"entries": entries, "total": len(entries)})
        except Exception as e:
            print(f"[all-history error] {e}")
            self._json(500, {"message": str(e)})

    def _json(self, code, data):
        b = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

if __name__ == "__main__":
    if not API_KEY:
        print("⚠️  ไม่พบ API_KEY — ตั้งค่า environment variable ก่อน")
    if SMTP_USER and SMTP_APP_PASSWORD:
        threading.Thread(target=digest_scheduler, daemon=True).start()
        print(f"📧 Email digest เปิดใช้งาน → ส่งทุกวัน {DIGEST_HOUR:02d}:00 น. ไปที่ {NOTIFY_EMAIL}")
    else:
        print("ℹ️  ไม่ได้ตั้งค่า SMTP_USER / SMTP_APP_PASSWORD — ปิดใช้งาน email digest (ดู README)")
    print(f"\n Purchase History  →  http://localhost:{PORT}\n")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
