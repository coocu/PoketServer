from fastapi import FastAPI
from pydantic import BaseModel
import secrets
import json
import os

app = FastAPI()

DATA_FILE = "auth_data.json"
ADMIN_PASSWORD = "Kyh5374!@#"   # 🔐 관리자 페이지 비밀번호


# ============================================================
#   JSON 저장/로드 기능
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
#   메모리 DB (서버 실행 시 JSON에서 복구)
# ============================================================
auth_db = load_data()

# 🔥 관리자 앱 / 포켓 앱에서 마지막으로 사용한 코드 기억용
last_admin_code: str | None = None
last_app_code: str | None = None


# ============================================================
#   요청 모델
# ============================================================
class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


# ============================================================
#   관리자 API
# ============================================================
@app.post("/register")
def register(req: CodeRequest):
    global last_admin_code
    code = req.code
    last_admin_code = code

    if code not in auth_db:
        auth_db[code] = {
            "status": "pending",
            "token": None,
            "delete_password": None
        }
        save_data()

    return {"code": code, "status": auth_db[code]["status"]}


@app.post("/approve")
def approve(req: CodeRequest):
    global last_admin_code
    code = req.code
    last_admin_code = code

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


@app.get("/list")
def list_codes():
    return auth_db


@app.post("/delete")
def delete(req: CodeRequest):
    code = req.code

    if code.lower() == "all":
        auth_db.clear()
        save_data()
        return {"status": "all_deleted"}

    if code in auth_db:
        del auth_db[code]
        save_data()
        return {"status": "deleted"}

    return {"status": "not_found"}


# ============================================================
#   🔥 코드별 삭제 비밀번호 설정
# ============================================================
@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest):
    global last_admin_code

    if last_admin_code is None:
        return {"error": "no_last_code"}

    if last_admin_code not in auth_db:
        return {"error": "code_not_found"}

    auth_db[last_admin_code]["delete_password"] = req.password
    save_data()

    return {
        "status": "ok",
        "code": last_admin_code,
        "delete_password": req.password
    }


@app.get("/get_delete_pwd")
def get_delete_pwd(code: str):
    if code not in auth_db:
        return {"error": "code_not_found"}

    return {"password": auth_db[code].get("delete_password")}


# ============================================================
#   앱 인증 API
# ============================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    global last_app_code
    code = req.code

    if code not in auth_db:
        return {"status": "invalid"}

    status = auth_db[code]["status"]
    token = auth_db[code]["token"]

    if status == "approved" and token is not None:
        last_app_code = code
        return {"status": "approved", "token": token}

    return {"status": status}


@app.get("/app/delete_password")
def app_delete_password():
    if last_app_code is None:
        return {"password": None}

    data = auth_db.get(last_app_code)
    if not data:
        return {"password": None}

    return {"password": data.get("delete_password")}


# ============================================================
#   관리자 페이지 /tokens (비밀번호 입력 + 모바일 최적화)
# ============================================================
from fastapi.responses import HTMLResponse

@app.get("/tokens", response_class=HTMLResponse)
def admin_page(admin: str = None):

    # 🔐 로그인 여부 확인
    if admin != ADMIN_PASSWORD:
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
            <input type="password" name="admin" placeholder="비밀번호 입력"/>
            <button type="submit">로그인</button>
        </form>

        </body></html>
        """

    # 🔥 목록 출력 페이지
    html = """
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Pocket Blackbox Tokens</title>
        <style>
            body { font-family: Arial; background: #111; color: #eee; padding: 20px; }
            h1 { color: #4DB6AC; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            table, th, td { border: 1px solid #444; }
            th, td { 
                padding: 10px; 
                text-align: left;
                white-space: nowrap; /* 🔥 모바일 줄바꿈 방지 */
            }
            th { background: #222; }
            tr:nth-child(even) { background: #1a1a1a; }
        </style>
    </head>
    <body>

        <h1>🔐 Pocket Blackbox Admin</h1>
        <h2>등록된 토큰 목록</h2>

        <div style="overflow-x:auto; width:100%;">  <!-- 🔥 모바일 가로 스크롤 -->
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
                <td>{data.get('delete_password', '')}</td>
                <td>{data['status']}</td>
                <td>{data['token']}</td>
            </tr>
        """

    html += """
        </table>
        </div>

        <p style="margin-top:50px; color:#777">© Pocket Blackbox Token Interface</p>
    </body>
    </html>
    """

    return html
