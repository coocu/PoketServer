from fastapi import FastAPI
from pydantic import BaseModel
import secrets
import json
import os
from datetime import datetime

from fastapi.responses import HTMLResponse, FileResponse
from openpyxl import Workbook

app = FastAPI()

DATA_FILE = "auth_data.json"
ADMIN_PASSWORD = "Kim86110!@"


# ============================================================
#   JSON 저장/로드
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


auth_db = load_data()

last_admin_code: str | None = None
last_app_code: str | None = None


# ============================================================
#   요청 모델
# ============================================================
class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


class RegisterRequest(BaseModel):
    name: str
    phoneLast4: str
    code: str


# 🔥 추가: 사용자 기준 삭제 요청
class UserDeleteRequest(BaseModel):
    name: str
    phoneLast4: str


# ============================================================
#   관리자 API
# ============================================================
@app.post("/register")
def register(req: RegisterRequest):
    global last_admin_code
    code = req.code
    last_admin_code = code

    if code not in auth_db:
        auth_db[code] = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "name": req.name,
            "phone": req.phoneLast4,
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
        return {"error": "code_not_found"}

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
#   🔥 추가: 앱용 사용자 기준 삭제 API
# ============================================================
@app.post("/delete_by_user")
def delete_by_user(req: UserDeleteRequest):
    target_code = None

    for code, data in auth_db.items():
        if data.get("name") == req.name and data.get("phone") == req.phoneLast4:
            target_code = code
            break

    if not target_code:
        return {"status": "not_found"}

    del auth_db[target_code]
    save_data()

    return {
        "status": "deleted",
        "name": req.name,
        "phone": req.phoneLast4
    }


@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest):
    global last_admin_code

    if last_admin_code is None:
        return {"error": "no_last_code"}

    if last_admin_code not in auth_db:
        return {"error": "code_not_found"}

    auth_db[last_admin_code]["delete_password"] = req.password
    save_data()

    return {"status": "ok"}


# ============================================================
#   앱 인증 API (변경 없음)
# ============================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    global last_app_code
    code = req.code

    if code not in auth_db:
        return {"status": "invalid"}

    data = auth_db[code]
    if data["status"] == "approved" and data["token"]:
        last_app_code = code
        return {"status": "approved", "token": data["token"]}

    return {"status": data["status"]}


@app.get("/app/delete_password")
def app_delete_password():
    if last_app_code and last_app_code in auth_db:
        return {"password": auth_db[last_app_code].get("delete_password")}
    return {"password": None}


# ============================================================
#   📥 엑셀 다운로드 (변경 없음)
# ============================================================
@app.get("/tokens/export")
def export_excel(admin: str):
    if admin != ADMIN_PASSWORD:
        return {"error": "unauthorized"}

    wb = Workbook()
    ws = wb.active
    ws.title = "PocketBlackbox"

    ws.append(["날짜", "성함", "전화번호", "인증키", "비밀번호"])

    for code, d in auth_db.items():
        ws.append([
            d.get("date", ""),
            d.get("name", ""),
            d.get("phone", ""),
            code,
            d.get("delete_password", "")
        ])

    file_path = "tokens.xlsx"
    wb.save(file_path)

    return FileResponse(
        file_path,
        filename="PocketBlackbox_Tokens.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ============================================================
#   관리자 페이지 (변경 없음)
# ============================================================
@app.get("/tokens", response_class=HTMLResponse)
def admin_page(admin: str = None):

    if admin != ADMIN_PASSWORD:
        return """
        <html><meta charset="UTF-8">
        <body style="background:#111;color:#eee;padding:40px">
        <h2>🔐 관리자 로그인</h2>
        <form>
            <input type="password" name="admin" placeholder="비밀번호"/>
            <button type="submit">로그인</button>
        </form>
        </body></html>
        """

    html = """
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Pocket Blackbox Admin</title>
        <style>
            body { background:#111; color:#eee; font-family:Arial; padding:20px; }
            table { border-collapse: collapse; width:100%; }
            th, td {
                border:1px solid #444;
                padding:10px;
                white-space: nowrap;
            }
            th { background:#222; }
            tr:nth-child(even){background:#1a1a1a;}
            a { color:#4DB6AC; text-decoration:none; }
        </style>
    </head>
    <body>

    <h1>🔐 Pocket Blackbox 관리자</h1>
    <a href="/tokens/export?admin=""" + ADMIN_PASSWORD + """">📥 엑셀 다운로드</a>

    <div style="overflow-x:auto;margin-top:20px;">
    <table>
        <tr>
            <th>날짜</th>
            <th>성함</th>
            <th>전화번호</th>
            <th>인증키</th>
            <th>비밀번호</th>
        </tr>
    """

    for code, d in auth_db.items():
        html += f"""
        <tr>
            <td>{d.get("date","")}</td>
            <td>{d.get("name","")}</td>
            <td>{d.get("phone","")}</td>
            <td>{code}</td>
            <td>{d.get("delete_password","")}</td>
        </tr>
        """

    html += """
    </table>
    </div>

    </body>
    </html>
    """

    return html
