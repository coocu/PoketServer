from fastapi import FastAPI
from pydantic import BaseModel
import secrets
import json
import os
from datetime import datetime, timedelta
from fastapi.responses import HTMLResponse, FileResponse
from openpyxl import Workbook

app = FastAPI()

DATA_FILE = "auth_data.json"
ADMIN_PASSWORD = "Kim86110!@"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k in data:
                if "deletedAt" not in data[k]:
                    data[k]["deletedAt"] = None
            return data
    except:
        return {}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(auth_db, f, ensure_ascii=False, indent=2)

auth_db = load_data()

last_admin_code = None
last_app_code = None

class CodeRequest(BaseModel):
    code: str

class PasswordRequest(BaseModel):
    password: str

class RegisterRequest(BaseModel):
    name: str
    phoneLast4: str
    code: str

class UserDeleteRequest(BaseModel):
    name: str
    phoneLast4: str

def move_to_trash(code):
    auth_db[code]["deletedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_data()

def purge_trash():
    now = datetime.now()
    remove = []
    for code, d in auth_db.items():
        if d.get("deletedAt"):
            deleted = datetime.strptime(d["deletedAt"], "%Y-%m-%d %H:%M")
            if now - deleted > timedelta(days=180):
                remove.append(code)
    for c in remove:
        del auth_db[c]
    if remove:
        save_data()

purge_trash()

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
            "delete_password": None,
            "deletedAt": None
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
        for c in auth_db:
            auth_db[c]["deletedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_data()
        return {"status": "all_moved_to_trash"}
    if code in auth_db:
        move_to_trash(code)
        return {"status": "moved_to_trash"}
    return {"status": "not_found"}

@app.post("/delete_by_user")
def delete_by_user(req: UserDeleteRequest):
    for code, data in auth_db.items():
        if data.get("name") == req.name and data.get("phone") == req.phoneLast4:
            move_to_trash(code)
            return {"status": "moved_to_trash"}
    return {"status": "not_found"}

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

@app.post("/app/check")
def app_check(req: CodeRequest):
    global last_app_code
    code = req.code
    if code not in auth_db:
        return {"status": "invalid"}
    data = auth_db[code]
    if data.get("deletedAt"):
        return {"status": "deleted"}
    if data["status"] == "approved" and data["token"]:
        last_app_code = code
        return {"status": "approved", "token": data["token"]}
    return {"status": data["status"]}

@app.get("/app/delete_password")
def app_delete_password():
    if last_app_code and last_app_code in auth_db:
        return {"password": auth_db[last_app_code].get("delete_password")}
    return {"password": None}

@app.get("/tokens/export")
def export_excel(admin: str):
    if admin != ADMIN_PASSWORD:
        return {"error": "unauthorized"}
    wb = Workbook()
    ws = wb.active
    ws.title = "PocketBlackbox"
    ws.append(["날짜", "성함", "전화번호", "인증키", "비밀번호"])
    for code, d in auth_db.items():
        if d.get("deletedAt"):
            continue
        ws.append([d.get("date",""), d.get("name",""), d.get("phone",""), code, d.get("delete_password","")])
    file_path = "tokens.xlsx"
    wb.save(file_path)
    return FileResponse(file_path, filename="PocketBlackbox_Tokens.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.get("/trash")
def trash(admin: str):
    if admin != ADMIN_PASSWORD:
        return {"error": "unauthorized"}
    return {c:d for c,d in auth_db.items() if d.get("deletedAt")}

@app.get("/restore")
def restore(code: str, admin: str):
    if admin != ADMIN_PASSWORD:
        return "Unauthorized"
    if code not in auth_db:
        return "Not found"
    auth_db[code]["deletedAt"] = None
    save_data()
    return f"<html><meta charset='UTF-8'><body style='background:#111;color:#0f0;padding:40px'><h2>복원 완료</h2><a href='/tokens?admin={ADMIN_PASSWORD}'>돌아가기</a></body></html>"
