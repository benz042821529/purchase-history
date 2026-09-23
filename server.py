#!/usr/bin/env python3
import os, json, time, threading, hmac, datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

API_KEY     = os.environ.get("API_KEY", "")
UNIVERSE_ID = os.environ.get("UNIVERSE_ID", "")
PASSWORD    = os.environ.get("PASSWORD", "admin")
PORT        = int(os.environ.get("PORT", 8080))

# หน้า "รายการซื้อทั้งหมด" แยกออกมาเป็น URL ลับ /all/<VIEW_TOKEN> — ใครมีลิงก์ก็ดูได้ ไม่ต้องใส่รหัสผ่าน
# ตั้ง env var VIEW_TOKEN บน Render เพื่อเปลี่ยน token ได้ (ตั้งว่างเพื่อปิดหน้านี้ทั้งหมด)
VIEW_TOKEN  = os.environ.get("VIEW_TOKEN", "_GOjnm1-spvz_Gvdarkl67epN_KmfjCE")

def token_ok(given):
    """เทียบ token แบบ constant-time โดยไม่ให้ non-ASCII ทำ compare_digest พัง"""
    if not VIEW_TOKEN or not isinstance(given, str) or not given.isascii():
        return False
    return hmac.compare_digest(given, VIEW_TOKEN)

BASE    = f"https://apis.roblox.com/datastores/v1/universes/{UNIVERSE_ID}"
DS_NAME = "PurchaseLog_v1"

# สรุปยอดค่าคอมต่อคน (เขียนโดย ServerScriptService.Admin.PurchaseHistoryServer ฝั่งเกม)
COMMISSION_DS_NAME = "PurchaseBuyersIndex_v1"
COMMISSION_KEY     = "AllBuyers"

# วันที่ตัดยอดล่าสุดต่อคน (userId -> "YYYY-MM-DD") -- เก็บถาวรใน GitHub Gist แยกจาก Roblox ทั้งหมด
# ข้อมูล Roblox แค่ดึงมาแสดง ไม่เขียนกลับ; เดิมเก็บใน cutoffs.json แต่ดิสก์ Render free tier โดนล้างทุกครั้งที่ deploy/หลับ
# ตั้ง env var บน Render: GIST_TOKEN (GitHub token สิทธิ์ gist) และ GIST_ID (id ของ gist ที่มีไฟล์ cutoffs.json)
GIST_TOKEN = os.environ.get("GIST_TOKEN", "").strip()
# รับได้ทั้ง id เปล่าๆ หรือวางลิงก์เต็ม https://gist.github.com/<user>/<id>
GIST_ID    = os.environ.get("GIST_ID", "").strip().rstrip("/").split("/")[-1].split("#")[0].split("?")[0]
GIST_FILE  = "cutoffs.json"
# ใช้ seed ครั้งแรกเท่านั้น (ตอนที่ไฟล์ใน gist ยังว่าง)
DEFAULT_CUTOFFS = {
    "9240613140":  "2026-08-16",
    "7618177365":  "2026-09-18",
    "9220235901":  "2026-09-12",
    "9750743916":  "2026-09-21",
    "8988744930":  "2026-08-16",
    "9675242101":  "2026-07-24",
    "10154004825": "2026-09-21",
    "10268037883": "2026-09-16",
    "9435680691":  "2026-09-18",
    "927899604":   "2026-09-14",
    "8792410182":  "2026-09-14",
    "8619451331":  "2026-09-14",
    "3982309897":  "2026-09-14",
    "9061633806":  "2026-09-14",
    "8793030389":  "2026-09-22",
    "9876746009":  "2026-09-14",
    "9404303714":  "2026-09-20",
    "9110827190":  "2026-09-20",
    "8984915796":  "2026-09-21",
    "10914473375": "2026-09-20",
    "3865531926":  "2026-09-21",
    "9148725166":  "2026-09-21",
}
_cutoff_lock  = threading.Lock()
_cutoff_cache = None  # dict ที่โหลดจาก gist สำเร็จแล้ว

def _gist_request(method, body=None):
    if not (GIST_TOKEN and GIST_ID):
        raise RuntimeError("GIST_TOKEN / GIST_ID ยังไม่ได้ตั้ง")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{GIST_ID}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
            "User-Agent":    "purchase-history-dashboard",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        hint = {401: "GIST_TOKEN ไม่ถูกต้อง/หมดอายุ",
                403: "GIST_TOKEN ไม่มีสิทธิ์เขียน gist",
                404: "ไม่เจอ gist -- เช็ก GIST_ID หรือ token ไม่มีสิทธิ์ gist"}.get(e.code, "")
        raise RuntimeError(f"gist {method} HTTP {e.code} {hint} (GIST_ID={GIST_ID[:6]}..., len={len(GIST_ID)})") from None

def _write_cutoffs(cutoffs):
    content = json.dumps(cutoffs, ensure_ascii=False, indent=2, sort_keys=True)
    _gist_request("PATCH", {"files": {GIST_FILE: {"content": content}}})

def _read_cutoffs():
    """คืน dict จาก gist; ถ้าไฟล์ยังไม่มี/ว่าง ให้ seed ด้วย DEFAULT_CUTOFFS; error อื่นให้ throw"""
    f = (_gist_request("GET").get("files") or {}).get(GIST_FILE)
    content = (f or {}).get("content", "").strip()
    if content and content != "{}":
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    seed = dict(DEFAULT_CUTOFFS)
    _write_cutoffs(seed)
    return seed

def load_cutoffs():
    global _cutoff_cache
    with _cutoff_lock:
        if _cutoff_cache is None:
            try:
                _cutoff_cache = _read_cutoffs()
            except Exception as e:
                print(f"[load_cutoffs] {e}")
                return {}
        return dict(_cutoff_cache)

def save_cutoff(uid, date_str):
    global _cutoff_cache
    with _cutoff_lock:
        # อ่านค่าล่าสุดจาก gist ก่อนเขียนเสมอ -- ถ้าอ่านไม่ได้ให้ล้มเหลว ห้ามเขียนทับด้วยข้อมูลเก่า
        cutoffs = _read_cutoffs()
        if date_str:
            cutoffs[str(uid)] = date_str
        else:
            cutoffs.pop(str(uid), None)
        _write_cutoffs(cutoffs)
        _cutoff_cache = cutoffs
        return dict(cutoffs)

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


def fetch_avatars(uids):
    """ดึง avatar headshot ของผู้เล่นเป็นชุด -> {userId: imageUrl} เฉพาะที่ state=Completed"""
    out = {}
    base = "https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={}&size=150x150&format=Png&isCircular=true"
    for i in range(0, len(uids), 100):
        chunk = ",".join(str(x) for x in uids[i:i + 100])
        try:
            d = roblox_public(base.format(chunk))
            for x in d.get("data", []):
                if x.get("state") == "Completed" and x.get("imageUrl"):
                    out[x["targetId"]] = x["imageUrl"]
        except Exception as e:
            print(f"[avatars] {e}")
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


# ต้องตรงกับ COMMISSION_MIN_PRICE ใน ServerScriptService.Admin.PurchaseHistoryServer (ฝั่งเกม)
COMMISSION_MIN_PRICE = 5
# เจ้าของแมพ (AdminConfig.OWNER_ID ฝั่งเกม) -- ของที่เจ้าของซื้อเองไม่นับเข้าค่าคอมเลย (เหมือน COMMISSION_EXCLUDED_IDS ใน PurchaseHistoryPanel.lua)
OWNER_ID = 8486039661
# ค่าคอมจริงที่จ่าย = 40% ของยอดที่เข้าเกณฑ์ (commissionBase คือยอด "ฐาน" ก่อนคูณเปอร์เซ็นต์ ไม่ใช่ยอดจ่ายจริง -- ยังไม่เคยคูณใน Studio เลย ทำตรงนี้ที่เดียว)
# 40% นี้แบ่งเป็น 2 ส่วนเท่ากัน: คนซื้อได้ 20% (buyerPay) + เจ้าของแมพได้ 20% (ownerPay) -- หารจาก commissionPay ที่ปัดแล้ว ไม่ใช่คำนวณ 20% แยก 2 รอบ กันผลรวมคลาดเคลื่อนจากการปัดเศษซ้ำ
COMMISSION_RATE = 0.40

def entry_earns_commission(e):
    if (e.get("p") or 0) < COMMISSION_MIN_PRICE:
        return False
    if e.get("tp") == "B":
        return True
    return not e.get("lim")


def fetch_commission_data():
    """สรุปยอดค่าคอมของทุกคน (totalSpent/commissionBase/purchaseCount/lastTs) จาก PurchaseBuyersIndex_v1 -- ยอดสะสมทั้งหมด ยังไม่หักส่วนที่จ่ายไปแล้ว"""
    ck = ("commission",)
    cached = cache_get(ck, 120)
    if cached is not None:
        return cached
    try:
        data = roblox_get(
            f"{BASE}/standard-datastores/datastore/entries/entry",
            {"datastoreName": COMMISSION_DS_NAME, "entryKey": COMMISSION_KEY}
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = []
        else:
            raise
    if not isinstance(data, list):
        data = []
    data.sort(key=lambda x: x.get("lastTs", 0), reverse=True)

    avatars = fetch_avatars([b["userId"] for b in data if b.get("userId")])
    for b in data:
        b["avatar"] = avatars.get(b.get("userId"), "")

    for b in data:
        # ของเจ้าของแมพเองไม่นับค่าคอม -- commissionBase ที่เก็บใน index มาจากฝั่งเกม (ยังไม่ได้กันเจ้าของไว้ตรงนั้น) เลยต้องกันตรงนี้แทน totalSpent/purchaseCount ยังโชว์จริงตามปกติ แค่ค่าคอมเป็น 0
        base = 0 if b.get("userId") == OWNER_ID else (b.get("commissionBase") or 0)
        b["commissionPay"] = round(base * COMMISSION_RATE, 2)
        b["buyerPay"] = round(b["commissionPay"] / 2, 2)
        b["ownerPay"] = round(b["commissionPay"] - b["buyerPay"], 2)

    cache_set(ck, data)
    return data


def fetch_commission_data_ranged(from_ts, to_ts):
    """สรุปยอดค่าคอมเฉพาะที่ซื้อในช่วงวันที่ที่กำหนด (ไล่สแกนทุก entry จริงในช่วงนั้น ไม่ใช่ยอดสะสมทั้งหมด)"""
    entries = fetch_all_history(from_ts, to_ts)
    byUid = {}
    for e in entries:
        uid = e["uid"]
        row = byUid.setdefault(uid, {
            "userId": uid, "username": e.get("username", str(uid)), "displayName": None,
            "totalSpent": 0, "commissionBase": 0, "purchaseCount": 0, "lastTs": 0,
        })
        row["totalSpent"] += (e.get("p") or 0)
        if uid != OWNER_ID and entry_earns_commission(e):  # ของเจ้าของแมพเองไม่นับค่าคอม
            row["commissionBase"] += (e.get("p") or 0)
        row["purchaseCount"] += 1
        row["lastTs"] = max(row["lastTs"], e.get("ts") or 0)

    data = list(byUid.values())
    data.sort(key=lambda x: x.get("lastTs", 0), reverse=True)

    avatars = fetch_avatars([b["userId"] for b in data])
    for b in data:
        b["avatar"] = avatars.get(b["userId"], "")
        b["commissionPay"] = round((b.get("commissionBase") or 0) * COMMISSION_RATE, 2)
        b["buyerPay"] = round(b["commissionPay"] / 2, 2)
        b["ownerPay"] = round(b["commissionPay"] - b["buyerPay"], 2)

    return data


def add_outstanding_commission(buyers, cutoffs):
    """ค่าคอมส่วนลูกค้า 20% ที่ยังค้างจ่าย = ครึ่งหนึ่งของค่าคอม 40% จากยอดที่ซื้อหลังวันตัดยอด (หลังเที่ยงคืนของวันนั้น เวลาไทย) จนถึงตอนนี้
    คนที่ตัดยอดถึงวันซื้อล่าสุดแล้วได้ 0 ไม่ต้องดึง log; ไม่มีวันตัดยอดเลย = ค้างทั้งหมด"""
    tz = datetime.timezone(datetime.timedelta(hours=7))
    todo = {}
    for b in buyers:
        uid = b.get("userId")
        b["outstandingPay"] = 0
        if uid == OWNER_ID:
            continue
        cut = cutoffs.get(str(uid))
        from_ts = 0
        if cut:
            try:
                d = datetime.datetime.strptime(cut, "%Y-%m-%d").replace(tzinfo=tz) + datetime.timedelta(days=1)
                from_ts = int(d.timestamp())
            except ValueError:
                pass
        if (b.get("lastTsAll") or 0) < from_ts:
            continue
        todo[uid] = from_ts
    def work(item):
        uid, from_ts = item
        return uid, fetch_history(uid, from_ts=from_ts or None)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(ex.map(work, todo.items()))
    for b in buyers:
        uid = b.get("userId")
        if uid not in results:
            continue
        entries = results[uid]
        if entries is None:
            b["outstandingPay"] = None  # ดึง log ไม่สำเร็จ
            continue
        base = sum((e.get("p") or 0) for e in entries if entry_earns_commission(e))
        # หารครึ่งจาก 40% ที่ปัดแล้ว แบบเดียวกับ buyerPay กันผลรวมคลาดจากการปัดเศษซ้ำ
        b["outstandingPay"] = round(round(base * COMMISSION_RATE, 2) / 2, 2)


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
    date:`${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()}`,
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
  if(res.ok){_pw=pw;document.getElementById('loginOverlay').style.display='none'}
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

async function search(){
  const q=document.getElementById('query').value.trim()
  const btn=document.getElementById('searchBtn')
  const status=document.getElementById('status')
  if(!q){status.className='status err';status.textContent='กรุณากรอกชื่อ Player หรือ User ID';return}
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


# ── หน้า "รายการซื้อทั้งหมด" แบบแยก URL ลับ (ไม่มี login overlay, ยืนยันตัวด้วย token ใน URL) ──
ALL_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>รายการซื้อทั้งหมด</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f0f2f7;color:#1a1a2e;font-family:'Segoe UI',sans-serif;padding:28px 16px}
h1{color:#1a1a2e;font-size:22px;font-weight:700;margin-bottom:4px}
.sub{color:#888;font-size:12px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e4e6ef;border-radius:14px;padding:20px;max-width:780px;margin:0 auto 14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card-title{font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;font-weight:600}
.btn{padding:10px 24px;background:#4f8ef7;border:none;border-radius:9px;color:#fff;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#3a7de8}.btn:disabled{background:#ccc;cursor:default}
.date-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:4px}
.fg label{font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
input[type=date]{padding:8px 10px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:13px;outline:none}
input[type=date]:focus{border-color:#4f8ef7}
.qrow{display:flex;gap:6px;align-items:flex-end}
.qbtn{padding:8px 14px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#888;font-size:12px;cursor:pointer;font-weight:600}
.qbtn:hover{border-color:#4f8ef7;color:#4f8ef7}
.qbtn.active{background:#4f8ef7;color:#fff;border-color:#4f8ef7}
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
</style>
</head>
<body>

<div style="max-width:780px;margin:0 auto 18px">
  <h1>รายการซื้อทั้งหมด</h1>
  <div class="sub">ประวัติการซื้อของผู้เล่นทุกคน</div>
</div>

<div class="card">
  <div class="card-title">ตัวกรอง</div>
  <div class="date-row">
    <div class="fg"><label>จากวันที่</label><input type="date" id="fromDate" onchange="loadAll()"></div>
    <div class="fg"><label>ถึงวันที่</label><input type="date" id="toDate" onchange="loadAll()"></div>
    <div class="qrow">
      <button class="qbtn rbtn" onclick="quickFilter(1)">วันนี้</button>
      <button class="qbtn rbtn" onclick="quickFilter(7)">7 วัน</button>
      <button class="qbtn rbtn" onclick="quickFilter(30)">30 วัน</button>
      <button class="qbtn rbtn active" onclick="quickFilter(0)">ทั้งหมด</button>
    </div>
    <div class="qrow">
      <button class="qbtn" id="sortHighBtn" onclick="sortByPrice('desc')">ราคา: สูง → ต่ำ</button>
      <button class="qbtn" id="sortLowBtn" onclick="sortByPrice('asc')">ราคา: ต่ำ → สูง</button>
    </div>
    <div class="qrow"><button class="btn" onclick="loadAll()">โหลดใหม่</button></div>
  </div>
</div>

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
const TOKEN='__VIEW_TOKEN__'
let _items=[],_itemMap={},_sortMode=null
function pad(n){return String(n).padStart(2,'0')}
function fmtParts(ts){
  const d=new Date(ts*1000)
  return{
    date:`${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()}`,
    time:`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  }
}
function toISO(ts){const d=new Date(ts*1000);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`}
function dateToTs(s,end=false){
  if(!s)return ''
  const[y,m,d]=s.split('-').map(Number)
  return Math.floor(new Date(y,m-1,d,end?23:0,end?59:0,end?59:0).getTime()/1000)
}

function quickFilter(days){
  document.querySelectorAll('.rbtn').forEach(b=>b.classList.remove('active'))
  event.target.classList.add('active')
  if(!days){document.getElementById('fromDate').value='';document.getElementById('toDate').value=''}
  else{
    const now=new Date(),from=new Date(now)
    from.setDate(now.getDate()-days+1);from.setHours(0,0,0,0)
    document.getElementById('fromDate').value=toISO(Math.floor(from.getTime()/1000))
    document.getElementById('toDate').value=toISO(Math.floor(now.getTime()/1000))
  }
  loadAll()
}

function resetSortUI(){
  _sortMode=null
  document.getElementById('sortHighBtn').classList.remove('active')
  document.getElementById('sortLowBtn').classList.remove('active')
}

function sortByPrice(mode){
  const btn=document.getElementById(mode==='desc'?'sortHighBtn':'sortLowBtn')
  if(_sortMode===mode){
    resetSortUI()
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
  document.getElementById('tbody').innerHTML=renderRows(items)
  applyDetails(items,_itemMap)
}

function renderRows(items){
  return items.map(e=>{
    const isB=e.tp==='B',{date,time}=fmtParts(e.ts)
    const player=e.username||''
    return `<tr><td><img class="thumb" data-id="${e.id}" data-tp="${e.tp}" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></td><td class="player-col">${player}</td><td class="item-name" data-id="${e.id}" data-tp="${e.tp}">${e.n||'ID:'+e.id}</td><td><span class="creator" data-id="${e.id}" data-tp="${e.tp}">${e.cr||'...'}</span></td><td class="price" data-id="${e.id}" data-tp="${e.tp}">${e.p?'R$ '+e.p:'ฟรี'}</td><td><span class="badge ${isB?'bb':'ba'}">${isB?'Bundle':'Asset'}</span></td><td><div class="date-cell">${date}</div></td><td><div class="time-cell">${time}</div></td></tr>`
  }).join('')
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
}

async function loadThumbnails(items){
  try{
    const unique=[...new Map(items.map(e=>[`${e.tp}_${e.id}`,{id:e.id,tp:e.tp}])).values()]
    const r=await fetch('/api/item-details',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-View-Token':TOKEN},
      body:JSON.stringify(unique)
    })
    _itemMap=await r.json()
    applyDetails(items,_itemMap)
  }catch{}
}

async function loadAll(){
  const status=document.getElementById('status')
  status.className='status';status.textContent='กำลังโหลด...'
  document.getElementById('statsRow').style.display='none'
  document.getElementById('tbl').style.display='none'
  document.getElementById('tbody').innerHTML=''
  const from=dateToTs(document.getElementById('fromDate').value,false)
  const to=dateToTs(document.getElementById('toDate').value,true)
  const params=new URLSearchParams({from:from||'',to:to||''})
  try{
    const res=await fetch('/api/all-history?'+params,{headers:{'X-View-Token':TOKEN}})
    if(res.status===401||res.status===403){status.className='status err';status.textContent='ลิงก์ไม่ถูกต้อง';return}
    const data=await res.json()
    if(!res.ok){status.className='status err';status.textContent='Error: '+(data.message||res.status);return}
    const items=data.entries||[]
    if(!items.length){status.className='status';status.textContent='ไม่มีประวัติการซื้อ';return}
    status.className='status ok';status.textContent=`ทั้งหมด ${items.length} รายการจากทุกผู้เล่น`
    document.getElementById('sTotal').textContent=items.length.toLocaleString()
    document.getElementById('sRevenue').textContent='R$ '+items.reduce((s,e)=>s+(e.p||0),0).toLocaleString()
    document.getElementById('statsRow').style.display='flex'
    _items=items;resetSortUI()
    document.getElementById('tbody').innerHTML=renderRows(items)
    document.getElementById('tbl').style.display='table'
    loadThumbnails(items)
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
}

window.addEventListener('DOMContentLoaded',loadAll)
</script>
</body>
</html>"""

# ── หน้า "ค่าคอมมิชชั่น" แบบแยก URL ลับ (เหมือน /all/ ใครมีลิงก์ก็ดูได้ ไม่ต้อง login) ──
COMMISSION_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ค่าคอมมิชชั่น</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f0f2f7;color:#1a1a2e;font-family:'Segoe UI',sans-serif;padding:28px 16px}
h1{color:#1a1a2e;font-size:22px;font-weight:700;margin-bottom:4px}
.sub{color:#888;font-size:12px;margin-bottom:22px}
.btn{padding:10px 24px;background:#4f8ef7;border:none;border-radius:9px;color:#fff;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#3a7de8}.btn:disabled{background:#ccc;cursor:default}
.back{display:none;padding:8px 16px;background:#fff;border:1.5px solid #e4e6ef;border-radius:9px;color:#4f8ef7;font-size:13px;font-weight:700;cursor:pointer;margin-bottom:14px}
.back:hover{border-color:#4f8ef7}
.status{max-width:1400px;margin:0 auto 10px;font-size:13px;color:#aaa;min-height:16px}
.status.err{color:#ef4444}.status.ok{color:#22c55e}
.stats{max-width:1400px;margin:0 auto 14px;display:flex;gap:10px;flex-wrap:wrap}
.stat{flex:1;min-width:120px;background:#fff;border:1px solid #e4e6ef;border-radius:12px;padding:14px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.stat .val{font-size:22px;font-weight:700;color:#4f8ef7}
.stat .lbl{font-size:11px;color:#aaa;margin-top:3px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.tbl-wrap{max-width:1400px;margin:0 auto;overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0 5px;font-size:13px}
thead th{padding:6px 14px;color:#bbb;font-size:11px;text-transform:uppercase;letter-spacing:.5px;text-align:left;font-weight:600}
tbody tr{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.05)}
tbody tr.clickable{cursor:pointer}
tbody tr.clickable:hover{box-shadow:0 2px 8px rgba(79,142,247,.15)}
td{padding:11px 14px;border-top:1px solid #f0f2f7;border-bottom:1px solid #f0f2f7}
td:first-child{border-left:1px solid #f0f2f7;border-radius:10px 0 0 10px}
td:last-child{border-right:1px solid #f0f2f7;border-radius:0 10px 10px 0}
#listTbl td:last-child{position:sticky;right:0;background:#fff;box-shadow:-6px 0 8px -6px rgba(0,0,0,.12)}
#listTbl thead th:last-child{position:sticky;right:0;background:#f0f2f7}
.rank{color:#bbb;font-weight:700;font-size:13px}
.name-cell{color:#1a1a2e;font-weight:700}
.uid-cell{color:#aaa;font-size:11px;margin-top:2px}
.money{color:#f59e0b;font-weight:700}
.comm{color:#22c55e;font-weight:700}
.buyerpay{color:#ef4444;font-weight:700}
.ownerpay{color:#7c3aed;font-weight:700}
#sBuyerTotal,#dBuyerPay{color:#ef4444}
#dOwnerPay{color:#7c3aed}
#sCommTotal,#dCommTotal{color:#22c55e}
.item-name{color:#1a1a2e;font-weight:600}
.price{color:#f59e0b;font-weight:700}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700}
.ba{background:#eff6ff;color:#3b82f6}.bb{background:#f5f3ff;color:#7c3aed}
.cyes{background:#f0fdf4;color:#22c55e}.cno{background:#f9fafb;color:#9ca3af}
.date-cell{color:#1a1a2e;font-size:13px;font-weight:500}
.time-cell{color:#aaa;font-size:12px;margin-top:2px}
.creator{color:#888;font-size:12px}
.thumb{width:48px;height:48px;border-radius:8px;object-fit:cover;background:#f0f2f7;display:block}
.wide{max-width:1400px}
.card{background:#fff;border:1px solid #e4e6ef;border-radius:14px;padding:16px 20px;max-width:1400px;margin:0 auto 14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.date-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:4px}
.fg label{font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
input[type=date]{padding:8px 10px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:13px;outline:none}
input[type=date]:focus{border-color:#4f8ef7}
input[type=text]{width:100%;padding:8px 12px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#1a1a2e;font-size:13px;outline:none}
input[type=text]:focus{border-color:#4f8ef7;background:#fff}
input[type=text]::placeholder{color:#bbb}
.qrow{display:flex;gap:6px;align-items:flex-end}
.qbtn{padding:8px 14px;background:#f7f8fc;border:1.5px solid #e4e6ef;border-radius:9px;color:#888;font-size:12px;cursor:pointer;font-weight:600}
.qbtn:hover{border-color:#4f8ef7;color:#4f8ef7}
.qbtn.active{background:#4f8ef7;color:#fff;border-color:#4f8ef7}
.cutoffInput{width:160px;min-width:160px;padding:6px 4px;font-size:12px}
.cutoffWrap{display:flex;align-items:center;gap:6px}
#listTbl tr.groupRow{background:transparent;box-shadow:none}
#listTbl tr.groupRow td{position:static;background:transparent;box-shadow:none;border:none;padding:14px 4px 4px;font-size:14px;font-weight:800}
#listTbl tr.groupRow.pending td{color:#e67e22}
#listTbl tr.groupRow.done td{color:#22a55b}
#listTbl tr.settled td,#listTbl tr.settled td:last-child{background:#f3fbf6}
.settledMark{width:18px;font-size:16px;color:#22a55b;font-weight:800;text-align:center}
.cutoffLabel{font-size:13px;color:#1a1a2e;font-weight:700;padding:8px 0}
</style>
</head>
<body>

<div style="max-width:1400px;margin:0 auto 18px">
  <h1>ค่าคอมมิชชั่น</h1>
  <div class="sub">ค่าคอม = 40% ของยอดที่เข้าเกณฑ์ (ไม่นับไอเทม &lt;5 Robux และ Limited) — ยอดสะสมทั้งหมด ยังไม่หักส่วนที่จ่ายไปแล้ว กดชื่อเพื่อดูรายการซื้อ</div>
</div>

<button class="back" id="backBtn" onclick="showList()">← กลับไปรายชื่อทั้งหมด</button>

<div class="status" id="status"></div>

<div id="listView">
  <div class="card">
    <div class="date-row">
      <div class="fg"><label>จากวันที่</label><input type="date" id="listFromDate" onchange="loadList()"></div>
      <div class="fg"><label>ถึงวันที่</label><input type="date" id="listToDate" onchange="loadList()"></div>
      <div class="qrow">
        <button class="qbtn" onclick="listQuickFilter(1)">วันนี้</button>
        <button class="qbtn" onclick="listQuickFilter(7)">7 วัน</button>
        <button class="qbtn" onclick="listQuickFilter(30)">30 วัน</button>
        <button class="qbtn active" onclick="listQuickFilter(0)">ทั้งหมด</button>
      </div>
    </div>
    <div class="date-row" style="margin-top:10px">
      <div class="fg" style="flex:1;min-width:200px"><label>ค้นหาชื่อ / User ID</label><input type="text" id="nameSearch" placeholder="พิมพ์ชื่อหรือ User ID..." oninput="filterListByName()"></div>
    </div>
  </div>
  <div class="stats" id="listStats" style="display:none">
    <div class="stat"><div class="val" id="sPeople">0</div><div class="lbl">คนที่ซื้อ</div></div>
    <div class="stat"><div class="val" id="sBuyerTotal">0</div><div class="lbl">คนซื้อได้รวม 20% (Robux)</div></div>
    <div class="stat"><div class="val" id="sCommTotal">0</div><div class="lbl">ค่าคอมรวม 40% (Robux)</div></div>
    <div class="stat"><div class="val" id="sSpentTotal">0</div><div class="lbl">ยอดซื้อรวมทั้งหมด (ไม่หักค่าคอม)</div></div>
  </div>
  <div class="tbl-wrap">
    <table id="listTbl" style="display:none">
      <thead><tr><th>#</th><th style="width:52px"></th><th>ผู้เล่น</th><th>ยอดซื้อรวม</th><th>ฐานค่าคอม</th><th>คนซื้อได้ 20%</th><th>ค้างจ่ายลูกค้า 20%<br><span style="text-transform:none">(หลังวันตัดยอด → วันนี้)</span></th><th>รวม 40%</th><th>จำนวนครั้ง</th><th>ซื้อล่าสุด</th><th>ตัดยอดล่าสุด</th></tr></thead>
      <tbody id="listBody"></tbody>
    </table>
  </div>
</div>

<div id="detailView" style="display:none">
  <div class="card">
    <div class="date-row">
      <div class="fg"><label>จากวันที่</label><input type="date" id="fromDate" onchange="renderDetail()"></div>
      <div class="fg"><label>ถึงวันที่</label><input type="date" id="toDate" onchange="renderDetail()"></div>
      <div class="qrow">
        <button class="qbtn" onclick="quickFilter(7)">7 วัน</button>
        <button class="qbtn" onclick="quickFilter(30)">30 วัน</button>
        <button class="qbtn active" onclick="quickFilter(0)">ทั้งหมด</button>
      </div>
    </div>
    <div class="date-row" style="margin-top:10px">
      <div class="fg"><label>ตัดยอดล่าสุด</label><div class="cutoffLabel" id="cutoffLabel">-</div></div>
      <button class="qbtn" onclick="saveDetailCutoff()">💾 บันทึกเป็นวันตัดยอดล่าสุด</button>
    </div>
  </div>
  <div class="stats" id="detailStats">
    <div class="stat"><div class="val" id="dItems">0</div><div class="lbl">รายการที่ซื้อ</div></div>
    <div class="stat"><div class="val" id="dAssets">0</div><div class="lbl">Asset</div></div>
    <div class="stat"><div class="val" id="dBundles">0</div><div class="lbl">Bundle</div></div>
    <div class="stat"><div class="val" id="dSpent">0</div><div class="lbl">ยอดซื้อรวม</div></div>
    <div class="stat"><div class="val" id="dCommBase">0</div><div class="lbl">ฐานค่าคอม</div></div>
    <div class="stat"><div class="val" id="dBuyerPay">0</div><div class="lbl">คนซื้อได้ 20%</div></div>
    <div class="stat"><div class="val" id="dOwnerPay">0</div><div class="lbl">เจ้าของแมพได้ 20%</div></div>
    <div class="stat"><div class="val" id="dCommTotal">0</div><div class="lbl">รวมค่าคอม 40%</div></div>
  </div>
  <div class="tbl-wrap">
    <table id="detailTbl">
      <thead><tr><th style="width:60px"></th><th>สินค้า</th><th>ผู้สร้าง</th><th>ราคา</th><th>ประเภท</th><th>นับค่าคอม</th><th>วันที่</th><th>เวลา</th></tr></thead>
      <tbody id="detailBody"></tbody>
    </table>
  </div>
</div>

<script>
const TOKEN='__VIEW_TOKEN__'
function pad(n){return String(n).padStart(2,'0')}
function fmtParts(ts){
  const d=new Date(ts*1000)
  return{date:`${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()}`,time:`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`}
}

function showList(){
  document.getElementById('detailView').style.display='none'
  document.getElementById('listView').style.display='block'
  document.getElementById('backBtn').style.display='none'
  history.replaceState(null,'','#')
}

const BLANK_PX="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
const COMMISSION_RATE=0.40
function fmtR(n){return 'R$ '+(n||0).toLocaleString(undefined,{maximumFractionDigits:2})}
// แบ่งค่าคอม 40% (ที่คำนวณแล้ว) ออกเป็น 2 ส่วนเท่าๆ -- หารจากยอดที่ได้แล้ว ไม่คำนวณ 20% แยก 2 รอบ (กันคลาดเคลื่อนจากการปัดเศษซ้ำ)
function splitComm(total){const buyer=Math.round((total/2)*100)/100;return{buyer,owner:Math.round((total-buyer)*100)/100}}
let _listRows=[]

function filterListByName(){
  const q=(document.getElementById('nameSearch').value||'').trim().toLowerCase()
  if(!q){renderList(_listRows);return}
  const filtered=_listRows.filter(b=>{
    const name=(b.displayName||b.username||'').toLowerCase()
    const uname=(b.username||'').toLowerCase()
    const uid=String(b.userId||'')
    return name.includes(q)||uname.includes(q)||uid.includes(q)
  })
  renderList(filtered)
}

function renderList(rows){
  // แยก 2 กลุ่ม: ยังไม่ได้ตัดยอดถึงปัจจุบัน (บน) / ตัดยอดแล้ว ✓ (ล่าง) -- ลำดับในกลุ่มคงเดิม
  const pending=rows.filter(b=>!isSettled(b)), done=rows.filter(isSettled)
  const group=(cls,label,list,start)=>list.length
    ?`<tr class="groupRow ${cls}"><td colspan="11">${label} (${list.length} คน)</td></tr>`+list.map((b,i)=>rowHtml(b,start+i)).join('')
    :''
  document.getElementById('listBody').innerHTML=
    group('pending','⏳ ยังไม่ได้ตัดยอดถึงวันที่ซื้อล่าสุด',pending,0)+
    group('done','✓ ตัดยอดถึงปัจจุบันแล้ว',done,pending.length)
}

function rowHtml(b,i){
    const name=b.displayName||b.username||('ID '+b.userId)
    const last=b.lastTs?fmtParts(b.lastTs).date:'-'
    const pay=b.commissionPay!=null?b.commissionPay:(b.commissionBase||0)*COMMISSION_RATE
    const buyerPay=b.buyerPay!=null?b.buyerPay:splitComm(pay).buyer
    return `<tr class="clickable${isSettled(b)?' settled':''}" onclick="openDetail(${b.userId},'${(name+'').replace(/'/g,"\\\\'")}')">
      <td class="rank">${i+1}</td>
      <td><img class="thumb" src="${b.avatar||BLANK_PX}"></td>
      <td><div class="name-cell">${name}</div><div class="uid-cell">User ID ${b.userId}${b.username&&b.username!==name?' · @'+b.username:''}</div></td>
      <td class="money">${fmtR(b.totalSpent)}</td>
      <td>${fmtR(b.commissionBase)}</td>
      <td class="buyerpay">${fmtR(buyerPay)}</td>
      <td class="ownerpay">${b.outstandingPay==null?'-':fmtR(b.outstandingPay)}</td>
      <td class="comm">${fmtR(pay)}</td>
      <td>${b.purchaseCount||0}</td>
      <td class="date-cell">${last}</td>
      <td onclick="event.stopPropagation()"><div class="cutoffWrap"><input type="date" class="cutoffInput" value="${b.cutoffDate||''}" onchange="saveCutoff(${b.userId},this.value,this)"><span class="settledMark" title="ตัดยอดถึงวันที่ซื้อล่าสุดแล้ว">${isSettled(b)?'✓':''}</span></div></td>
    </tr>`
}

// ตัดยอดถึงปัจจุบันแล้ว = วันตัดยอด >= วันที่ซื้อล่าสุด (เทียบแบบ YYYY-MM-DD ตามเวลาเครื่อง)
function isoDate(ts){const d=new Date(ts*1000);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`}
function isSettled(b){
  const last=b.lastTsAll||b.lastTs
  return !!(b.cutoffDate&&last&&b.cutoffDate>=isoDate(last))
}

async function saveCutoff(uid,date,el){
  el.disabled=true
  try{
    const res=await fetch('/api/cutoff?token='+encodeURIComponent(TOKEN),{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid,date})
    })
    if(!res.ok){alert('บันทึกวันตัดยอดไม่สำเร็จ')}
    else{
      const b=_listRows.find(x=>x.userId===uid)
      if(b){b.cutoffDate=date||null;filterListByName()}
    }
  }catch(e){alert('เกิดข้อผิดพลาด: '+e.message)}
  el.disabled=false
}

function listQuickFilter(days){
  document.querySelectorAll('#listView .qbtn').forEach(b=>b.classList.remove('active'))
  event.target.classList.add('active')
  if(!days){document.getElementById('listFromDate').value='';document.getElementById('listToDate').value=''}
  else{
    const now=new Date(),from=new Date(now)
    from.setDate(now.getDate()-days+1);from.setHours(0,0,0,0)
    document.getElementById('listFromDate').value=toISO(Math.floor(from.getTime()/1000))
    document.getElementById('listToDate').value=toISO(Math.floor(now.getTime()/1000))
  }
  loadList()
}

async function loadList(){
  const status=document.getElementById('status')
  status.className='status';status.textContent='กำลังโหลด...'
  document.getElementById('listStats').style.display='none'
  document.getElementById('listTbl').style.display='none'
  const from=dateToTs(document.getElementById('listFromDate').value,false)
  const to=dateToTs(document.getElementById('listToDate').value,true)
  const params=new URLSearchParams({token:TOKEN})
  if(from!==null)params.set('from',from)
  if(to!==null)params.set('to',to)
  try{
    const res=await fetch('/api/commission?'+params)
    if(res.status===401||res.status===403){status.className='status err';status.textContent='ลิงก์ไม่ถูกต้อง';return}
    const data=await res.json()
    if(!res.ok){status.className='status err';status.textContent='Error: '+(data.message||res.status);return}
    const rows=data.buyers||[]
    document.querySelector('#listStats .lbl').textContent=data.ranged?'คนที่ซื้อในช่วงนี้':'คนที่เคยซื้อ'
    document.querySelectorAll('#listStats .lbl')[1].textContent=(data.ranged?'คนซื้อได้รวม 20% ในช่วงนี้':'คนซื้อได้รวม 20% ทั้งหมด')+' (Robux)'
    document.querySelectorAll('#listStats .lbl')[2].textContent=(data.ranged?'ค่าคอมรวม 40% ในช่วงนี้':'ค่าคอมรวม 40% ทั้งหมด')+' (Robux)'
    document.querySelectorAll('#listStats .lbl')[3].textContent=(data.ranged?'ยอดซื้อรวมในช่วงนี้':'ยอดซื้อรวมทั้งหมด')+' (ไม่หักค่าคอม)'
    if(!rows.length){status.className='status';status.textContent=data.ranged?'ไม่มีใครซื้อในช่วงวันที่นี้':'ยังไม่มีข้อมูลการซื้อ';return}
    status.textContent=''
    document.getElementById('sPeople').textContent=rows.length.toLocaleString()
    document.getElementById('sBuyerTotal').textContent=fmtR(rows.reduce((s,b)=>{const pay=b.commissionPay!=null?b.commissionPay:(b.commissionBase||0)*COMMISSION_RATE;return s+(b.buyerPay!=null?b.buyerPay:splitComm(pay).buyer)},0))
    document.getElementById('sCommTotal').textContent=fmtR(rows.reduce((s,b)=>s+(b.commissionPay!=null?b.commissionPay:(b.commissionBase||0)*COMMISSION_RATE),0))
    document.getElementById('sSpentTotal').textContent=fmtR(rows.reduce((s,b)=>s+(b.totalSpent||0),0))
    document.getElementById('listStats').style.display='flex'
    _listRows=rows
    filterListByName()
    document.getElementById('listTbl').style.display='table'
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
}

function dateToTs(s,end=false){
  if(!s)return null
  const[y,m,d]=s.split('-').map(Number)
  return Math.floor(new Date(y,m-1,d,end?23:0,end?59:0,end?59:0).getTime()/1000)
}
function toISO(ts){const d=new Date(ts*1000);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`}

function quickFilter(days){
  document.querySelectorAll('#detailView .qbtn').forEach(b=>b.classList.remove('active'))
  event.target.classList.add('active')
  if(!days){document.getElementById('fromDate').value='';document.getElementById('toDate').value=''}
  else{
    const now=new Date(),from=new Date(now)
    from.setDate(now.getDate()-days+1);from.setHours(0,0,0,0)
    document.getElementById('fromDate').value=toISO(Math.floor(from.getTime()/1000))
    document.getElementById('toDate').value=toISO(Math.floor(now.getTime()/1000))
  }
  renderDetail()
}

let _detailItems=[],_detailName='',_detailUid=0,_detailCutoff=null

function updateCutoffLabel(){
  document.getElementById('cutoffLabel').textContent=_detailCutoff?_detailCutoff:'ยังไม่เคยตัดยอด'
}

async function saveDetailCutoff(){
  const to=document.getElementById('toDate').value
  if(!to){alert('เลือก "ถึงวันที่" ก่อน แล้วค่อยบันทึกเป็นวันตัดยอด');return}
  try{
    const res=await fetch('/api/cutoff?token='+encodeURIComponent(TOKEN),{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid:_detailUid,date:to})
    })
    if(res.ok){_detailCutoff=to;updateCutoffLabel();alert('บันทึกวันตัดยอดแล้ว: '+to)}
    else alert('บันทึกไม่สำเร็จ')
  }catch(e){alert('เกิดข้อผิดพลาด: '+e.message)}
}

function renderDetail(){
  const from=dateToTs(document.getElementById('fromDate').value,false)
  const to=dateToTs(document.getElementById('toDate').value,true)
  const items=_detailItems.filter(e=>(from===null||e.ts>=from)&&(to===null||e.ts<=to))

  document.getElementById('status').className='status ok'
  document.getElementById('status').textContent=_detailName+' (ID '+_detailUid+')'+(from||to?' — กรองตามช่วงวันที่':'')
  document.getElementById('dItems').textContent=items.length.toLocaleString()
  document.getElementById('dAssets').textContent=items.filter(e=>e.tp!=='B').length.toLocaleString()
  document.getElementById('dBundles').textContent=items.filter(e=>e.tp==='B').length.toLocaleString()
  document.getElementById('dSpent').textContent=fmtR(items.reduce((s,e)=>s+(e.p||0),0))
  const commBase=items.filter(e=>e.earns).reduce((s,e)=>s+(e.p||0),0)
  document.getElementById('dCommBase').textContent=fmtR(commBase)
  const {buyer:dBuyerPay,owner:dOwnerPay}=splitComm(commBase*COMMISSION_RATE)
  document.getElementById('dBuyerPay').textContent=fmtR(dBuyerPay)
  document.getElementById('dOwnerPay').textContent=fmtR(dOwnerPay)
  document.getElementById('dCommTotal').textContent=fmtR(dBuyerPay+dOwnerPay)
  document.getElementById('detailBody').innerHTML=items.map(e=>{
    const isB=e.tp==='B',{date,time}=fmtParts(e.ts)
    return `<tr>
      <td><img class="thumb" src="${e.thumb||BLANK_PX}"></td>
      <td class="item-name">${e.nm||('ID:'+e.id)}</td>
      <td><span class="creator">${e.cr||'—'}</span></td>
      <td class="price">${e.p?'R$ '+e.p:'ฟรี'}</td>
      <td><span class="badge ${isB?'bb':'ba'}">${isB?'Bundle':'Asset'}</span></td>
      <td><span class="badge ${e.earns?'cyes':'cno'}">${e.earns?'✓ นับ':'ไม่นับ'}</span></td>
      <td><div class="date-cell">${date}</div></td>
      <td><div class="time-cell">${time}</div></td>
    </tr>`
  }).join('')
}

async function openDetail(uid,name){
  const status=document.getElementById('status')
  document.getElementById('listView').style.display='none'
  document.getElementById('detailView').style.display='block'
  document.getElementById('backBtn').style.display='inline-block'
  document.getElementById('fromDate').value='';document.getElementById('toDate').value=''
  document.querySelectorAll('#detailView .qbtn').forEach(b=>b.classList.remove('active'))
  document.querySelector('#detailView .qbtn:last-child').classList.add('active')
  history.replaceState(null,'','#u='+uid)
  status.className='status';status.textContent='กำลังโหลดรายการของ '+name+'...'
  document.getElementById('detailBody').innerHTML=''
  try{
    const res=await fetch('/api/commission-detail?token='+encodeURIComponent(TOKEN)+'&uid='+uid)
    if(res.status===401||res.status===403){status.className='status err';status.textContent='ลิงก์ไม่ถูกต้อง';return}
    const data=await res.json()
    if(!res.ok){status.className='status err';status.textContent='Error: '+(data.message||res.status);return}
    _detailItems=data.entries||[];_detailName=name;_detailUid=uid;_detailCutoff=data.cutoffDate||null
    updateCutoffLabel()
    if(_detailCutoff){
      const d=new Date(_detailCutoff+'T00:00:00')
      d.setDate(d.getDate()+1)
      document.getElementById('fromDate').value=toISO(Math.floor(d.getTime()/1000))
      document.querySelectorAll('#detailView .qrow .qbtn').forEach(b=>b.classList.remove('active'))
    }
    renderDetail()
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
}

// เปิดตรงเข้ารายละเอียดได้ถ้ามี #u=<userId> ติดมาใน URL (เผื่อแชร์ลิงก์เฉพาะคน)
window.addEventListener('DOMContentLoaded',()=>{
  const m=location.hash.match(/^#u=(\\d+)/)
  if(m){openDetail(Number(m[1]),'ID '+m[1])}
  else{loadList()}
})
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[0]} {args[1]}")

    def _check_auth(self):
        return self.headers.get("X-Password", "") == PASSWORD

    def _check_view(self):
        return token_ok(self.headers.get("X-View-Token", ""))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/item-details":
            if not (self._check_auth() or self._check_view()):
                self._json(401, {"message": "Unauthorized"}); return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                items  = json.loads(body.decode())
                self._json(200, fetch_item_details(items))
            except Exception as e:
                self._json(500, {"message": str(e)})
        elif parsed.path == "/api/cutoff":
            p     = urllib.parse.parse_qs(parsed.query)
            token = (p.get("token") or [""])[0]
            if not (self._check_auth() or self._check_view() or token_ok(token)):
                self._json(401, {"message": "Unauthorized"}); return
            try:
                length   = int(self.headers.get("Content-Length", 0))
                body     = json.loads(self.rfile.read(length).decode())
                uid      = int(body.get("uid"))
                date_str = (body.get("date") or "").strip() or None
            except Exception as e:
                self._json(400, {"message": f"invalid body: {e}"}); return
            try:
                save_cutoff(uid, date_str)
                self._json(200, {"ok": True})
            except Exception as e:
                print(f"[save_cutoff {uid}] {e}")
                self._json(500, {"message": str(e)})
        else:
            self._json(404, {"message": "Not found"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/all/") and token_ok(parsed.path[5:]):
            b = ALL_HTML.replace("__VIEW_TOKEN__", VIEW_TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if parsed.path.startswith("/commission/") and token_ok(parsed.path[len("/commission/"):]):
            b = COMMISSION_HTML.replace("__VIEW_TOKEN__", VIEW_TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
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
            if not (self._check_auth() or self._check_view()):
                self._json(401, {"message": "Unauthorized"}); return
            self._api_all(parsed.query)
        elif parsed.path == "/api/commission":
            p     = urllib.parse.parse_qs(parsed.query)
            token = (p.get("token") or [""])[0]
            if not (self._check_auth() or self._check_view() or token_ok(token)):
                self._json(401, {"message": "Unauthorized"}); return
            from_s = (p.get("from") or [""])[0]
            to_s   = (p.get("to")   or [""])[0]
            from_ts = float(from_s) if from_s else None
            to_ts   = float(to_s)   if to_s   else None
            try:
                if from_ts or to_ts:
                    buyers = fetch_commission_data_ranged(from_ts, to_ts)
                else:
                    buyers = fetch_commission_data()
                cutoffs = load_cutoffs()
                # วันซื้อล่าสุดตลอดกาล (ไม่ขึ้นกับช่วงที่กรอง) -- ใช้ตัดสินว่าตัดยอดถึงปัจจุบันแล้วหรือยัง
                last_all = {str(x.get("userId")): x.get("lastTs") or 0 for x in fetch_commission_data()}
                for b in buyers:
                    b["cutoffDate"] = cutoffs.get(str(b.get("userId")))
                    b["lastTsAll"]  = max(last_all.get(str(b.get("userId")), 0), b.get("lastTs") or 0)
                add_outstanding_commission(buyers, cutoffs)
                self._json(200, {"buyers": buyers, "ranged": bool(from_ts or to_ts)})
            except Exception as e:
                print(f"[commission error] {e}")
                self._json(500, {"message": str(e)})
        elif parsed.path == "/api/commission-detail":
            p     = urllib.parse.parse_qs(parsed.query)
            token = (p.get("token") or [""])[0]
            if not (self._check_auth() or self._check_view() or token_ok(token)):
                self._json(401, {"message": "Unauthorized"}); return
            try:
                uid = int((p.get("uid") or ["0"])[0])
            except ValueError:
                self._json(400, {"message": "invalid uid"}); return
            self._api_commission_detail(uid)
        elif parsed.path == "/api/debug-list-keys":
            if not self._check_auth():
                self._json(401, {"message": "Unauthorized"}); return
            url = f"{BASE}/standard-datastores/datastore/entries?" + urllib.parse.urlencode(
                {"datastoreName": DS_NAME, "limit": 100, "prefix": "P_"}
            )
            req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._json(200, {"status": r.status, "body": json.loads(r.read().decode())})
            except urllib.error.HTTPError as e:
                self._json(200, {"status": e.code, "body": e.read().decode()})
            except Exception as e:
                self._json(200, {"status": None, "error": str(e)})
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

    def _api_commission_detail(self, uid):
        try:
            entries = fetch_history(uid)
            if entries is None:
                self._json(500, {"message": "DataStore error"}); return
            for e in entries:
                e["earns"] = False if uid == OWNER_ID else entry_earns_commission(e)
            # เติมชื่อ/ผู้สร้าง/thumbnail ให้ครบ (cache รายชิ้นอยู่แล้วใน fetch_item_details)
            details = fetch_item_details(entries) if entries else {}
            for e in entries:
                d = details.get(f"{e['tp']}_{e['id']}")
                if d:
                    e["nm"]    = d.get("name") or e.get("nm") or ""
                    e["cr"]    = d.get("creator", "")
                    e["thumb"] = d.get("thumb", "")
            cutoff = load_cutoffs().get(str(uid))
            self._json(200, {"entries": entries, "cutoffDate": cutoff})
        except Exception as e:
            print(f"[commission-detail error] {e}")
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
    print(f"\n Purchase History  →  http://localhost:{PORT}\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
