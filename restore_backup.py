"""backup_live_data.py로 만든 JSON을 새 서버에 병합 복구합니다.
사용: python3 restore_backup.py auth_data_backup_20260816_120000.json
"""
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://poketserver.onrender.com"
ADMIN_PASSWORD = "Kim86110!@"

if len(sys.argv) != 2:
    raise SystemExit("사용: python3 restore_backup.py <백업파일.json>")

with open(sys.argv[1], "r", encoding="utf-8") as f:
    raw = json.load(f)

data = raw.get("auth_db", raw) if isinstance(raw, dict) else raw
if not isinstance(data, dict):
    raise SystemExit("백업 파일 형식이 올바르지 않습니다.")

url = f"{BASE_URL}/manage/import-backup?" + urlencode({"admin": ADMIN_PASSWORD})
payload = json.dumps({"data": data, "replace": False}, ensure_ascii=False).encode("utf-8")
request = Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})

with urlopen(request, timeout=120) as response:
    result = json.loads(response.read().decode("utf-8"))

print("복구 완료:", result)
