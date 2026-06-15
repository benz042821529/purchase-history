#!/usr/bin/env python3
import os, json, time
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

API_KEY     = os.environ.get("API_KEY", "")
UNIVERSE_ID = os.environ.get("UNIVERSE_ID", "")
PASSWORD    = os.environ.get("PASSWORD", "admin")
PORT        = int(os.environ.get("PORT", 8080))

BASE    = f"https://apis.roblox.com/datastores/v1/universes/{UNIVERSE_ID}"
DS_NAME = "PurchaseLog_v1"

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
    except: pass
    return None, None

def get_display_name(uid):
    try:
        req = urllib.request.Request(f"https://users.roblox.com/v1/users/{uid}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("name", str(uid))
    except:
        return str(uid)

def fetch_history(uid, from_ts=None, to_ts=None):
    try:
        data = roblox_get(
            f"{BASE}/standard-datastores/datastore/entries/entry",
            {"datastoreName": DS_NAME, "entryKey": f"P_{uid}"}
        )
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None
    except:
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

def roblox_public(url, data=None, method="GET"):
    headers = {"User-Agent": UA}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def fetch_item_details(items):
    result = {}
    assets  = [e["id"] for e in items if e["tp"] != "B"]
    bundles = [e["id"] for e in items if e["tp"] == "B"]

    # Thumbnails — assets
    for i in range(0, len(assets), 100):
        ids = ",".join(str(x) for x in assets[i:i+100])
        try:
            d = roblox_public(f"https://thumbnails.roblox.com/v1/assets?assetIds={ids}&size=150x150&format=Png")
            for x in d.get("data", []):
                if x.get("state") == "Completed":
                    result[f"A_{x['targetId']}"] = {"thumb": x["imageUrl"], "creator": ""}
        except Exception as e:
            print(f"[thumb asset] {e}")

    # Thumbnails — bundles
    for i in range(0, len(bundles), 100):
        ids = ",".join(str(x) for x in bundles[i:i+100])
        try:
            d = roblox_public(f"https://thumbnails.roblox.com/v1/bundles/thumbnails?bundleIds={ids}&size=150x150&format=Png")
            for x in d.get("data", []):
                if x.get("state") == "Completed":
                    result[f"B_{x['targetId']}"] = {"thumb": x["imageUrl"], "creator": ""}
        except Exception as e:
            print(f"[thumb bundle] {e}")

    # Creator names — GET endpoints (no CSRF needed)
    def get_creator(item):
        iid, tp = item["id"], item["tp"]
        try:
            if tp == "B":
                d = roblox_public(f"https://catalog.roblox.com/v1/bundles/{iid}/details")
                return f"B_{iid}", d.get("creator", {}).get("name", "")
            else:
                d = roblox_public(f"https://economy.roblox.com/v1/assets/{iid}/details")
                return f"A_{iid}", d.get("Creator", {}).get("Name", "")
        except Exception as e:
            print(f"[creator {tp}_{iid}] {e}")
            return f"{tp}_{iid}", ""

    unique = list({f"{e['tp']}_{e['id']}": e for e in items}.values())
    with ThreadPoolExecutor(max_workers=10) as ex:
        for key, name in ex.map(get_creator, unique):
            if key not in result:
                result[key] = {"thumb": "", "creator": ""}
            result[key]["creator"] = name

    return result

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
    <thead><tr><th style="width:66px"></th><th>สินค้า</th><th>ผู้สร้าง</th><th>ราคา</th><th>ประเภท</th><th>วันที่</th><th>เวลา</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<script>
let _pw = ''
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

async function search(){
  const q=document.getElementById('query').value.trim()
  if(!q)return
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
    document.getElementById('tbody').innerHTML=items.map(e=>{
      const isB=e.tp==='B',{date,time}=fmtParts(e.ts)
      return `<tr><td><img class="thumb" data-id="${e.id}" data-tp="${e.tp}" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></td><td class="item-name">${e.n||'ID:'+e.id}</td><td><span class="creator" data-id="${e.id}" data-tp="${e.tp}">...</span></td><td class="price">${e.p?'R$ '+e.p:'ฟรี'}</td><td><span class="badge ${isB?'bb':'ba'}">${isB?'Bundle':'Asset'}</span></td><td><div class="date-cell">${date}</div></td><td><div class="time-cell">${time}</div></td></tr>`
    }).join('')
    document.getElementById('tbl').style.display='table'
    loadItemDetails(items)
  }catch(e){status.className='status err';status.textContent='เกิดข้อผิดพลาด: '+e.message}
  finally{btn.disabled=false}
}

async function loadItemDetails(items){
  try{
    const unique=[...new Map(items.map(e=>[`${e.tp}_${e.id}`,{id:e.id,tp:e.tp}])).values()]
    const r=await fetch('/api/item-details',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-Password':_pw},
      body:JSON.stringify(unique)
    })
    const map=await r.json()
    document.querySelectorAll('img.thumb').forEach(img=>{
      const key=`${img.dataset.tp}_${img.dataset.id}`
      if(map[key]?.thumb) img.src=map[key].thumb
    })
    document.querySelectorAll('span.creator').forEach(el=>{
      const key=`${el.dataset.tp}_${el.dataset.id}`
      el.textContent=map[key]?.creator||'—'
    })
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
            try:
                if tp == "B":
                    url = f"https://catalog.roblox.com/v1/bundles/{iid}/details"
                    d   = roblox_public(url)
                    out = {"url": url, "creator": d.get("creator", {}), "raw_keys": list(d.keys())}
                else:
                    url = f"https://economy.roblox.com/v1/assets/{iid}/details"
                    d   = roblox_public(url)
                    out = {"url": url, "Creator": d.get("Creator", {}), "raw_keys": list(d.keys())}
            except Exception as e:
                out = {"error": str(e)}
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
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
