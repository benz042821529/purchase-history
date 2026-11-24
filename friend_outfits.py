"""
(2026-11-20) หน้าเว็บ "ชุดของเพื่อน" — เวอร์ชันเว็บของเมนู ชุดเพื่อน ในแมพ รวมมิจของtangsan
  /friends                → หน้าเว็บ (ใครก็ใช้ได้ ไม่ต้องใส่รหัส)
  /api/fo/search?q=        → หาผู้เล่นจาก ID / ชื่อผู้ใช้ / ชื่อคล้าย
  /api/fo/friends?uid=     → รายชื่อเพื่อน (สูงสุด 200 คนแรก ตามที่ Roblox ให้แบบไม่ล็อกอิน)
  /api/fo/person?uid=      → ชุดที่ใส่อยู่ + ชุดที่สร้างไว้ในบัญชี Roblox + ชุดที่เซฟในแมพ (DataStore PlayerOutfitCodes_v1)
  /api/fo/outfit?id=       → ไอเทมทีละชิ้นของชุดใน Roblox
  /api/fo/thumbs?kind=&ids= → รูปที่ยังโหลดไม่เสร็จ (Roblox ตอบ Pending) ให้หน้าเว็บถามซ้ำ
  POST /api/fo/items       → ชื่อ/ราคา/รูป ของไอเทม (ใช้กับชุดที่เซฟในแมพ)
เว็บเรียก roblox.com ได้ตรงๆ (ไม่ต้องผ่าน roproxy แบบในเกม) · ข้อมูลทั้งหมดเป็นข้อมูลสาธารณะ ไม่เขียนอะไรกลับเข้าเกม
"""
import json, time, threading, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MAP_DS_NAME = "PlayerOutfitCodes_v1"   # ตรงกับ SaveOutfitServer.CONFIG.OwnStoreName ในแมพ

# ฟังก์ชันจาก server.py (ตั้งผ่าน init กัน import วน)
_h = {}

def init(roblox_get, fetch_item_details, base, cache_get, cache_set, catalog_batch=None):
    _h.update(roblox_get=roblox_get, fetch_item_details=fetch_item_details, base=base,
              cache_get=cache_get, cache_set=cache_set, catalog_batch=catalog_batch)

# ---------------- จำกัดความถี่ต่อเครื่อง (กันคนยิงจน Roblox บล็อกเว็บ) ----------------
_rl_lock = threading.Lock()
_rl = {}
RL_PER_MIN = 90

def rate_ok(ip):
    now = time.time()
    with _rl_lock:
        t, n = _rl.get(ip, (now, 0))
        if now - t > 60:
            t, n = now, 0
        n += 1
        _rl[ip] = (t, n)
        if len(_rl) > 5000:
            for k in [k for k, v in _rl.items() if now - v[0] > 60]:
                _rl.pop(k, None)
        return n <= RL_PER_MIN

# ---------------- HTTP ----------------
class FoError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg); self.code = code; self.msg = msg

def _req(url, data=None, method=None, timeout=10):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode(); headers["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep((1.5 if e.code == 429 else 0.6) * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5); continue
    raise FoError(503, "Roblox ตอบช้า/คนใช้เยอะ ลองใหม่อีกครั้ง")

def _cached(key, ttl, fn):
    v = _h["cache_get"](("fo",) + key, ttl)
    if v is not None:
        return v
    v = fn()
    _h["cache_set"](("fo",) + key, v)
    return v

# ---------------- รูป ----------------
THUMB_URL = {
    "headshot": "https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={}&size=150x150&format=Png&isCircular=false",
    "avatar":   "https://thumbnails.roblox.com/v1/users/avatar?userIds={}&size=420x420&format=Png&isCircular=false",
    "outfit":   "https://thumbnails.roblox.com/v1/users/outfits?userOutfitIds={}&size=420x420&format=Png&isCircular=false",
    "asset":    "https://thumbnails.roblox.com/v1/assets?assetIds={}&size=150x150&format=Png&isCircular=false",
    "bundle":   "https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={}&size=150x150&format=Png&isCircular=false",
}

def thumbs(kind, ids):
    """{id: url} เฉพาะรูปที่เสร็จแล้ว · รูปที่เสร็จแล้วจำไว้ 1 ชม."""
    out, need = {}, []
    for i in ids:
        u = _h["cache_get"](("fothumb", kind, i), 3600)
        if u: out[i] = u
        else: need.append(i)
    step = 50 if kind == "outfit" else 100
    for k in range(0, len(need), step):
        chunk = need[k:k + step]
        try:
            d = _req(THUMB_URL[kind].format(",".join(str(x) for x in chunk)))
        except Exception as e:
            print(f"[fo thumbs {kind}] {e}"); continue
        for x in d.get("data", []):
            if x.get("state") == "Completed" and x.get("imageUrl"):
                tid = x.get("targetId")
                out[tid] = x["imageUrl"]
                _h["cache_set"](("fothumb", kind, tid), x["imageUrl"])
    return out

# ---------------- ผู้เล่น ----------------
def users_info(ids):
    """{id: {name, displayName}} (POST ทีละ 100)"""
    out = {}
    for k in range(0, len(ids), 100):
        try:
            d = _req("https://users.roblox.com/v1/users", {"userIds": ids[k:k + 100], "excludeBannedUsers": False})
            for u in d.get("data", []):
                out[u["id"]] = {"name": u.get("name", ""), "displayName": u.get("displayName") or u.get("name", "")}
        except Exception as e:
            print(f"[fo users] {e}")
    return out

def search_users(q):
    q = (q or "").strip().lstrip("@")
    if not q or len(q) > 40:
        raise FoError(400, "พิมพ์ ID หรือชื่อผู้เล่น")
    def go():
        found, order = {}, []
        def add(uid, name, disp):
            if uid and uid not in found:
                found[uid] = {"id": uid, "name": name, "displayName": disp or name}; order.append(uid)
        if q.isdigit():
            info = users_info([int(q)])
            if int(q) in info:
                add(int(q), info[int(q)]["name"], info[int(q)]["displayName"])
        else:
            try:
                d = _req("https://users.roblox.com/v1/usernames/users", {"usernames": [q], "excludeBannedUsers": False})
                for u in d.get("data", []):
                    add(u["id"], u.get("name"), u.get("displayName"))
            except Exception as e:
                print(f"[fo username] {e}")
            if len(q) >= 3:
                try:
                    d = _req("https://users.roblox.com/v1/users/search?" + urllib.parse.urlencode({"keyword": q, "limit": 10}))
                    for u in d.get("data", []):
                        add(u["id"], u.get("name"), u.get("displayName"))
                except Exception as e:
                    print(f"[fo search] {e}")
        rows = [found[i] for i in order][:12]
        th = thumbs("headshot", [r["id"] for r in rows])
        for r in rows: r["head"] = th.get(r["id"], "")
        return rows
    rows = _cached(("search", q.lower()), 300, go)
    if not rows:
        raise FoError(404, "ไม่เจอผู้เล่นชื่อนี้")
    return {"list": rows}

def friends(uid):
    def go():
        try:
            d = _req(f"https://friends.roblox.com/v1/users/{uid}/friends?userSort=0")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return {"blocked": True, "list": [], "count": None}
            raise
        ids = [f["id"] for f in d.get("data", []) if f.get("id")]
        info = users_info(ids)
        th = thumbs("headshot", ids)
        rows = []
        for i in ids:
            u = info.get(i, {})
            rows.append({"id": i, "name": u.get("name", ""), "displayName": u.get("displayName", ""), "head": th.get(i, "")})
        count = None
        try:
            count = _req(f"https://friends.roblox.com/v1/users/{uid}/friends/count").get("count")
        except Exception:
            pass
        return {"list": rows, "count": count}
    return _cached(("friends", uid), 300, go)

# ---------------- ประเภทไอเทม → หมวด (ไทย/อังกฤษ) ----------------
CAT = {
    "bundle": ("ชุดเซ็ต (Bundle)", "Bundles"),
    "hair":   ("ผม", "Hair"),        "head":   ("หมวก / หัว", "Head"),   "face":  ("หน้า", "Face"),
    "top":    ("เสื้อ", "Tops"),      "bottom": ("กางเกง / กระโปรง", "Bottoms"), "shoes": ("รองเท้า", "Shoes"),
    "acc":    ("เครื่องประดับ", "Accessories"), "body": ("ร่างกาย", "Body"), "anim": ("ท่าเดิน / ท่าทาง", "Animations"),
    "emote":  ("อีโมต", "Emotes"),   "other":  ("อื่นๆ", "Other"),
}
CAT_ORDER = ["bundle", "hair", "head", "face", "top", "bottom", "shoes", "acc", "body", "anim", "emote", "other"]
TYPE_CAT = {
    "HairAccessory": "hair", "Hat": "head", "Face": "face", "FaceAccessory": "face", "EyebrowAccessory": "face",
    "EyelashAccessory": "face", "MoodAnimation": "face", "DynamicHead": "body",
    "Shirt": "top", "TShirt": "top", "TShirtAccessory": "top", "ShirtAccessory": "top", "JacketAccessory": "top",
    "SweaterAccessory": "top", "DressSkirtAccessory": "bottom", "Pants": "bottom", "PantsAccessory": "bottom",
    "ShortsAccessory": "bottom", "LeftShoeAccessory": "shoes", "RightShoeAccessory": "shoes",
    "NeckAccessory": "acc", "ShoulderAccessory": "acc", "FrontAccessory": "acc", "BackAccessory": "acc",
    "WaistAccessory": "acc", "Gear": "acc",
    "Head": "body", "Torso": "body", "LeftArm": "body", "RightArm": "body", "LeftLeg": "body", "RightLeg": "body",
    "IdleAnimation": "anim", "WalkAnimation": "anim", "RunAnimation": "anim", "JumpAnimation": "anim",
    "FallAnimation": "anim", "ClimbAnimation": "anim", "SwimAnimation": "anim", "EmoteAnimation": "emote",
}
# ชื่อช่องของ AccessoryType ในแมพ (Enum.AccessoryType) → ชื่อแบบ assetType
ACC_TYPE = {
    "Hat": "Hat", "Hair": "HairAccessory", "Face": "FaceAccessory", "Neck": "NeckAccessory",
    "Shoulder": "ShoulderAccessory", "Front": "FrontAccessory", "Back": "BackAccessory", "Waist": "WaistAccessory",
    "TShirt": "TShirtAccessory", "Shirt": "ShirtAccessory", "Pants": "PantsAccessory", "Jacket": "JacketAccessory",
    "Sweater": "SweaterAccessory", "Shorts": "ShortsAccessory", "LeftShoe": "LeftShoeAccessory",
    "RightShoe": "RightShoeAccessory", "DressSkirt": "DressSkirtAccessory", "Eyebrow": "EyebrowAccessory",
    "Eyelash": "EyelashAccessory",
}

# ---------------- (2026-11-24) หมวดหลัก → หมวดย่อย แบบหน้า Inventory ของ Roblox (ใช้ทั้งเว็บและในเกม) ----------------
# key หมวดย่อย = "<หลัก>.<ย่อย>" · key หมวดหลักเฉยๆ = ทั้งหมดในหมวดนั้น
# (2026-11-24 v2) หมวดเหมือน "ของที่มี" ในร้านของแมพ (= หน้า Customize ของ Roblox) · bundle ขึ้นเป็นใบเดียว ไม่แยกชิ้นส่วน
# (key, ไทย, อังกฤษ, [ (subkey, ไทย, อังกฤษ, [assetTypes], [bundleTypes]) ])  · หมวดที่ไม่มีหมวดย่อย = ใส่ประเภทไว้ในแถว "" ตัวเดียว
INV_MAINS = [
    ("avatars", "อวาตาร์", "Avatars", [("", "", "", [], ["BodyParts", "DynamicHeadAvatar"])]),
    ("body", "ร่างกาย", "Body", [
        ("hair", "ผม", "Hair", ["HairAccessory"], []),
        ("heads", "หัว", "Heads", ["DynamicHead", "Head"], ["DynamicHead"]),
        ("faces", "หน้า", "Faces", ["Face"], []),
        # ส่วนร่างกาย/สีหน้า ส่วนใหญ่มาจาก bundle (ซ่อนอยู่แล้ว) → ไม่ทำเป็นหมวดย่อย แต่ยังนับใน "ทั้งหมด"
        ("", "", "", ["Torso", "LeftArm", "RightArm", "LeftLeg", "RightLeg", "MoodAnimation"], []),
    ]),
    ("clothing", "เสื้อผ้า", "Clothing", [
        ("shirts", "เสื้อ", "Shirts", ["ShirtAccessory"], []),
        ("tshirts", "เสื้อยืด", "T-Shirts", ["TShirtAccessory"], []),
        ("sweaters", "สเวตเตอร์", "Sweaters", ["SweaterAccessory"], []),
        ("jackets", "แจ็กเก็ต", "Jackets", ["JacketAccessory"], []),
        ("pants", "กางเกง", "Pants", ["PantsAccessory"], []),
        ("dresses", "เดรสและกระโปรง", "Dresses & Skirts", ["DressSkirtAccessory"], []),
        ("shorts", "กางเกงขาสั้น", "Shorts", ["ShortsAccessory"], []),
        ("shoes", "รองเท้า", "Shoes", ["LeftShoeAccessory", "RightShoeAccessory"], ["Shoes"]),
        ("cshirts", "เสื้อคลาสสิก", "Classic Shirts", ["Shirt"], []),
        ("ctshirts", "เสื้อยืดคลาสสิก", "Classic T-Shirts", ["TShirt"], []),
        ("cpants", "กางเกงคลาสสิก", "Classic Pants", ["Pants"], []),
    ]),
    ("accessories", "ไอเท็มเสริม", "Accessories", [
        ("head", "หัว", "Head", ["Hat"], []),
        ("face", "หน้า", "Face", ["FaceAccessory"], []),
        ("neck", "คอ", "Neck", ["NeckAccessory"], []),
        ("shoulder", "ไหล่", "Shoulder", ["ShoulderAccessory"], []),
        ("front", "ด้านหน้า", "Front", ["FrontAccessory"], []),
        ("back", "ด้านหลัง", "Back", ["BackAccessory"], []),
        ("waist", "เอว", "Waist", ["WaistAccessory"], []),
        ("gear", "Gear", "Gear", ["Gear"], []),
    ]),
    ("backgrounds", "พื้นหลัง", "Backgrounds", [("", "", "", ["AvatarBackground"], [])]),
    # แอนิเมชัน = ชุดท่าเดิน (bundle ใบเดียว) + ท่าเดี่ยวที่ไม่ได้อยู่ใน bundle · ไม่มีหมวดย่อย (เหมือนร้าน)
    ("animations", "แอนิเมชัน", "Animations", [("", "", "", ["IdleAnimation", "WalkAnimation", "RunAnimation", "JumpAnimation",
        "FallAnimation", "ClimbAnimation", "SwimAnimation"], ["Animations", "AvatarAnimations"])]),
    ("emotes", "อีโมต", "Emotes", [("", "", "", ["EmoteAnimation"], [])]),
    ("makeup", "แต่งหน้า", "Makeup", [
        ("eyes", "ตา", "Eyes", ["EyeMakeup"], []), ("lips", "ปาก", "Lips", ["LipMakeup"], []),
        ("face", "หน้า", "Face", ["FaceMakeup"], []), ("lashes", "ขนตา", "Eyelashes", ["EyelashAccessory"], []),
        ("brows", "คิ้ว", "Eyebrows", ["EyebrowAccessory"], []),
    ]),
]
INV_KEYS = {}      # key → ([assetTypes], [bundleTypes])
TYPE_SUB = {}      # assetType → key หมวดย่อย
BUNDLE_SUB = {}    # bundleType → key หมวดย่อย
for _m, _th, _en, _subs in INV_MAINS:
    INV_KEYS[_m] = ([t for _s in _subs for t in _s[3]], [t for _s in _subs for t in _s[4]])
    for _k, _sth, _sen, _at, _bt in _subs:
        _key = f"{_m}.{_k}" if _k else _m
        if _k: INV_KEYS[_key] = (_at, _bt)
        for _t in _at: TYPE_SUB.setdefault(_t, _key)
        for _t in _bt: BUNDLE_SUB.setdefault(_t, _key)
INV_META = {"mains": [{"k": m, "th": th, "en": en, "subs": [{"k": f"{m}.{k}", "th": sth, "en": sen} for k, sth, sen, _a, _b in subs if k]}
                      for m, th, en, subs in INV_MAINS]}

def _item(aid, atype, name=""):
    return {"id": int(aid), "type": atype or "", "cat": TYPE_CAT.get(atype or "", "other"),
            "sub": TYPE_SUB.get(atype or "", "other"), "name": name or ""}

def _assets_to_items(assets):
    out, seen = [], set()
    for a in assets or []:
        aid = a.get("id")
        if not aid or aid in seen: continue
        seen.add(aid)
        out.append(_item(aid, (a.get("assetType") or {}).get("name", ""), a.get("name", "")))
    return out

# ---------------- รวมชิ้นส่วนเป็น Bundle (2026-11-24) ----------------
# ชิ้นส่วนร่างกาย / ท่าเดิน / รองเท้า ซื้อแยกไม่ได้ → ถาม catalog ว่าชิ้นนี้อยู่ใน bundle ไหน แล้วยุบชิ้นที่อยู่ bundle เดียวกันเป็นแถวเดียว
PIECE_TYPES = {"Head", "Torso", "LeftArm", "RightArm", "LeftLeg", "RightLeg", "DynamicHead", "MoodAnimation",
               "IdleAnimation", "WalkAnimation", "RunAnimation", "JumpAnimation", "FallAnimation", "ClimbAnimation",
               "SwimAnimation", "LeftShoeAccessory", "RightShoeAccessory"}
BUNDLE_CAT = {"BodyParts": "body", "DynamicHead": "face", "DynamicHeadAvatar": "body", "Animations": "anim", "Shoes": "shoes"}

def _bundles_of(aid):
    def go():
        try:
            d = _req(f"https://catalog.roblox.com/v1/assets/{aid}/bundles?limit=10&sortOrder=Asc")
        except Exception as e:
            print(f"[fo bundles {aid}] {e}")
            return []
        out = []
        for b in d.get("data", []):
            ids = [it.get("id") for it in b.get("items", []) if it.get("type") == "Asset"]
            out.append({"id": b.get("id"), "name": b.get("name", ""), "type": b.get("bundleType", ""), "items": ids})
        return out
    return _cached(("abundles", aid), 86400, go)

def _norm(t):
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())

_NAME_OF = {}

def _name_hit(b, left):
    bn = _norm(b.get("name"))
    return 1 if bn and any(bn[:12] and _norm(_NAME_OF.get(i, "")).startswith(bn[:12]) for i in left) else 0

def merge_bundles(items):
    for it in items:
        if it.get("id"):
            _NAME_OF[it["id"]] = it.get("name", "")
    # รวมทุกชิ้นที่อยู่ใน bundle (ไม่จำกัดแค่ร่างกาย/ท่าเดิน/รองเท้า) — ตามที่ขอให้เว็บรวมทั้งหมด
    pieces = [it for it in items if it.get("id") and not it.get("bundle")]
    if not pieces:
        return items
    with ThreadPoolExecutor(max_workers=6) as ex:
        found = dict(zip([p["id"] for p in pieces], ex.map(_bundles_of, [p["id"] for p in pieces])))
    have = {p["id"] for p in pieces}
    cand = {}
    for pid, bl in found.items():
        for b in bl:
            if b.get("id"):
                c = cand.setdefault(b["id"], dict(b, cover=set()))
                c["cover"] |= {i for i in b["items"] if i in have}
    used, chosen = set(), []
    while True:
        best = None
        for b in cand.values():
            left = b["cover"] - used
            if not left:
                continue
            # เลือก bundle ที่คลุมได้มากที่สุดก่อน ถ้าเท่ากันเลือกอันที่ชื่อตรงกับชื่อชิ้น
            score = (len(left), _name_hit(b, left))
            if best is None or score > best[2]:
                best = (b, left, score)
        if not best:
            break
        used |= best[1]
        chosen.append(best[:2])
    if not chosen:
        return items
    out, placed = [], set()
    first_of = {}
    for b, left in chosen:
        for i in left:
            first_of[i] = b
    for it in items:
        b = first_of.get(it["id"])
        if b is None:
            out.append(it)
        elif b["id"] not in placed:
            placed.add(b["id"])
            out.append({"id": b["id"], "key": "b" + str(b["id"]), "bundle": True, "type": b["type"], "sub": BUNDLE_SUB.get(b["type"], "avatars"),
                        "cat": BUNDLE_CAT.get(b["type"], "bundle"), "name": b["name"], "pieces": len([i for i in first_of if first_of[i] is b])})
    return out

# ---------------- ชุดของคนหนึ่งคน ----------------
def _roblox_outfits(uid):
    """ชุดที่สร้างเก็บในบัญชี Roblox ทุกหน้า (ไม่ใช่แค่ 50 ชุดแรก) — สูงสุด 12 หน้า"""
    out, seen, err = [], set(), None
    token, pages = None, 0
    while pages < 12:
        pages += 1
        q = {"itemsPerPage": 50, "isEditable": "true"}
        if token:
            q["paginationToken"] = token
        try:
            d = _req(f"https://avatar.roblox.com/v2/avatar/users/{uid}/outfits?" + urllib.parse.urlencode(q))
        except Exception as e:
            err = str(e); break
        added = 0
        for o in d.get("data", []):
            if o.get("id") and o.get("isEditable", True) is not False and o["id"] not in seen:
                seen.add(o["id"]); out.append({"id": o["id"], "name": o.get("name", "")}); added += 1
        token = d.get("paginationToken")
        if not token or not added:
            break
    if not out:  # สำรอง: API v1
        try:
            d = _req(f"https://avatar.roblox.com/v1/users/{uid}/outfits?page=1&itemsPerPage=50&isEditable=true")
            out = [{"id": o["id"], "name": o.get("name", "")} for o in d.get("data", [])
                   if o.get("id") and o.get("isEditable", True) is not False]
        except Exception as e:
            err = err or str(e)
    return out, (None if out else err)

def _map_outfits(uid):
    """ชุดที่ผู้เล่นเซฟไว้ในแมพ (อ่านผ่าน Open Cloud ด้วย API_KEY ตัวเดียวกับหน้าประวัติการซื้อ)"""
    try:
        data = _h["roblox_get"](f"{_h['base']}/standard-datastores/datastore/entries/entry",
                                {"datastoreName": MAP_DS_NAME, "entryKey": f"Player_{uid}"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], None
        if e.code in (401, 403):
            return [], "noperm"
        return [], f"HTTP {e.code}"
    except Exception as e:
        return [], str(e)
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict): continue
        items = []
        def add(aid, atype):
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                return
            if aid > 0 and all(x["id"] != aid for x in items):
                items.append(_item(aid, atype))
        for acc in e.get("accessories") or []:
            if isinstance(acc, dict):
                add(acc.get("AssetId"), ACC_TYPE.get(acc.get("AccessoryType", ""), "Hat"))
        add(e.get("shirt"), "Shirt"); add(e.get("pants"), "Pants")
        add(e.get("graphicTShirt"), "TShirt"); add(e.get("face"), "Face")
        for part, aid in (e.get("bodyParts") or {}).items():
            add(aid, "DynamicHead" if part == "Head" else part)
        for field, aid in (e.get("animations") or {}).items():
            add(aid, field)
        items = merge_bundles(items)
        out.append({
            "code": e.get("code", ""), "name": e.get("name") or e.get("outfitName") or e.get("code") or "",
            "savedAt": e.get("savedAt") or e.get("createdAt") or e.get("ts") or 0, "items": items,
        })
    out.sort(key=lambda x: x["savedAt"] or 0, reverse=True)
    return out, None

def person(uid):
    def go():
        info = users_info([uid]).get(uid)
        if not info:
            raise FoError(404, "ไม่เจอผู้เล่นนี้")
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_av = ex.submit(_avatar, uid)
            f_ro = ex.submit(_roblox_outfits, uid)
            try:
                av = f_av.result()
            except Exception as e:
                print(f"[fo avatar {uid}] {e}"); av = None
            ro, ro_err = f_ro.result()
            mp, mp_err = [], None  # (2026-11-24) เอาชุดที่เซฟในแมพออกจากหน้าเว็บ (ผู้ใช้ขอ)
        av_err = av is None
        av = av or {}
        wearing = merge_bundles(_assets_to_items(av.get("assets")))
        for e in av.get("emotes") or []:
            if e.get("assetId"):
                wearing.append(_item(e["assetId"], "EmoteAnimation", e.get("assetName", "")))
        private = False
        if not ro:
            try:
                private = _req(f"https://inventory.roblox.com/v1/users/{uid}/can-view-inventory").get("canView") is False
            except Exception:
                pass
        th_av = thumbs("avatar", [uid]); th_hd = thumbs("headshot", [uid])
        th_of = thumbs("outfit", [o["id"] for o in ro])
        for o in ro: o["img"] = th_of.get(o["id"], "")
        return {
            "id": uid, "name": info["name"], "displayName": info["displayName"],
            "head": th_hd.get(uid, ""), "avatarImg": th_av.get(uid, ""),
            "wearing": wearing, "avatarType": av.get("playerAvatarType", ""),
            "robloxOutfits": ro, "robloxErr": None if ro else ro_err, "robloxPrivate": private,
            "mapOutfits": mp, "mapErr": mp_err, "wearErr": av_err,
        }
    # (2026-11-24) ถ้าดึงชุดที่ใส่อยู่ไม่สำเร็จ อย่าจำผลว่างไว้ — ครั้งหน้าดึงใหม่
    v = _h["cache_get"](("fo", "person", uid), 180)
    if v is not None:
        return v
    v = go()
    if not v.get("wearErr"):
        _h["cache_set"](("fo", "person", uid), v)
    return v

def _avatar(uid):
    """ชุดที่ใส่อยู่: ลอง v1 ก่อน ไม่ได้ลอง v2 (Roblox ทยอยเลิก v1)"""
    last = None
    for url in (f"https://avatar.roblox.com/v1/users/{uid}/avatar",
                f"https://avatar.roblox.com/v2/avatar/users/{uid}/avatar"):
        try:
            d = _req(url)
            if isinstance(d, dict) and "assets" in d:
                return d
        except Exception as e:
            last = e
    raise last or FoError(502, "avatar")

def outfit(oid):
    def go():
        try:
            d = _req(f"https://avatar.roblox.com/v1/outfits/{oid}/details")
        except urllib.error.HTTPError as e:
            raise FoError(404 if e.code in (400, 404) else 502, "เปิดชุดนี้ไม่ได้ (อาจถูกลบไปแล้ว)")
        return {"id": oid, "name": d.get("name", ""), "items": merge_bundles(_assets_to_items(d.get("assets")))}
    return _cached(("outfit", oid), 600, go)

def items(ids):
    keys = [str(i) for i in ids][:80]
    req, back = [], {}
    for k in keys:
        if k.startswith("b") and k[1:].isdigit():
            req.append({"id": int(k[1:]), "tp": "B"}); back[k] = f"B_{k[1:]}"
        elif k.isdigit():
            req.append({"id": int(k), "tp": "A"}); back[k] = f"A_{k}"
    # (2026-11-24) ดึงรายละเอียด + สถานะการขาย พร้อมกัน (เดิมทำทีละอย่าง = ช้าเท่าตัว)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_st = ex.submit(_sale_status, req)
        det = _h["fetch_item_details"](req)
        try:
            st_pre = f_st.result()
        except Exception as e:
            print(f"[fo sale status] {e}"); st_pre = {}
    out = {k: dict(det.get(v, {})) for k, v in back.items()}
    # (2026-11-24) ราคาให้ตรงความจริง: ของลิมิเต็ด/ของที่เลิกขาย Roblox ยังส่ง "ราคาเดิม" มา
    # → ลิมิเต็ดที่ซื้อจาก Roblox ไม่ได้แล้ว ใช้ราคาขายต่อถูกสุด (resale) · ของที่เลิกขาย = offsale
    try:
        st = st_pre
        for k, v in back.items():
            s2 = st.get(v)
            if not s2: continue
            d = out[k]
            if s2.get("resale"):
                d["price"] = s2["resale"]; d["resale"] = True
            elif s2.get("offsale"):
                d["offsale"] = True
                if s2.get("limited"): d["price"] = 0
    except Exception as e:
        print(f"[fo sale status] {e}")
    return out

_LIM = {"Limited", "LimitedUnique", "Collectible"}

def _sale_status(req):
    """{ "A_id"|"B_id": {offsale, limited, resale} } จาก catalog (cache 10 นาที ต่อชิ้น)"""
    res, need = {}, []
    for r in req:
        key = f"{r['tp']}_{r['id']}"
        c = _h["cache_get"](("fo", "sale2", key), 600)
        if c is not None: res[key] = c
        else: need.append(r)
    cb = _h.get("catalog_batch")
    if not need or not cb:
        return res
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        try:
            found = cb([(r["id"], r["tp"]) for r in chunk])
        except Exception as e:
            print(f"[fo sale batch] {e}"); continue
        for r in chunk:
            key = f"{r['tp']}_{r['id']}"
            ci = found.get((r["tp"], r["id"]))
            if not ci: continue
            restr = set(ci.get("itemRestrictions") or [])
            limited = bool(restr & _LIM)
            legacy = bool(restr & {"Limited", "LimitedUnique"})  # ลิมิเต็ดยุคเก่า: Roblox ไม่ขายแล้ว ซื้อได้แค่ขายต่อ
            status = str(ci.get("priceStatus") or "")
            units = ci.get("unitsAvailableForConsumption")
            off = legacy or bool(ci.get("isOffSale")) or status == "Off Sale" or (limited and units == 0)
            lowest = ci.get("lowestResalePrice") or ci.get("lowestPrice") or 0
            info = {"offsale": off, "limited": limited, "resale": 0}
            if limited and off and lowest:
                info["resale"] = int(lowest)
            res[key] = info
            _h["cache_set"](("fo", "sale2", key), info)
    return res

def merge_map(idx_items):
    return merge_bundles(idx_items)

# ---------------- (2026-11-24) ไอเทมทั้งหมดที่ผู้เล่นมี (คลัง) แยกหมวด ----------------
# GET /api/fo/inv?uid=&cat=&cursor=  → { items, next, private }  ทีละ 100 ชิ้น · คลังส่วนตัว = private: true
def _inv_assets(uid, types, cursor):
    q = {"assetTypes": ",".join(types), "limit": 100, "sortOrder": "Desc"}
    if cursor: q["cursor"] = cursor
    d = _req(f"https://inventory.roblox.com/v2/users/{uid}/inventory?" + urllib.parse.urlencode(q))
    items = []
    for a in d.get("data") or []:
        aid = a.get("assetId")
        if aid:
            items.append(_item(aid, a.get("assetType") or "", a.get("name") or a.get("assetName") or ""))
    return items, d.get("nextPageCursor")

PIECE_TYPES_INV = set(PIECE_TYPES)  # ชิ้นที่มักมาจาก bundle (หัว/ตัว/แขนขา/ท่าเดิน/รองเท้า/สีหน้า) → ซ่อนถ้าอยู่ใน bundle ที่มีแล้ว

def _user_bundles(uid):
    """bundle ทั้งหมดที่คนนี้มี (สูงสุด 1,000) จำไว้ 10 นาที — ใช้ทั้งหมวดชุดเซ็ตและการซ่อนชิ้นส่วน (ยิง catalog น้อยลง กันโดนจำกัดความถี่)"""
    def go():
        out, cur = [], ""
        for _ in range(10):
            q = {"limit": 100, "sortOrder": "Desc"}
            if cur: q["cursor"] = cur
            d = _req(f"https://catalog.roblox.com/v1/users/{uid}/bundles?" + urllib.parse.urlencode(q))
            out += [{"id": b["id"], "name": b.get("name", ""), "type": b.get("bundleType", "")} for b in d.get("data") or [] if b.get("id")]
            cur = d.get("nextPageCursor")
            if not cur: break
        return out
    return _cached(("ubundles", uid), 600, go)

def _bundle_piece_ids(uid):
    """รหัสชิ้นส่วนทุกชิ้นที่อยู่ใน bundle ที่คนนี้มี → ใช้ซ่อนชิ้นซ้ำในหมวดร่างกาย/ท่าเดิน/รองเท้า/หน้า (bundle ขึ้นเป็นใบเดียวในหมวดชุดเซ็ตแล้ว)"""
    def go():
        bids = [b["id"] for b in _user_bundles(uid)]
        ids = set()
        for k in range(0, len(bids), 50):
            try:
                det = _req("https://catalog.roblox.com/v1/bundles/details?bundleIds=" + ",".join(str(x) for x in bids[k:k + 50]))
            except Exception as e:
                print(f"[fo inv bundles {uid}] {e}"); continue
            for b in det if isinstance(det, list) else det.get("data") or []:
                for it in b.get("items") or []:
                    if it.get("type") == "Asset" and it.get("id"):
                        ids.add(int(it["id"]))
        return sorted(ids)
    try:
        return set(_cached(("invpieces", uid), 600, go))
    except Exception as e:
        print(f"[fo inv pieces {uid}] {e}")
        return set()

def inventory(uid, cat, cursor):
    if cat not in INV_KEYS:
        raise FoError(400, "หมวดไม่ถูกต้อง")
    atypes, btypes = INV_KEYS[cat]
    cursor = (cursor or "")[:600]
    def go():
        try:
            items, nxt, hidden = [], None, 0
            # bundle ขึ้นเป็นใบเดียว (หน้าแรกเท่านั้น) — ชิ้นส่วนของ bundle ที่มีจะถูกซ่อนด้านล่าง
            if btypes and not cursor:
                for b in _user_bundles(uid):
                    if b["type"] in btypes:
                        items.append({"id": b["id"], "key": "b" + str(b["id"]), "bundle": True, "type": b["type"], "cat": "bundle",
                                      "sub": BUNDLE_SUB.get(b["type"], cat), "name": b["name"]})
            if atypes:
                got, nxt = _inv_assets(uid, atypes, cursor)
                if got and any(t in PIECE_TYPES_INV for t in atypes):
                    pieces = _bundle_piece_ids(uid)
                    keep = [it for it in got if not (it["type"] in PIECE_TYPES_INV and it["id"] in pieces)]
                    hidden = len(got) - len(keep)
                    got = keep
                items += got
            return {"items": items, "next": nxt, "private": False, "hidden": hidden}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"items": [], "next": None, "private": True, "hidden": 0}
            raise
    return _cached(("inv3", uid, cat, cursor), 300, go)

# ---------------- เส้นทาง ----------------
def handle_get(path, query):
    """คืน (status, dict) หรือ None ถ้าไม่ใช่เส้นทางของหน้านี้"""
    p = urllib.parse.parse_qs(query)
    g = lambda k: (p.get(k) or [""])[0].strip()
    def num(k):
        v = g(k)
        if not v.isdigit() or len(v) > 20:
            raise FoError(400, "ID ไม่ถูกต้อง")
        return int(v)
    try:
        if path == "/api/fo/search":  return 200, search_users(g("q"))
        if path == "/api/fo/friends": return 200, friends(num("uid"))
        if path == "/api/fo/person":  return 200, person(num("uid"))
        if path == "/api/fo/outfit":  return 200, outfit(num("id"))
        if path == "/api/fo/inv":     return 200, inventory(num("uid"), g("cat"), g("cursor"))
        if path == "/api/fo/invmeta": return 200, INV_META
        if path == "/api/fo/rawsale":  # (2026-11-24) ไว้ตรวจราคา: ข้อมูลดิบจาก catalog ของไอเทมเดียว
            iid = int(g("id") or 0); tp = "B" if g("tp") == "B" else "A"
            cb = _h.get("catalog_batch")
            ci = cb([(iid, tp)]).get((tp, iid)) if cb and iid else None
            return 200, {"catalog": ci, "status": _sale_status([{"id": iid, "tp": tp}]).get(f"{tp}_{iid}")}
        if path == "/api/fo/thumbs":
            kind = g("kind")
            if kind not in THUMB_URL: raise FoError(400, "kind")
            ids = [int(x) for x in g("ids").split(",") if x.isdigit()][:100]
            return 200, {str(k): v for k, v in thumbs(kind, ids).items()}
    except FoError as e:
        return e.code, {"message": e.msg}
    except Exception as e:
        print(f"[fo {path}] {e}")
        return 502, {"message": "ดึงข้อมูลจาก Roblox ไม่สำเร็จ ลองใหม่อีกครั้ง"}
    return None

def handle_post(path, body):
    if path != "/api/fo/items":
        return None
    try:
        return 200, items((body or {}).get("ids") or [])
    except Exception as e:
        print(f"[fo items] {e}")
        return 502, {"message": "ดึงข้อมูลไอเทมไม่สำเร็จ"}

# ---------------- หน้าเว็บ ----------------
FO_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ดูชุดผู้เล่น · รวมมิจของtangsan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Mitr:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#17181c;--panel:#212228;--card:#2a2b33;--card2:#32333c;--ink:#f3f1f7;--sub:#a9a4bd;--pink:#f07aaa;--lilac:#b9a2f0;--green:#6ee69a;--gold:#ffd27a;--line:#3a3b45}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:'Mitr',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;gap:12px;margin-bottom:18px}
header h1{font-size:22px;font-weight:600;margin:0;flex:1}
header h1 small{display:block;font-size:13px;font-weight:400;color:var(--sub);margin-top:2px}
.lang{display:flex;background:var(--panel);border-radius:999px;padding:3px}
.lang button{border:0;background:none;color:var(--sub);font:inherit;font-size:13px;padding:5px 12px;border-radius:999px;cursor:pointer}
.lang button.on{background:var(--lilac);color:#241b38}
.search{display:flex;gap:8px;background:var(--panel);padding:10px;border-radius:16px}
.search input{flex:1;min-width:0;background:var(--card);border:1px solid var(--line);color:var(--ink);font:inherit;font-size:16px;padding:12px 14px;border-radius:12px;outline:none}
.search input:focus{border-color:var(--lilac)}
.btn{border:0;font:inherit;font-weight:500;cursor:pointer;border-radius:12px;padding:12px 18px;background:var(--pink);color:#2a1220}
.btn.ghost{background:var(--card2);color:var(--ink);text-decoration:none;white-space:nowrap}
.btn:disabled{opacity:.5;cursor:default}
.msg{color:var(--sub);text-align:center;padding:30px 10px;font-size:15px}
.msg.err{color:#ff8a95}
.results{display:grid;gap:8px;margin-top:12px}
.res{display:flex;align-items:center;gap:12px;background:var(--panel);padding:10px;border-radius:14px;cursor:pointer;border:1px solid transparent}
.res:hover{border-color:var(--lilac)}
.res img{width:52px;height:52px;border-radius:50%;background:var(--card)}
.res b{display:block;font-weight:500}.res span{color:var(--sub);font-size:13px}
.person{display:flex;align-items:center;gap:14px;background:var(--panel);padding:14px;border-radius:16px;margin-top:14px}
.person img{width:64px;height:64px;border-radius:50%;background:var(--card)}
.person .nm{flex:1;min-width:0}.person .nm b{font-size:19px;font-weight:600;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.person .nm span{color:var(--sub);font-size:13px}
.tabs{display:flex;gap:6px;margin:14px 0;flex-wrap:wrap}
.tab{border:0;font:inherit;font-size:14px;cursor:pointer;padding:9px 16px;border-radius:999px;background:var(--panel);color:var(--sub)}
.tab.on{background:var(--lilac);color:#241b38;font-weight:500}
.wear{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:14px}
@media(max-width:720px){.wear{grid-template-columns:1fr}}
.avatar{background:var(--panel);border-radius:16px;padding:10px;text-align:center;align-self:start;position:sticky;top:10px}
@media(max-width:720px){.avatar{position:static}}
.avatar img{width:100%;max-width:300px;aspect-ratio:1;object-fit:contain}
.avatar .sum{color:var(--sub);font-size:13px;margin-top:6px}
.cat{margin-bottom:14px}
.cat h3{font-size:14px;font-weight:500;color:var(--lilac);margin:0 0 8px}
.items{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--isz,110px),1fr));gap:6px}
.cats{display:flex;flex-wrap:wrap;gap:12px 18px;align-items:flex-start}
.cats .cat{margin:0;flex:0 1 auto;max-width:100%}
.cats .items{display:flex;flex-wrap:wrap}
.cats .item{width:var(--isz,110px)}
.item{background:var(--panel);border-radius:12px;padding:6px;display:flex;flex-direction:column;gap:4px;text-decoration:none}
.item:hover{outline:1px solid var(--lilac)}
.item .th{background:var(--card);border-radius:10px;aspect-ratio:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
.item .th img{width:100%;height:100%;object-fit:contain}
.item .n{font-size:12px;line-height:1.25;height:2.5em;overflow:hidden}
.item .p,.rowi .p{font-size:12px;font-weight:500;color:var(--gold);white-space:nowrap}
.item .p .rs{font-size:10px;font-weight:400;color:var(--sub)}
.item .p.free{color:var(--green)}.item .p.off{color:var(--sub);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--osz,130px),1fr));gap:8px}
.of{background:var(--panel);border-radius:12px;padding:6px;cursor:pointer;border:1px solid transparent}
.of:hover{border-color:var(--lilac)}
.of .th{background:var(--card);border-radius:12px;aspect-ratio:1;overflow:hidden;display:flex;align-items:center;justify-content:center}
.of .th img{width:100%;height:100%;object-fit:contain}
.of .collage{display:grid;grid-template-columns:1fr 1fr;width:100%;height:100%}
.of .collage img{width:100%;height:100%;object-fit:contain}
.of .t{font-size:13px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.of .s{font-size:12px;color:var(--sub)}
.more{display:block;margin:14px auto 0}
.bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
.chip{border:0;font:inherit;font-size:12px;cursor:pointer;padding:6px 12px;border-radius:999px;background:var(--panel);color:var(--sub)}
.chip.on{background:var(--lilac);color:#241b38}
.chip.main{font-size:13px;padding:8px 16px;font-weight:500}.chip.main.on{background:var(--pink);color:#2a1220}
.bar.sub{margin-top:-4px}.bar.sub .chip{background:var(--card)}.bar.sub .chip.on{background:var(--lilac)}
.seg{display:inline-flex;background:var(--panel);border-radius:999px;padding:2px}
.seg button{border:0;font:inherit;font-size:12px;cursor:pointer;padding:5px 11px;border-radius:999px;background:none;color:var(--sub);min-width:30px}
.seg button.on{background:var(--lilac);color:#241b38}
.seg button:disabled{opacity:.35;cursor:default}
.seg button:not(:disabled):hover{color:var(--ink)}
.sp{flex:1}.cnt{color:var(--sub);font-size:12px}
.rows{display:grid;gap:4px}
.rowi{display:flex;gap:10px;align-items:center;background:var(--panel);border-radius:10px;padding:5px 10px 5px 5px;text-decoration:none;cursor:pointer;border:1px solid transparent}
.rowi:hover{border-color:var(--lilac)}
.rowi .th{width:var(--rsz,40px);height:var(--rsz,40px);flex:none;background:var(--card);border-radius:8px;overflow:hidden}
.rowi .th img{width:100%;height:100%;object-fit:contain}
.rowi .n{flex:1;min-width:0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rowi .n span{color:var(--sub);font-size:12px}
.rowi .p.off{color:var(--sub);font-weight:400}
.item .by{font-size:11px;color:var(--lilac);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{background:var(--panel);border-radius:12px;padding:12px 14px;color:var(--sub);font-size:14px}
.modal{position:fixed;inset:0;background:rgba(10,10,14,.75);display:none;align-items:flex-start;justify-content:center;padding:24px 12px;overflow:auto;z-index:10}
.modal.on{display:flex}
.box{background:var(--bg);border:1px solid var(--line);border-radius:18px;width:100%;max-width:900px;padding:16px}
.box .hd{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.box .hd b{flex:1;font-size:18px;font-weight:600}
.box .hd .x{width:36px;height:36px;border-radius:50%;border:0;background:var(--pink);color:#fff;font-size:18px;cursor:pointer}
.total{color:var(--sub);font-size:14px;margin-bottom:12px}.total b{color:var(--gold)}
footer{color:var(--sub);font-size:12px;text-align:center;margin-top:30px}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1 id="ttl">👕 ดูชุดผู้เล่น Roblox<small id="sub1">พิมพ์ ID หรือชื่อผู้เล่น → เห็นชุดที่ใส่อยู่ ไอเทมทั้งหมด และชุดที่สร้างไว้</small></h1>
  <div class="lang"><button data-l="th">ไทย</button><button data-l="en">EN</button></div>
</header>
<form class="search" id="f">
  <input id="q" autocomplete="off" placeholder="ID / @username">
  <button class="btn" id="go">ค้นหา</button>
</form>
<div id="out"></div>
<footer id="foot">ข้อมูลจาก Roblox (สาธารณะ)</footer>
</div>
<div class="modal" id="m"><div class="box" id="mb"></div></div>
<script>
const T={
 th:{ttl:'👕 ดูชุดผู้เล่น Roblox',sub:'พิมพ์ ID หรือชื่อผู้เล่น → เห็นชุดที่ใส่อยู่ ไอเทมทั้งหมด และชุดที่สร้างไว้',ph:'ID / @ชื่อผู้เล่น',go:'ค้นหา',
  loading:'กำลังโหลด...',pick:'เจอหลายคน เลือกคนที่ใช่',wearing:'ใส่อยู่ตอนนี้',roblox:'ชุดใน Roblox',map:'ชุดที่เซฟในแมพ',
  items:'ชิ้น',total:'ราคารวมชิ้นที่ยังขาย',free:'ฟรี',off:'ไม่ขาย',profile:'โปรไฟล์ Roblox',showMore:'แสดงเพิ่ม',
  noRoblox:'ไม่มีชุดที่สร้างเก็บไว้ใน Roblox',priv:'🔒 เจ้าตัวปิดการดูไอเทม/ชุดไว้ ดูได้แค่ชุดที่ใส่อยู่',
  noMap:'ยังไม่เคยเซฟชุดในแมพ',inv:'ไอเทมทั้งหมด',invPriv:'🔒 เจ้าตัวปิดการดูคลังไอเทมไว้ เลยดูไอเทมทั้งหมดไม่ได้',invEmpty:'ไม่มีไอเทมในหมวดนี้',invHint:'ไอเทมที่อยู่ในคลังของคนนี้ เรียงจากที่ได้มาล่าสุด',invHid:'ซ่อนชิ้นส่วนที่อยู่ในชุดเซ็ตแล้ว',mapPerm:'ยังเปิดดูชุดในแมพไม่ได้ (ต้องเพิ่มสิทธิ์ API key ให้อ่าน DataStore PlayerOutfitCodes_v1)',
  code:'โค้ด',saved:'เซฟเมื่อ',sDef:'ค่าเริ่มต้น',by:'โดย',sNew:'ล่าสุด',sPd:'ราคา ↓',sPa:'ราคา ↑',sNa:'ชื่อ ก-ฮ',sNz:'ชื่อ ฮ-ก',grp:'แยกหมวด',wearErr:'Roblox ตอบช้า กำลังลองดึงชุดที่ใส่อยู่ใหม่อัตโนมัติ...',wearFail:'ยังดึงชุดที่ใส่อยู่ไม่ได้ ลองกดค้นหาอีกครั้งภายหลัง',resale:'ขายต่อ',resaleTip:'ลิมิเต็ด ซื้อจาก Roblox ไม่ได้แล้ว ราคานี้คือราคาขายต่อที่ถูกที่สุด',offsale:'ไม่วางขาย',flat:'รวม',grid:'ตาราง',list:'รายการ',smaller:'เล็กลง',bigger:'ใหญ่ขึ้น',outfits:'ชุด',fail:'โหลดไม่สำเร็จ ลองใหม่อีกครั้ง',back:'ค้นหาใหม่',
  foot:'ข้อมูลจาก Roblox (สาธารณะ)',
  cats:{bundle:'ชุดเซ็ต (Bundle)',hair:'ผม',head:'หมวก / หัว',face:'หน้า',top:'เสื้อ',bottom:'กางเกง / กระโปรง',shoes:'รองเท้า',acc:'เครื่องประดับ',body:'ร่างกาย',anim:'ท่าเดิน / ท่าทาง',emote:'อีโมต',other:'อื่นๆ'}},
 en:{ttl:'👕 Roblox Outfit Viewer',sub:"Type an ID or username → see what they're wearing, all their items and their saved Roblox outfits",ph:'ID / @username',go:'Search',
  loading:'Loading...',pick:'Several players found — pick one',wearing:'Wearing now',roblox:'Roblox outfits',map:'Saved in map',
  items:'items',total:'Total for items still on sale',free:'Free',off:'Off sale',profile:'Roblox profile',showMore:'Show more',
  noRoblox:'No outfits saved on their Roblox account',priv:'🔒 This player hides their inventory — only the current outfit is visible',
  noMap:'No outfits saved in the map yet',inv:'All items',invPriv:'🔒 This player hides their inventory, so their items can\'t be shown',invEmpty:'No items in this category',invHint:'Items in this player\'s inventory, newest first',invHid:'pieces already shown under Bundles were hidden',mapPerm:'Map outfits not available yet (the API key needs read access to DataStore PlayerOutfitCodes_v1)',
  code:'Code',saved:'Saved',sDef:'Default',by:'by',sNew:'Newest',sPd:'Price ↓',sPa:'Price ↑',sNa:'Name A-Z',sNz:'Name Z-A',grp:'By category',wearErr:'Roblox is slow — retrying automatically...',wearFail:'Still couldn\'t load what they\'re wearing — try searching again later',resale:'resale',resaleTip:'Limited — no longer sold by Roblox; lowest resale price',offsale:'Off sale',flat:'All together',grid:'Grid',list:'List',smaller:'Smaller',bigger:'Bigger',outfits:'outfits',fail:'Could not load — please try again',back:'New search',
  foot:'Data from Roblox (public)',
  cats:{bundle:'Bundles',hair:'Hair',head:'Hats / Head',face:'Face',top:'Tops',bottom:'Bottoms',shoes:'Shoes',acc:'Accessories',body:'Body',anim:'Animations',emote:'Emotes',other:'Other'}}
};
const ORDER=['bundle','hair','head','face','top','bottom','shoes','acc','body','anim','emote','other'];
let L=(()=>{try{return localStorage.getItem('fo_lang')||'th'}catch(e){return 'th'}})();
const $=s=>document.querySelector(s), out=$('#out'), modal=$('#m'), mb=$('#mb');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=k=>T[L][k];
const itemCache={};
let person=null, tab='wearing', shown=60, invCat='avatars', inv={};
const INVMETA=__INVMETA__;
const SUBLBL={};INVMETA.mains.forEach(m=>{SUBLBL[m.k]=m;m.subs.forEach(x=>{SUBLBL[x.k]=Object.assign({main:m},x)})});
const SUBORDER=[];INVMETA.mains.forEach(m=>{m.subs.forEach(x=>SUBORDER.push(x.k));SUBORDER.push(m.k)});SUBORDER.push('other');
const lbl=o=>o?(L==='en'?o.en:o.th):'';
const SIZES={i:[84,110,140,180],o:[96,130,165,210],r:[30,40,56,72]};
const UI0={sort:'def',grp:true,view:'grid',size:1};
let ui=(()=>{try{return Object.assign({},UI0,JSON.parse(localStorage.getItem('fo_ui')||'{}'))}catch(e){return Object.assign({},UI0)}})();
function saveUi(){try{localStorage.setItem('fo_ui',JSON.stringify(ui))}catch(e){}}
function applySize(){document.body.style.setProperty('--isz',SIZES.i[ui.size]+'px');document.body.style.setProperty('--osz',SIZES.o[ui.size]+'px');document.body.style.setProperty('--rsz',SIZES.r[ui.size]+'px');document.querySelectorAll('[data-z]').forEach(b=>b.disabled=(b.dataset.z==='-1'&&ui.size===0)||(b.dataset.z==='1'&&ui.size===3))}
const kOf=it=>String(it.key||it.id);
const prOf=it=>(itemCache[kOf(it)]||{}).price||0;
const nmOf=it=>String((itemCache[kOf(it)]||{}).name||it.name||'').toLowerCase();
function sortBy(list,kind,nameFn){
  const a=list.slice(), s=ui.sort;
  if(kind==='items'&&s==='pd') a.sort((x,y)=>prOf(y)-prOf(x));
  else if(kind==='items'&&s==='pa') a.sort((x,y)=>(prOf(x)||1e9)-(prOf(y)||1e9));
  else if(s==='na') a.sort((x,y)=>nameFn(x).localeCompare(nameFn(y),L));
  else if(s==='nz') a.sort((x,y)=>nameFn(y).localeCompare(nameFn(x),L));
  return a;
}
function barHtml(kind,extra){
  const sorts=[['def',kind==='map'?t('sNew'):t('sDef')]].concat(kind==='items'?[['pd',t('sPd')],['pa',t('sPa')]]:[]).concat([['na',t('sNa')],['nz',t('sNz')]]);
  const cur=sorts.some(x=>x[0]===ui.sort)?ui.sort:'def';
  let h='<div class="bar">'+sorts.map(([k,l])=>'<button class="chip'+(cur===k?' on':'')+'" data-s="'+k+'">'+esc(l)+'</button>').join('');
  if(kind==='items') h+='<span class="seg"><button data-g="1" class="'+(ui.grp?'on':'')+'">'+esc(t('grp'))+'</button><button data-g="0" class="'+(ui.grp?'':'on')+'">'+esc(t('flat'))+'</button></span>';
  h+='<span class="sp"></span>'+(extra?'<span class="cnt">'+extra+'</span>':'');
  h+='<span class="seg"><button data-v="grid" class="'+(ui.view==='grid'?'on':'')+'" title="'+esc(t('grid'))+'">▦</button><button data-v="list" class="'+(ui.view==='list'?'on':'')+'" title="'+esc(t('list'))+'">☰</button></span>';
  h+='<span class="seg"><button data-z="-1" title="'+esc(t('smaller'))+'">−</button><button data-z="1" title="'+esc(t('bigger'))+'">+</button></span></div>';
  return h;
}
function bindBar(root,rerender){
  root.querySelectorAll('[data-s]').forEach(b=>b.onclick=()=>{ui.sort=b.dataset.s;saveUi();rerender()});
  root.querySelectorAll('[data-g]').forEach(b=>b.onclick=()=>{ui.grp=b.dataset.g==='1';saveUi();rerender()});
  root.querySelectorAll('[data-v]').forEach(b=>b.onclick=()=>{ui.view=b.dataset.v;saveUi();rerender()});
  root.querySelectorAll('[data-z]').forEach(b=>b.onclick=()=>{ui.size=Math.max(0,Math.min(3,ui.size+(+b.dataset.z)));saveUi();applySize()});
  applySize();
}
applySize();

function applyLang(){
  document.documentElement.lang=L;
  $('#ttl').innerHTML=esc(t('ttl'))+'<small>'+esc(t('sub'))+'</small>';
  $('#q').placeholder=t('ph'); $('#go').textContent=t('go'); $('#foot').textContent=t('foot');
  document.querySelectorAll('.lang button').forEach(b=>b.classList.toggle('on',b.dataset.l===L));
  if(person) renderPerson();
}
document.querySelectorAll('.lang button').forEach(b=>b.onclick=()=>{L=b.dataset.l;try{localStorage.setItem('fo_lang',L)}catch(e){};applyLang()});

async function api(path,opt){
  // (2026-11-24) กันค้าง "กำลังโหลด..." ตลอด: หมดเวลา 45 วิ แล้วลองใหม่อัตโนมัติ 1 ครั้ง
  let r=null;
  for(let a=0;a<2&&!r;a++){
    const ac=new AbortController(), tm=setTimeout(()=>ac.abort(),45000);
    try{r=await fetch(path,Object.assign({},opt||{},{signal:ac.signal}))}catch(e){if(a===1) throw new Error(t('fail'))}
    finally{clearTimeout(tm)}
  }
  let j={};
  try{j=await r.json()}catch(e){}
  if(!r.ok) throw new Error(j.message||t('fail'));
  return j;
}
function priceHtml(d){
  if(!d||!d.name&&!d.price) return '<div class="p off">…</div>';
  if(d.resale&&d.price>0) return '<div class="p" title="'+esc(t('resaleTip'))+'">R$ '+(d.price>=100000?(d.price>=1e6?(d.price/1e6).toFixed(d.price>=1e7?0:1)+'M':Math.round(d.price/1e3)+'K'):d.price.toLocaleString())+' <span class="rs">'+esc(t('resale'))+'</span></div>';
  if(d.offsale) return '<div class="p off">'+esc(t('offsale'))+'</div>';
  if(d.price>0) return '<div class="p">R$ '+d.price.toLocaleString()+'</div>';
  return '<div class="p off">—</div>';
}
// (2026-11-24) โหลดเร็วขึ้น: 1) ดึง "รูป" อย่างเดียวก่อน (เร็ว) แสดงทันที  2) ค่อยเติมชื่อ/ราคา ทีละชุด พร้อมกันได้ 3 ชุด
const thumbCache={};
async function fillThumbs(keys){
  const a=[],b=[];
  keys.forEach(k=>{if(thumbCache[k]||(itemCache[k]||{}).thumb)return; if(/^b\d+$/.test(k))b.push(k.slice(1)); else if(/^\d+$/.test(k))a.push(k)});
  const jobs=[];
  for(let i=0;i<a.length;i+=100) jobs.push(api('/api/fo/thumbs?kind=asset&ids='+a.slice(i,i+100).join(',')).then(r=>{for(const id in r) thumbCache[id]=r[id]}).catch(()=>{}));
  for(let i=0;i<b.length;i+=100) jobs.push(api('/api/fo/thumbs?kind=bundle&ids='+b.slice(i,i+100).join(',')).then(r=>{for(const id in r) thumbCache['b'+id]=r[id]}).catch(()=>{}));
  await Promise.all(jobs);
}
async function fillItems(ids,onStep){
  const need=ids.filter(i=>!(i in itemCache));
  if(!need.length) return;
  await fillThumbs(need); if(onStep) onStep();
  const chunks=[]; for(let k=0;k<need.length;k+=60) chunks.push(need.slice(k,k+60));
  let next=0;
  const worker=async()=>{while(next<chunks.length){const c=chunks[next++];
    try{Object.assign(itemCache,await api('/api/fo/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:c})}))}catch(e){}
    if(onStep) onStep()}};
  await Promise.all([worker(),worker(),worker()]);
}
function itemCard(it){
  const d=itemCache[kOf(it)]||{};
  const nm=(it.bundle?'📦 ':'')+(d.name||it.name||('#'+it.id));
  const tu=d.thumb||thumbCache[kOf(it)];
  const th=tu?'<img loading="lazy" src="'+esc(tu)+'" alt="">':'';
  const href=it.bundle?'https://www.roblox.com/bundles/'+it.id:'https://www.roblox.com/catalog/'+it.id;
  const by=d.creator?'<div class="by">'+esc(t('by'))+' '+esc(d.creator)+'</div>':'';
  const tip=esc(nm+(d.creator?' — '+t('by')+' '+d.creator:''));
  if(ui.view==='list') return '<a class="rowi" href="'+href+'" target="_blank" rel="noopener" title="'+tip+'"><div class="th">'+th+'</div><div class="n">'+esc(nm)+' <span>· '+esc(SUBLBL[it.sub]?lbl(SUBLBL[it.sub]):(T[L].cats[it.cat]||''))+(d.creator?' · '+esc(t('by'))+' '+esc(d.creator):'')+'</span></div>'+priceHtml(d)+'</a>';
  return '<a class="item" href="'+href+'" target="_blank" rel="noopener" title="'+tip+'"><div class="th">'+th+'</div><div class="n">'+esc(nm)+'</div>'+by+priceHtml(d)+'</a>';
}
function itemsHtml(items){
  const list=sortBy(items,'items',nmOf), wrap=ui.view==='list'?'rows':'items';
  if(!ui.grp) return '<div class="'+wrap+'">'+list.map(itemCard).join('')+'</div>';
  // (2026-11-24) แยกตามหมวดย่อย แบบหน้า Inventory ของ Roblox: "เครื่องประดับ · หมวก"
  const groups={};
  list.forEach(it=>{const k=SUBLBL[it.sub]?it.sub:(it.bundle?'avatars':'other');(groups[k]=groups[k]||[]).push(it)});
  let h='';
  SUBORDER.forEach(c=>{
    if(!groups[c]) return;
    const o=SUBLBL[c], name=o?(o.main?lbl(o.main)+' · '+lbl(o):lbl(o)):T[L].cats.other;
    h+='<div class="cat"><h3>'+esc(name)+' · '+groups[c].length+'</h3><div class="'+wrap+'">'+groups[c].map(itemCard).join('')+'</div></div>';
  });
  return wrap==='items'?'<div class="cats">'+h+'</div>':h;
}
function totalHtml(items){
  let sum=0;items.forEach(it=>{const d=itemCache[kOf(it)];if(d&&d.price>0&&!d.resale&&!d.offsale)sum+=d.price});
  return '<div class="total">'+items.length+' '+t('items')+(sum?' · '+t('total')+' <b>R$ '+sum.toLocaleString()+'</b>':'')+'</div>';
}

async function search(q){
  q=(q||'').trim(); if(!q) return;
  person=null; out.innerHTML='<div class="msg">'+t('loading')+'</div>';
  try{
    const r=await api('/api/fo/search?q='+encodeURIComponent(q));
    const exact=r.list.find(u=>String(u.id)===q.replace('@','')||u.name.toLowerCase()===q.replace('@','').toLowerCase());
    if(exact||r.list.length===1) return openPerson((exact||r.list[0]).id);
    out.innerHTML='<p class="msg">'+t('pick')+'</p><div class="results">'+r.list.map(u=>
      '<div class="res" data-id="'+u.id+'"><img src="'+esc(u.head)+'" alt=""><div><b>'+esc(u.displayName)+'</b><span>@'+esc(u.name)+' · ID '+u.id+'</span></div></div>').join('')+'</div>';
    out.querySelectorAll('.res').forEach(e=>e.onclick=()=>openPerson(e.dataset.id));
  }catch(e){out.innerHTML='<div class="msg err">'+esc(e.message)+'</div>'}
}
async function openPerson(uid){
  out.innerHTML='<div class="msg">'+t('loading')+'</div>';
  try{location.hash='u='+uid}catch(e){}
  try{
    person=await api('/api/fo/person?uid='+uid);
    tab='wearing'; shown=60; inv={}; renderPerson();
    await fillItems(person.wearing.map(kOf),()=>{if(tab==='wearing')renderPerson()}); if(tab==='wearing') renderPerson();
    retryThumbs();
    if(person.wearErr) retryWearing(uid);
  }catch(e){out.innerHTML='<div class="msg err">'+esc(e.message)+'</div>'}
}
// (2026-11-24) ดึงชุดที่ใส่อยู่ไม่สำเร็จ → ลองใหม่เองเบื้องหลัง ไม่ต้องรีโหลด/กดค้นหาซ้ำ
async function retryWearing(uid){
  for(let n=0;n<6;n++){
    await new Promise(r=>setTimeout(r,3000+n*2000));
    if(!person||String(person.id)!==String(uid)) return;  // เปลี่ยนไปดูคนอื่นแล้ว
    try{
      const p2=await api('/api/fo/person?uid='+uid);
      if(!person||String(person.id)!==String(uid)) return;
      if(!p2.wearErr){
        person.wearing=p2.wearing; person.avatarType=p2.avatarType; person.wearErr=false;
        if(!person.avatarImg) person.avatarImg=p2.avatarImg;
        renderPerson(); await fillItems(person.wearing.map(kOf)); renderPerson(); return;
      }
    }catch(e){}
  }
  if(person&&String(person.id)===String(uid)){person.wearGaveUp=true; renderPerson()}
}
function renderPerson(){
  const p=person; if(!p) return;
  const nR=p.robloxOutfits.length, nM=p.mapOutfits.length;
  let h='<div class="person"><img src="'+esc(p.head)+'" alt=""><div class="nm"><b>'+esc(p.displayName)+'</b><span>@'+esc(p.name)+' · ID '+p.id+'</span></div>'+
    '<a class="btn ghost" href="https://www.roblox.com/users/'+p.id+'/profile" target="_blank" rel="noopener">'+t('profile')+'</a></div>';
  h+='<div class="tabs">'+[['wearing',t('wearing')+' ('+(p.wearErr?'…':p.wearing.length)+')'],['inv',t('inv')+(p.robloxPrivate?' 🔒':'')],['roblox',t('roblox')+' ('+(p.robloxPrivate?'🔒':nR)+')']]
    .map(([k,l])=>'<button class="tab'+(tab===k?' on':'')+'" data-t="'+k+'">'+esc(l)+'</button>').join('')+'</div>';
  if(tab==='wearing'){
    h+='<div class="wear"><div class="avatar">'+(p.avatarImg?'<img src="'+esc(p.avatarImg)+'" alt="">':'')+'<div class="sum">'+totalHtml(p.wearing)+'</div></div><div>'+(p.wearErr?'<p class="msg">'+esc(t(p.wearGaveUp?'wearFail':'wearErr'))+'</p>':barHtml('items')+itemsHtml(p.wearing))+'</div></div>';
  }else if(tab==='inv'){
    const st=inv[invCat], mainK=invCat.split('.')[0], main=INVMETA.mains.find(m=>m.k===mainK)||INVMETA.mains[0];
    const cnt=c=>{const x=inv[c];return x&&x.items?' ('+x.items.length+(x.next?'+':'')+')':''};
    h+='<div class="bar">'+INVMETA.mains.map(m=>'<button class="chip main'+(m.k===mainK?' on':'')+'" data-ic="'+m.k+'">'+esc(lbl(m))+'</button>').join('')+'</div>';
    if(main.subs.length) h+='<div class="bar sub">'+[{k:main.k,th:'ทั้งหมด',en:'All'}].concat(main.subs).map(x=>'<button class="chip'+(invCat===x.k?' on':'')+'" data-ic="'+x.k+'">'+esc(lbl(x))+cnt(x.k)+'</button>').join('')+'</div>';
    if(!st||st.loading&&!st.items) h+='<div class="msg">'+t('loading')+'</div>';
    else if(st.err) h+='<div class="note">'+esc(st.err)+'</div>';
    else if(st.private) h+='<div class="note">'+t('invPriv')+'</div>';
    else if(!st.items.length) h+='<div class="note">'+t('invEmpty')+'</div>';
    else{
      h+='<div class="total">'+esc(t('invHint'))+(st.hidden?' · '+st.hidden+' '+esc(t('invHid')):'')+'</div>'+barHtml('items',st.items.length+(st.next?'+':'')+' '+t('items'));
      const g0=ui.grp; ui.grp=false; h+=itemsHtml(st.items); ui.grp=g0;
      if(st.next) h+='<button class="btn ghost more" id="imore">'+(st.loading?t('loading'):t('showMore'))+'</button>';
    }
  }else if(tab==='roblox'){
    if(!nR) h+='<div class="note">'+(p.robloxPrivate?t('priv'):p.robloxErr?t('fail'):t('noRoblox'))+'</div>';
    else{
      const ro=sortBy(p.robloxOutfits,'outfits',o=>String(o.name||'').toLowerCase()).slice(0,shown);
      const img=o=>o.img?'<img loading="lazy" src="'+esc(o.img)+'" alt="">':'';
      h+=barHtml('outfits',nR+' '+t('outfits'));
      h+=ui.view==='list'
        ?'<div class="rows">'+ro.map(o=>'<div class="rowi" data-o="'+o.id+'"><div class="th">'+img(o)+'</div><div class="n">'+esc(o.name)+'</div></div>').join('')+'</div>'
        :'<div class="grid">'+ro.map(o=>'<div class="of" data-o="'+o.id+'"><div class="th">'+img(o)+'</div><div class="t">'+esc(o.name)+'</div></div>').join('')+'</div>';
      if(nR>shown) h+='<button class="btn ghost more" id="more">'+t('showMore')+' ('+(nR-shown)+')</button>';
    }
  }else{
    if(p.mapErr==='noperm') h+='<div class="note">'+t('mapPerm')+'</div>';
    else if(!nM) h+='<div class="note">'+t('noMap')+'</div>';
    else{
      const order=sortBy(p.mapOutfits.map((o,i)=>Object.assign({i},o)),'map',o=>String(o.name||o.code||'').toLowerCase());
      h+=barHtml('map',nM+' '+t('outfits'));
      h+='<div class="'+(ui.view==='list'?'rows':'grid')+'">'+order.map(o=>{
        const imgs=o.items.filter(x=>['hair','head','top','bottom','acc','face','shoes'].includes(x.cat)).slice(0,4).map(x=>(itemCache[kOf(x)]||{}).thumb).filter(Boolean);
        const date=o.savedAt?new Date(o.savedAt*1000).toLocaleDateString(L==='th'?'th-TH':'en-GB'):'';
        const sub=t('code')+' '+esc(o.code)+' · '+o.items.length+' '+t('items')+(date?' · '+date:'');
        if(ui.view==='list') return '<div class="rowi" data-m="'+o.i+'"><div class="th">'+(imgs[0]?'<img src="'+esc(imgs[0])+'" alt="">':'')+'</div><div class="n">'+esc(o.name||o.code)+' <span>· '+sub+'</span></div></div>';
        return '<div class="of" data-m="'+o.i+'"><div class="th"><div class="collage">'+imgs.map(u=>'<img src="'+esc(u)+'" alt="">').join('')+'</div></div><div class="t">'+esc(o.name||o.code)+'</div><div class="s">'+sub+'</div></div>'}).join('')+'</div>';
    }
  }
  out.innerHTML=h;
  bindBar(out,renderPerson);
  out.querySelectorAll('.tab').forEach(b=>b.onclick=async()=>{tab=b.dataset.t;renderPerson();
    if(tab==='map'&&p.mapOutfits.length){await fillItems([].concat(...p.mapOutfits.map(o=>o.items.slice(0,8).map(kOf))));if(tab==='map')renderPerson()}});
  const more=$('#more'); if(more) more.onclick=()=>{shown+=60;renderPerson();retryThumbs()};
  out.querySelectorAll('[data-ic]').forEach(b=>b.onclick=()=>{invCat=b.dataset.ic;renderPerson();loadInv(invCat,false)});
  const im=$('#imore'); if(im) im.onclick=()=>loadInv(invCat,true);
  if(tab==='inv'&&!inv[invCat]) loadInv(invCat,false);
  out.querySelectorAll('[data-o]').forEach(e=>e.onclick=()=>openOutfit(e.dataset.o));
  out.querySelectorAll('[data-m]').forEach(e=>e.onclick=()=>openMap(+e.dataset.m));
}
async function loadInv(cat,more){
  const p=person; if(!p) return;
  let st=inv[cat];
  if(st&&(st.loading||(!more&&st.items))) return;
  if(!st) st=inv[cat]={items:null,next:null};
  st.loading=true; st.err=null;
  if(tab==='inv'&&invCat===cat) renderPerson();
  try{
    const r=await api('/api/fo/inv?uid='+p.id+'&cat='+cat+(more&&st.next?'&cursor='+encodeURIComponent(st.next):''));
    if(person!==p) return;
    st.private=r.private; st.hidden=(more?(st.hidden||0):0)+(r.hidden||0); st.items=(more&&st.items?st.items:[]).concat(r.items||[]); st.next=r.next||null;
    st.loading=false; if(tab==='inv'&&invCat===cat) renderPerson();
    await fillItems((r.items||[]).map(kOf),()=>{if(person===p&&tab==='inv'&&invCat===cat)renderPerson()});
  }catch(e){st.loading=false; if(!st.items) st.err=e.message}
  if(person===p&&tab==='inv'&&invCat===cat) renderPerson();
}
async function retryThumbs(){
  for(let n=0;n<3&&person;n++){
    const miss=person.robloxOutfits.slice(0,shown).filter(o=>!o.img).map(o=>o.id);
    if(!miss.length) return;
    await new Promise(r=>setTimeout(r,2500));
    try{const got=await api('/api/fo/thumbs?kind=outfit&ids='+miss.slice(0,100).join(','));
      person.robloxOutfits.forEach(o=>{if(got[o.id])o.img=got[o.id]}); if(tab==='roblox')renderPerson()}catch(e){}
  }
}
function showModal(title,body){
  mb.innerHTML='<div class="hd"><b>'+esc(title)+'</b><button class="x" id="mx">✕</button></div>'+body;
  modal.classList.add('on'); $('#mx').onclick=closeModal;
}
function closeModal(){modal.classList.remove('on')}
modal.onclick=e=>{if(e.target===modal)closeModal()};
async function openOutfit(id){
  const o=person.robloxOutfits.find(x=>String(x.id)===String(id))||{};
  showModal(o.name||'',' <div class="msg">'+t('loading')+'</div>');
  try{
    const d=await api('/api/fo/outfit?id='+id);
    const draw=()=>{showModal(o.name||d.name,'<div class="wear"><div class="avatar">'+(o.img?'<img src="'+esc(o.img)+'" alt="">':'')+'<div class="sum">'+totalHtml(d.items)+'</div></div><div>'+barHtml('items')+itemsHtml(d.items)+'</div></div>');bindBar(mb,()=>{draw();renderPerson()})};
    draw();
    await fillItems(d.items.map(kOf),()=>{if(modal.classList.contains('on'))draw()});
    if(modal.classList.contains('on')) draw();
  }catch(e){showModal(o.name||'','<div class="msg err">'+esc(e.message)+'</div>')}
}
async function openMap(i){
  const o=person.mapOutfits[i];
  showModal((o.name||o.code)+' · '+t('code')+' '+o.code,'<div class="msg">'+t('loading')+'</div>');
  await fillItems(o.items.map(kOf));
  const draw=()=>{showModal((o.name||o.code)+' · '+t('code')+' '+o.code,totalHtml(o.items)+barHtml('items')+itemsHtml(o.items));bindBar(mb,()=>{draw();renderPerson()})};
  draw();
}
$('#f').onsubmit=e=>{e.preventDefault();search($('#q').value)};
applyLang();
const m=(location.hash||'').match(/u=(\d+)/); if(m){$('#q').value=m[1];openPerson(m[1])}
</script>
</body>
</html>"""
FO_HTML = FO_HTML.replace("__INVMETA__", json.dumps(INV_META, ensure_ascii=False))
