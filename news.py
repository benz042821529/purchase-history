"""ข่าว Roblox สำหรับแมพ รวมมิจของtangsan (2026-11-24)

GET /api/news  (สาธารณะ ไม่ต้องใช้ key)
  {
    "updated": unix,
    "news":   [ { id, title, th, excerpt, excerpt_th, url, created, cat } ... ],   # DevForum หมวด Updates
    "status": { overall: 0|1|2, items: [ { key, th, en, level } ], incidents: [ { title, th } ], maintenance: [ ... ] }
  }
level: 0 = ปกติ / 1 = ช้าหรือปิดปรับปรุง / 2 = มีปัญหา

- ดึง DevForum (Discourse JSON) + status.roblox.com (Status.io API) ทุก 10 นาที แล้วเก็บไว้ในหน่วยความจำ
- แปลหัวข้อเป็นไทยด้วย translate.googleapis.com (แบบฟรี) ถ้าแปลไม่ได้ใช้ภาษาอังกฤษแทน
- แมพ (ServerScriptService.RobloxNewsServer) เป็นคนเรียก ไม่ใช่ผู้เล่นแต่ละคน
"""
import json, re, time, threading, html, urllib.request, urllib.parse

UA = {"User-Agent": "Mozilla/5.0 (tangsan-news)", "Accept": "application/json"}
DEVFORUM_URL = "https://devforum.roblox.com/c/updates/45.json"
STATUS_URL = "https://api.status.io/1.0/status/59db90dbcdeb2f04dadcf16d"
TTL = 600
MAX_NEWS = 12

_lock = threading.Lock()
_cache = {"t": 0, "data": None}
_tr_cache = {}


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _translate_many(texts):
    """แปลหลายข้อความในครั้งเดียว (คั่นด้วยบรรทัดใหม่) — คืนลิสต์ยาวเท่าเดิม แปลไม่ได้ = ''"""
    out = [_tr_cache.get(t, None) for t in texts]
    todo = [t for t, o in zip(texts, out) if o is None and t]
    if todo:
        try:
            q = "\n".join(t.replace("\n", " ") for t in todo)
            url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
                {"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": q})
            d = _get(url, timeout=15)
            joined = "".join(seg[0] for seg in d[0] if seg and seg[0])
            parts = [p.strip() for p in joined.split("\n")]
            if len(parts) == len(todo):
                for src, th in zip(todo, parts):
                    _tr_cache[src] = th
        except Exception as e:
            print(f"[news translate] {e}")
        out = [_tr_cache.get(t, "") for t in texts]
    return [o or "" for o in out]


def _fetch_news():
    d = _get(DEVFORUM_URL)
    topics = (d.get("topic_list") or {}).get("topics") or []
    rows = []
    for t in topics:
        title = _clean(t.get("title"))
        low = title.lower()
        if not title or low.startswith("about the") or "forum rules" in low or "how to join" in low:
            continue
        rows.append({
            "id": t.get("id"),
            "title": title,
            "excerpt": _clean(t.get("excerpt"))[:220],
            "url": f"https://devforum.roblox.com/t/{t.get('slug')}/{t.get('id')}",
            "created": t.get("created_at") or "",
            "cat": t.get("category_id"),
        })
    rows.sort(key=lambda r: r["created"], reverse=True)
    rows = rows[:MAX_NEWS]
    ths = _translate_many([r["title"] for r in rows])
    exs = _translate_many([r["excerpt"] for r in rows])
    for r, th, ex in zip(rows, ths, exs):
        r["th"], r["excerpt_th"] = th, ex
    return rows


# ส่วนที่ผู้เล่นสนใจ → ตำแหน่งในหน้า status (กลุ่ม / ชื่อ)
STATUS_ITEMS = [
    ("join", "เข้าเกม", "Joining games", [("Player", "Games"), ("Creator", "Game Join")]),
    ("avatar", "อวาตาร์ / ร้านค้า", "Avatar / Marketplace", [("Player", "Avatar"), ("Creator", "Marketplace")]),
    ("website", "เว็บไซต์", "Website", [("Player", "Website")]),
    ("mobile", "แอปมือถือ", "Mobile app", [("Player", "Mobile App")]),
    ("studio", "Studio / เซฟข้อมูล", "Studio / Data", [("Creator", "Studio"), ("Creator", "Data Store")]),
]


def _level(code):
    try:
        code = int(code)
    except Exception:
        return 0
    if code <= 100:
        return 0
    if code <= 300:
        return 1
    return 2


def _fetch_status():
    d = (_get(STATUS_URL) or {}).get("result") or {}
    found = {}
    for grp in d.get("status") or []:
        gname = grp.get("name")
        for c in grp.get("containers") or []:
            found[(gname, c.get("name"))] = _level(c.get("status_code"))
    items = []
    for key, th, en, refs in STATUS_ITEMS:
        lv = max([found.get(r, 0) for r in refs] or [0])
        items.append({"key": key, "th": th, "en": en, "level": lv})
    incidents = [_clean(i.get("name")) for i in d.get("incidents") or [] if i.get("name")]
    maint = [_clean(m.get("name")) for m in ((d.get("maintenance") or {}).get("active") or []) if m.get("name")]
    inc_th = _translate_many(incidents) if incidents else []
    mt_th = _translate_many(maint) if maint else []
    overall = _level((d.get("status_overall") or {}).get("status_code"))
    return {
        "overall": max([overall] + [i["level"] for i in items]),
        "items": items,
        "incidents": [{"title": a, "th": b} for a, b in zip(incidents, inc_th)],
        "maintenance": [{"title": a, "th": b} for a, b in zip(maint, mt_th)],
    }


def get_news():
    now = time.time()
    if _cache["data"] and now - _cache["t"] < TTL:
        return _cache["data"]
    with _lock:
        if _cache["data"] and time.time() - _cache["t"] < TTL:
            return _cache["data"]
        old = _cache["data"] or {}
        data = {"updated": int(time.time())}
        try:
            data["news"] = _fetch_news()
        except Exception as e:
            print(f"[news devforum] {e}")
            data["news"] = old.get("news") or []
        try:
            data["status"] = _fetch_status()
        except Exception as e:
            print(f"[news status] {e}")
            data["status"] = old.get("status") or {"overall": 0, "items": [], "incidents": [], "maintenance": []}
        _cache["data"], _cache["t"] = data, time.time()
        return data
