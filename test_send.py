import sys, time
sys.path.insert(0, ".")
import server as s

fake_entries = [
    {"id": 1818, "tp": "B", "p": 15300, "ts": int(time.time()) - 3600, "username": "llNomSoDll"},
    {"id": 2929, "tp": "A", "p": 350,   "ts": int(time.time()) - 7200, "username": "mintchoco99"},
]
fake_item_map = {
    "B_1818": {"thumb": "https://tr.rbxcdn.com/180DAY-7fddb094b8752cd87b62ffba2abae797/150/150/Hat/Png/noFilter", "name": "Korblox ลำโพงแห่งความตาย", "creator": "Korblox", "price": 15300},
    "A_2929": {"thumb": "https://tr.rbxcdn.com/180DAY-7fddb094b8752cd87b62ffba2abae797/150/150/Hat/Png/noFilter", "name": "ปีกนางฟ้าสีชมพู",           "creator": "CloudNine", "price": 350},
}

html = s.build_digest_html("TEST-SEND", fake_entries, fake_item_map)
s.send_email("[Purchase History] ทดสอบระบบส่งอีเมล", html)
print("ส่งสำเร็จ! เช็คอีเมลได้เลย")
