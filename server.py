from fastapi import FastAPI
from pydantic import BaseModel
import secrets
import json
import os

app = FastAPI()

DATA_FILE = "auth_data.json"


# ============================================================
#   JSON 저장/로드 기능 (추가된 부분)
# ============================================================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}, "del1234"

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("auth_db", {}), data.get("delete_password", "del1234")
    except:
        return {}, "del1234"


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "auth_db": auth_db,
            "delete_password": delete_password
        }, f, ensure_ascii=False, indent=2)


# ============================================================
#   메모리 DB (서버 실행 시 JSON에서 자동 복구됨)
# ============================================================
auth_db, delete_password = load_data()


# ============================================================
#   요청 모델
# ============================================================
class CodeRequest(BaseModel):
    code: str

class PasswordRequest(BaseModel):
    password: str


# ============================================================
#   관리자 API — 기존 로직 유지
# ============================================================
@app.post("/register")
def register(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}
        save_data()

    return {"code": code, "status": auth_db[code]["status"]}


@app.post("/approve")
def approve(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}

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

    # ===============================================
    # 1) ALL 삭제 기능 추가 (보안 중요)
    # ===============================================
    if code.lower() == "all":
        auth_db.clear()
        save_data()
        return {"status": "all_deleted"}

    # ===============================================
    # 2) 기존 기능 유지 — 단일 코드 삭제
    # ===============================================
    if code in auth_db:
        del auth_db[code]
        save_data()
        return {"status": "deleted"}

    return {"status": "not_found"}


@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest):
    global delete_password
    delete_password = req.password
    save_data()
    return {"status": "ok"}


@app.get("/get_delete_pwd")
def get_delete_pwd():
    return {"password": delete_password}


# ============================================================
#   앱 인증 API — 기존 로직 절대 변경 없음
# ============================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        return {"status": "invalid"}

    status = auth_db[code]["status"]
    token = auth_db[code]["token"]

    if status == "approved" and token is not None:
        return {
            "status": "approved",
            "token": token
        }

    return {"status": status}


# ============================================================
#   앱 삭제 비밀번호 요청 — 기존 로직 유지
# ============================================================
@app.get("/app/delete_password")
def app_delete_password():
    return {"password": delete_password}
