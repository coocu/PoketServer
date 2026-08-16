"""배포 전에 현재 운영 서버의 /list 데이터를 로컬 파일로 백업합니다.
사용: python3 backup_live_data.py
"""
from urllib.request import urlopen
from datetime import datetime
import json

URL = "https://poketserver.onrender.com/list"

with urlopen(URL, timeout=60) as response:
    data = json.loads(response.read().decode("utf-8"))

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
filename = f"auth_data_backup_{stamp}.json"
with open(filename, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"백업 완료: {filename} / {len(data)}개 인증키")
