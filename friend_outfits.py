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

def init(roblox_get, fetch_item_details, base, cache_get, cache_set):
    _h.update(roblox_get=roblox_get, fetch_item_details=fetch_item_details, base=base,
              cache_get=cache_get, cache_set=cache_set)

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
                time.sleep(0.6 * (attempt + 1)); continue
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
    "hair":   ("ผม", "Hair"),        "head":   ("หมวก / หัว", "Head"),   "face":  ("หน้า", "Face"),
    "top":    ("เสื้อ", "Tops"),      "bottom": ("กางเกง / กระโปรง", "Bottoms"), "shoes": ("รองเท้า", "Shoes"),
    "acc":    ("เครื่องประดับ", "Accessories"), "body": ("ร่างกาย", "Body"), "anim": ("ท่าเดิน / ท่าทาง", "Animations"),
    "emote":  ("อีโมต", "Emotes"),   "other":  ("อื่นๆ", "Other"),
}
CAT_ORDER = ["hair", "head", "face", "top", "bottom", "shoes", "acc", "body", "anim", "emote", "other"]
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

def _item(aid, atype, name=""):
    return {"id": int(aid), "type": atype or "", "cat": TYPE_CAT.get(atype or "", "other"), "name": name or ""}

def _assets_to_items(assets):
    out, seen = [], set()
    for a in assets or []:
        aid = a.get("id")
        if not aid or aid in seen: continue
        seen.add(aid)
        out.append(_item(aid, (a.get("assetType") or {}).get("name", ""), a.get("name", "")))
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
            f_av = ex.submit(lambda: _req(f"https://avatar.roblox.com/v1/users/{uid}/avatar"))
            f_ro = ex.submit(_roblox_outfits, uid)
            f_mp = ex.submit(_map_outfits, uid)
            try:
                av = f_av.result()
            except Exception as e:
                print(f"[fo avatar {uid}] {e}"); av = {}
            ro, ro_err = f_ro.result()
            mp, mp_err = f_mp.result()
        wearing = _assets_to_items(av.get("assets"))
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
            "mapOutfits": mp, "mapErr": mp_err,
        }
    return _cached(("person", uid), 180, go)

def outfit(oid):
    def go():
        try:
            d = _req(f"https://avatar.roblox.com/v1/outfits/{oid}/details")
        except urllib.error.HTTPError as e:
            raise FoError(404 if e.code in (400, 404) else 502, "เปิดชุดนี้ไม่ได้ (อาจถูกลบไปแล้ว)")
        return {"id": oid, "name": d.get("name", ""), "items": _assets_to_items(d.get("assets"))}
    return _cached(("outfit", oid), 600, go)

def items(ids):
    ids = [int(i) for i in ids if str(i).isdigit()][:80]
    det = _h["fetch_item_details"]([{"id": i, "tp": "A"} for i in ids])
    return {str(i): det.get(f"A_{i}", {}) for i in ids}

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
.avatar{background:var(--panel);border-radius:16px;padding:10px;text-align:center;align-self:start}
.avatar img{width:100%;max-width:300px;aspect-ratio:1;object-fit:contain}
.avatar .sum{color:var(--sub);font-size:13px;margin-top:6px}
.cat{margin-bottom:14px}
.cat h3{font-size:14px;font-weight:500;color:var(--lilac);margin:0 0 8px}
.items{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.item{background:var(--panel);border-radius:14px;padding:8px;display:flex;flex-direction:column;gap:6px;text-decoration:none}
.item:hover{outline:1px solid var(--lilac)}
.item .th{background:var(--card);border-radius:10px;aspect-ratio:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
.item .th img{width:100%;height:100%;object-fit:contain}
.item .n{font-size:13px;line-height:1.25;height:2.5em;overflow:hidden}
.item .p{font-size:13px;font-weight:500;color:var(--gold)}
.item .p.free{color:var(--green)}.item .p.off{color:var(--sub);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.of{background:var(--panel);border-radius:16px;padding:8px;cursor:pointer;border:1px solid transparent}
.of:hover{border-color:var(--lilac)}
.of .th{background:var(--card);border-radius:12px;aspect-ratio:1;overflow:hidden;display:flex;align-items:center;justify-content:center}
.of .th img{width:100%;height:100%;object-fit:contain}
.of .collage{display:grid;grid-template-columns:1fr 1fr;width:100%;height:100%}
.of .collage img{width:100%;height:100%;object-fit:contain}
.of .t{font-size:14px;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.of .s{font-size:12px;color:var(--sub)}
.more{display:block;margin:14px auto 0}
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
  <h1 id="ttl">👕 ดูชุดผู้เล่น Roblox<small id="sub1">พิมพ์ ID หรือชื่อผู้เล่น → เห็นชุดที่ใส่อยู่ ชุดที่สร้างไว้ และชุดที่เซฟในแมพ</small></h1>
  <div class="lang"><button data-l="th">ไทย</button><button data-l="en">EN</button></div>
</header>
<form class="search" id="f">
  <input id="q" autocomplete="off" placeholder="ID / @username">
  <button class="btn" id="go">ค้นหา</button>
</form>
<div id="out"></div>
<footer id="foot">ข้อมูลจาก Roblox (สาธารณะ) · ชุดที่เซฟในแมพ = ระบบบันทึกชุดในแมพ รวมมิจของtangsan</footer>
</div>
<div class="modal" id="m"><div class="box" id="mb"></div></div>
<script>
const T={
 th:{ttl:'👕 ดูชุดผู้เล่น Roblox',sub:'พิมพ์ ID หรือชื่อผู้เล่น → เห็นชุดที่ใส่อยู่ ชุดที่สร้างไว้ และชุดที่เซฟในแมพ',ph:'ID / @ชื่อผู้เล่น',go:'ค้นหา',
  loading:'กำลังโหลด...',pick:'เจอหลายคน เลือกคนที่ใช่',wearing:'ใส่อยู่ตอนนี้',roblox:'ชุดใน Roblox',map:'ชุดที่เซฟในแมพ',
  items:'ชิ้น',total:'ราคารวมชิ้นที่ยังขาย',free:'ฟรี',off:'ไม่ขาย',profile:'โปรไฟล์ Roblox',showMore:'แสดงเพิ่ม',
  noRoblox:'ไม่มีชุดที่สร้างเก็บไว้ใน Roblox',priv:'🔒 เจ้าตัวปิดการดูไอเทม/ชุดไว้ ดูได้แค่ชุดที่ใส่อยู่',
  noMap:'ยังไม่เคยเซฟชุดในแมพ',mapPerm:'ยังเปิดดูชุดในแมพไม่ได้ (ต้องเพิ่มสิทธิ์ API key ให้อ่าน DataStore PlayerOutfitCodes_v1)',
  code:'โค้ด',saved:'เซฟเมื่อ',fail:'โหลดไม่สำเร็จ ลองใหม่อีกครั้ง',back:'ค้นหาใหม่',
  foot:'ข้อมูลจาก Roblox (สาธารณะ) · ชุดที่เซฟในแมพ = ระบบบันทึกชุดในแมพ รวมมิจของtangsan',
  cats:{hair:'ผม',head:'หมวก / หัว',face:'หน้า',top:'เสื้อ',bottom:'กางเกง / กระโปรง',shoes:'รองเท้า',acc:'เครื่องประดับ',body:'ร่างกาย',anim:'ท่าเดิน / ท่าทาง',emote:'อีโมต',other:'อื่นๆ'}},
 en:{ttl:'👕 Roblox Outfit Viewer',sub:"Type an ID or username → see what they're wearing, their saved Roblox outfits and outfits saved in the map",ph:'ID / @username',go:'Search',
  loading:'Loading...',pick:'Several players found — pick one',wearing:'Wearing now',roblox:'Roblox outfits',map:'Saved in map',
  items:'items',total:'Total for items still on sale',free:'Free',off:'Off sale',profile:'Roblox profile',showMore:'Show more',
  noRoblox:'No outfits saved on their Roblox account',priv:'🔒 This player hides their inventory — only the current outfit is visible',
  noMap:'No outfits saved in the map yet',mapPerm:'Map outfits not available yet (the API key needs read access to DataStore PlayerOutfitCodes_v1)',
  code:'Code',saved:'Saved',fail:'Could not load — please try again',back:'New search',
  foot:'Data from Roblox (public) · Map outfits come from the outfit saver in รวมมิจของtangsan',
  cats:{hair:'Hair',head:'Hats / Head',face:'Face',top:'Tops',bottom:'Bottoms',shoes:'Shoes',acc:'Accessories',body:'Body',anim:'Animations',emote:'Emotes',other:'Other'}}
};
const ORDER=['hair','head','face','top','bottom','shoes','acc','body','anim','emote','other'];
let L=(()=>{try{return localStorage.getItem('fo_lang')||'th'}catch(e){return 'th'}})();
const $=s=>document.querySelector(s), out=$('#out'), modal=$('#m'), mb=$('#mb');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=k=>T[L][k];
const itemCache={};
let person=null, tab='wearing', shown=60;

function applyLang(){
  document.documentElement.lang=L;
  $('#ttl').innerHTML=esc(t('ttl'))+'<small>'+esc(t('sub'))+'</small>';
  $('#q').placeholder=t('ph'); $('#go').textContent=t('go'); $('#foot').textContent=t('foot');
  document.querySelectorAll('.lang button').forEach(b=>b.classList.toggle('on',b.dataset.l===L));
  if(person) renderPerson();
}
document.querySelectorAll('.lang button').forEach(b=>b.onclick=()=>{L=b.dataset.l;try{localStorage.setItem('fo_lang',L)}catch(e){};applyLang()});

async function api(path,opt){
  const r=await fetch(path,opt); let j={};
  try{j=await r.json()}catch(e){}
  if(!r.ok) throw new Error(j.message||t('fail'));
  return j;
}
function priceHtml(d){
  if(!d||!d.name&&!d.price) return '<div class="p off">…</div>';
  if(d.price>0) return '<div class="p">R$ '+d.price.toLocaleString()+'</div>';
  return '<div class="p off">—</div>';
}
async function fillItems(ids){
  const need=ids.filter(i=>!(i in itemCache));
  for(let k=0;k<need.length;k+=60){
    try{Object.assign(itemCache,await api('/api/fo/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:need.slice(k,k+60)})}))}catch(e){}
  }
}
function itemsHtml(items){
  const groups={};
  items.forEach(it=>(groups[it.cat]=groups[it.cat]||[]).push(it));
  let h='';
  ORDER.forEach(c=>{
    if(!groups[c]) return;
    h+='<div class="cat"><h3>'+esc(T[L].cats[c])+' · '+groups[c].length+'</h3><div class="items">';
    groups[c].forEach(it=>{
      const d=itemCache[it.id]||{};
      const nm=d.name||it.name||('#'+it.id);
      const th=d.thumb?'<img loading="lazy" src="'+esc(d.thumb)+'" alt="">':'';
      h+='<a class="item" href="https://www.roblox.com/catalog/'+it.id+'" target="_blank" rel="noopener"><div class="th">'+th+'</div><div class="n">'+esc(nm)+'</div>'+priceHtml(d)+'</a>';
    });
    h+='</div></div>';
  });
  return h;
}
function totalHtml(items){
  let sum=0;items.forEach(it=>{const d=itemCache[it.id];if(d&&d.price>0)sum+=d.price});
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
    tab='wearing'; shown=60; renderPerson();
    await fillItems(person.wearing.map(i=>i.id)); if(tab==='wearing') renderPerson();
    retryThumbs();
  }catch(e){out.innerHTML='<div class="msg err">'+esc(e.message)+'</div>'}
}
function renderPerson(){
  const p=person; if(!p) return;
  const nR=p.robloxOutfits.length, nM=p.mapOutfits.length;
  let h='<div class="person"><img src="'+esc(p.head)+'" alt=""><div class="nm"><b>'+esc(p.displayName)+'</b><span>@'+esc(p.name)+' · ID '+p.id+'</span></div>'+
    '<a class="btn ghost" href="https://www.roblox.com/users/'+p.id+'/profile" target="_blank" rel="noopener">'+t('profile')+'</a></div>';
  h+='<div class="tabs">'+[['wearing',t('wearing')+' ('+p.wearing.length+')'],['roblox',t('roblox')+' ('+(p.robloxPrivate?'🔒':nR)+')'],['map',t('map')+' ('+nM+')']]
    .map(([k,l])=>'<button class="tab'+(tab===k?' on':'')+'" data-t="'+k+'">'+esc(l)+'</button>').join('')+'</div>';
  if(tab==='wearing'){
    h+='<div class="wear"><div class="avatar">'+(p.avatarImg?'<img src="'+esc(p.avatarImg)+'" alt="">':'')+'<div class="sum">'+totalHtml(p.wearing)+'</div></div><div>'+itemsHtml(p.wearing)+'</div></div>';
  }else if(tab==='roblox'){
    if(!nR) h+='<div class="note">'+(p.robloxPrivate?t('priv'):p.robloxErr?t('fail'):t('noRoblox'))+'</div>';
    else{
      h+='<div class="grid">'+p.robloxOutfits.slice(0,shown).map(o=>'<div class="of" data-o="'+o.id+'"><div class="th">'+(o.img?'<img loading="lazy" src="'+esc(o.img)+'" alt="">':'')+'</div><div class="t">'+esc(o.name)+'</div></div>').join('')+'</div>';
      if(nR>shown) h+='<button class="btn ghost more" id="more">'+t('showMore')+' ('+(nR-shown)+')</button>';
    }
  }else{
    if(p.mapErr==='noperm') h+='<div class="note">'+t('mapPerm')+'</div>';
    else if(!nM) h+='<div class="note">'+t('noMap')+'</div>';
    else h+='<div class="grid">'+p.mapOutfits.map((o,i)=>{
      const imgs=o.items.filter(x=>['hair','head','top','bottom','acc','face','shoes'].includes(x.cat)).slice(0,4).map(x=>(itemCache[x.id]||{}).thumb).filter(Boolean);
      const date=o.savedAt?new Date(o.savedAt*1000).toLocaleDateString(L==='th'?'th-TH':'en-GB'):'';
      return '<div class="of" data-m="'+i+'"><div class="th"><div class="collage">'+imgs.map(u=>'<img src="'+esc(u)+'" alt="">').join('')+'</div></div><div class="t">'+esc(o.name||o.code)+'</div><div class="s">'+t('code')+' '+esc(o.code)+' · '+o.items.length+' '+t('items')+(date?' · '+date:'')+'</div></div>'}).join('')+'</div>';
  }
  out.innerHTML=h;
  out.querySelectorAll('.tab').forEach(b=>b.onclick=async()=>{tab=b.dataset.t;renderPerson();
    if(tab==='map'&&p.mapOutfits.length){await fillItems([].concat(...p.mapOutfits.map(o=>o.items.slice(0,8).map(x=>x.id))));if(tab==='map')renderPerson()}});
  const more=$('#more'); if(more) more.onclick=()=>{shown+=60;renderPerson();retryThumbs()};
  out.querySelectorAll('[data-o]').forEach(e=>e.onclick=()=>openOutfit(e.dataset.o));
  out.querySelectorAll('[data-m]').forEach(e=>e.onclick=()=>openMap(+e.dataset.m));
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
    await fillItems(d.items.map(i=>i.id));
    showModal(o.name||d.name,'<div class="wear"><div class="avatar">'+(o.img?'<img src="'+esc(o.img)+'" alt="">':'')+'<div class="sum">'+totalHtml(d.items)+'</div></div><div>'+itemsHtml(d.items)+'</div></div>');
  }catch(e){showModal(o.name||'','<div class="msg err">'+esc(e.message)+'</div>')}
}
async function openMap(i){
  const o=person.mapOutfits[i];
  showModal((o.name||o.code)+' · '+t('code')+' '+o.code,'<div class="msg">'+t('loading')+'</div>');
  await fillItems(o.items.map(x=>x.id));
  showModal((o.name||o.code)+' · '+t('code')+' '+o.code,totalHtml(o.items)+itemsHtml(o.items));
}
$('#f').onsubmit=e=>{e.preventDefault();search($('#q').value)};
applyLang();
const m=(location.hash||'').match(/u=(\d+)/); if(m){$('#q').value=m[1];openPerson(m[1])}
</script>
</body>
</html>"""
