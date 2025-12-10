from fastapi import FastAPI
from pydantic import BaseModel
import secrets

app = FastAPI()

# ================================================
#   메모리 저장 DB
# ================================================
auth_db = {}
delete_password = "del1234"

# ================================================
#   요청 모델
# ================================================
class CodeRequest(BaseModel):
    code: str

class PasswordRequest(BaseModel):
    password: str


# ================================================
#   관리자 API
# ================================================
@app.post("/register")
def register(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}

    return {"code": code, "status": auth_db[code]["status"]}


@app.post("/approve")
def approve(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}

    token = secrets.token_hex(32)
    auth_db[code]["status"] = "approved"
    auth_db[code]["token"] = token

    return {"status": "approved", "token": token}


@app.get("/list")
def list_codes():
    return auth_db


@app.post("/delete")
def delete(req: CodeRequest):
    code = req.code
    if code in auth_db:
        del auth_db[code]
        return {"status": "deleted"}
    return {"status": "not_found"}


@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest):
    global delete_password
    delete_password = req.password
    return {"status": "ok"}


@app.get("/get_delete_pwd")
def get_delete_pwd():
    return {"password": delete_password}



# ================================================
#   앱 인증 API (수정됨!) 자동 삭제 없어짐
# ================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    code = req.code

    if code not in auth_db:
        return {"status": "invalid"}

    status = auth_db[code]["status"]
    token = auth_db[code]["token"]

    # 승인됨 → 바로 token 반환 (삭제 없음)
    if status == "approved" and token is not None:
        return {
            "status": "approved",
            "token": token
        }

    return {"status": status}


# ================================================
#   앱 삭제 비밀번호 요청
# ================================================
@app.get("/app/delete_password")
def app_delete_password():
    return {"password": delete_password}
