from fastapi import FastAPI
from pydantic import BaseModel
import secrets
import json
import os

app = FastAPI()

DATA_FILE = "auth_data.json"

# ============================================================
# JSON 로드/저장
# ============================================================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(auth_db, f, ensure_ascii=False, indent=2)


# ============================================================
# 메모리 DB
# ============================================================
auth_db = load_data()


# ============================================================
# 요청 모델
# ============================================================
class CodeRequest(BaseModel):
    code: str

class PasswordRequest(BaseModel):
    password: str


# ============================================================
# register
# ============================================================
@app.post("/register")
def register(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {
            "status": "pending",
            "token": None,
            "delete_password": None      # ★ 코드별 삭제비번
        }
        save_data()

    return {"code": code, "status": auth_db[code]["status"]}


# ============================================================
# approve
# ============================================================
@app.post("/approve")
def approve(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {
            "status": "pending",
            "token": None,
            "delete_password": None
        }

    token = secrets.token_hex(32)
    auth_db[code]["status"] = "approved"
    auth_db[code]["token"] = token

    save_data()
    return {"status": "approved", "token": token}


# ============================================================
# 코드별 삭제 비밀번호 저장 API
# ============================================================
@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest, code: str = None):
    # Android 앱 구조 때문에 code를 Body에서 받는 대신 Query로 받음
    # ex) POST /set_delete_pwd?code=kyh

    if code is None:
        return {"error": "code query required"}

    if code not in auth_db:
        return {"error": "code_not_found"}

    auth_db[code]["delete_password"] = req.password
    save_data()

    return {"status": "ok", "code": code, "delete_password": req.password}


# ============================================================
# 삭제 API
# ============================================================
@app.post("/delete")
def delete(req: CodeRequest):
    code = req.code

    # 전체 삭제
    if code.lower() == "all":
        auth_db.clear()
        save_data()
        return {"status": "all_deleted"}

    # 개별 삭제
    if code in auth_db:
        del auth_db[code]
        save_data()
        return {"status": "deleted"}

    return {"status": "not_found"}


# ============================================================
# 리스트 API
# ============================================================
@app.get("/list")
def list_codes():
    return auth_db


# ============================================================
# 앱 인증 API (변경 없음)
# ============================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        return {"status": "invalid"}

    status = auth_db[code]["status"]
    token = auth_db[code]["token"]

    if status == "approved" and token:
        return {"status": "approved", "token": token}

    return {"status": status}


# ============================================================
# 관리자 페이지 /tokens
# ============================================================
from fastapi.responses import HTMLResponse

# 관리자 접속 비밀번호 (원하는 값으로 변경 가능)
ADMIN_PASSWORD = "Kyh5374!@#"


@app.get("/tokens", response_class=HTMLResponse)
def tokens_page(admin: str = None):

    # 1) 비밀번호 검증
    if admin != ADMIN_PASSWORD:
        # 로그인 화면 출력
        return """
        <html><head><meta charset="UTF-8">
        <style>
            body { background:#111; color:#eee; font-family:Arial; padding:40px; }
            input { padding:10px; font-size:16px; }
            button { padding:10px 20px; font-size:16px; margin-left:10px; }
        </style>
        </head><body>

        <h2>🔐 관리자 로그인</h2>
        <form method="get" action="/tokens">
            <input type="password" name="admin" placeholder="비밀번호 입력" />
            <button type="submit">로그인</button>
        </form>

        </body></html>
        """

    # 2) 비밀번호 맞으면 토큰 목록 출력
    html = """
    <html><head><meta charset="UTF-8">
    <style>
        body { background:#111; color:#eee; font-family:Arial; padding:20px; }
        table { width:100%; border-collapse:collapse; margin-top:20px; }
        th,td { border:1px solid #444; padding:8px; }
        th { background:#222; }
        tr:nth-child(even) { background:#1a1a1a; }
    </style>
    </head><body>

    <h1>🔐 Pocket Blackbox Token List</h1>
    <table>
        <tr>
            <th>코드</th>
            <th>삭제 비밀번호</th>
            <th>상태</th>
            <th>토큰</th>
        </tr>
    """

    for code, data in auth_db.items():
        html += f"""
        <tr>
            <td>{code}</td>
            <td>{data.get('delete_password','')}</td>
            <td>{data.get('status')}</td>
            <td>{data.get('token')}</td>
        </tr>
        """

    html += "</table></body></html>"
    return html
