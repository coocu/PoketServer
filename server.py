from fastapi import FastAPI
from pydantic import BaseModel
import secrets

app = FastAPI()

# ================================================
#   메모리 저장 DB (원하면 SQLite 버전도 만들어줄게)
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
    """인증키 등록"""
    code = req.code

    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}

    return {"code": code, "status": auth_db[code]["status"]}


@app.post("/approve")
def approve(req: CodeRequest):
    """관리자가 인증키 승인"""
    code = req.code

    # 자동 등록
    if code not in auth_db:
        auth_db[code] = {"status": "pending", "token": None}

    # 랜덤 토큰 생성
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
#   앱 인증 API (여기가 핵심)
# ================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    code = req.code

    # 인증키가 존재하지 않으면 → 인증 불가
    if code not in auth_db:
        return {"status": "invalid"}

    status = auth_db[code]["status"]
    token = auth_db[code]["token"]

    # 승인된 인증키라면 → token 발급 후 인증키 삭제 (1회용)
    if status == "approved" and token is not None:

        # 인증키 즉시 삭제 (1회용 보안 핵심)
        del auth_db[code]

        return {
            "status": "approved",
            "token": token
        }

    # pending 상태
    return {"status": status}


# ================================================
#   앱이 삭제 비밀번호 요청 (인증과 무관)
# ================================================
@app.get("/app/delete_password")
def app_delete_password():
    return {"password": delete_password}
