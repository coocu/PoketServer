from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import secrets
import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from openpyxl import Workbook
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()

# 기존 기본 경로를 그대로 유지합니다.
# Render Persistent Disk를 사용하는 경우 AUTH_DATA_FILE 환경변수로 경로만 바꿀 수 있습니다.
DATA_FILE = os.environ.get("AUTH_DATA_FILE", "auth_data.json")
CATEGORY_FILE = os.environ.get("AUTH_CATEGORY_FILE", "auth_categories.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Kim86110!@")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "poket-admin-session-v1-change-me")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 24 * 30,
)

# 항상 활성 인증키: 기존 로직 유지
ALWAYS_ACTIVE_KEYS = {
    "google-playstore-sign.key",
    "test.kyh",
}

_db_lock = threading.RLock()


def _ensure_parent(path: str):
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def _atomic_json_save(path: str, data):
    """기존 파일을 .bak로 남긴 뒤 원자적으로 교체합니다."""
    _ensure_parent(path)
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    backup = target.with_name(target.name + ".bak")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    if target.exists():
        try:
            shutil.copy2(target, backup)
        except Exception:
            # 백업 복사 실패 때문에 현재 데이터 저장 자체를 막지는 않습니다.
            pass

    os.replace(tmp, target)


def _normalize_record(record: dict) -> dict:
    # 기존 레코드에 없는 새 필드는 기본값으로만 보완합니다.
    record.setdefault("deletedAt", None)
    record.setdefault("category", "미지정")
    record.setdefault("enabled", True)
    return record


def load_data():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("auth_data.json must be a JSON object")
        for key, value in data.items():
            if not isinstance(value, dict):
                raise ValueError(f"invalid record: {key}")
            _normalize_record(value)
        return data
    except Exception as exc:
        # 손상된 파일을 빈 DB로 간주한 뒤 덮어쓰는 사고를 막습니다.
        raise RuntimeError(f"인증키 데이터 로드 실패: {exc}") from exc


def load_categories():
    categories = []
    if os.path.exists(CATEGORY_FILE):
        try:
            with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                categories = [str(x).strip() for x in raw if str(x).strip() and str(x).strip() != "미지정"]
        except Exception:
            categories = []

    # 기존 데이터에 이미 category가 있으면 별도 파일이 없어도 목록에 포함합니다.
    for data in auth_db.values() if "auth_db" in globals() else []:
        category = str(data.get("category") or "미지정").strip()
        if category and category != "미지정" and category not in categories:
            categories.append(category)

    return sorted(set(categories), key=lambda x: x.lower())


def save_data():
    with _db_lock:
        _atomic_json_save(DATA_FILE, auth_db)


def save_categories():
    with _db_lock:
        cleaned = sorted({c.strip() for c in categories if c.strip() and c.strip() != "미지정"}, key=lambda x: x.lower())
        categories[:] = cleaned
        _atomic_json_save(CATEGORY_FILE, categories)


auth_db = load_data()
categories = load_categories()

# 기존 API 호환용 상태값. 새 iOS 앱은 비밀번호 요청에 code도 같이 보내 레이스를 방지합니다.
last_admin_code: str | None = None
last_app_code: str | None = None


# ============================================================
#   요청 모델
# ============================================================
class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str
    code: Optional[str] = None


class RegisterRequest(BaseModel):
    name: str
    phoneLast4: str
    code: str


class UserDeleteRequest(BaseModel):
    name: str
    phoneLast4: str


class CategoryRequest(BaseModel):
    name: str


class CodeCategoryRequest(BaseModel):
    code: str
    category: str = "미지정"


class UpdateAuthRequest(BaseModel):
    originalCode: str
    name: str
    phoneLast4: str
    code: str
    deletePassword: str
    category: str = "미지정"


class FullRegisterRequest(BaseModel):
    name: str
    phoneLast4: str
    code: str
    deletePassword: str
    category: str = "미지정"


class BackupImportRequest(BaseModel):
    data: dict[str, dict]
    replace: bool = False


# ============================================================
#   공통 유틸
# ============================================================
def clean_category(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value else "미지정"


def validate_phone(phone: str):
    if len(phone) != 4 or not phone.isdigit():
        raise HTTPException(status_code=400, detail="phoneLast4 must be exactly 4 digits")


def move_to_trash(code):
    # 호환성을 위해 함수명은 유지하지만, 이제 삭제 요청은 서버에서 즉시 완전 삭제합니다.
    # 기존 API 경로와 호출부는 그대로 유지됩니다.
    with _db_lock:
        if code not in auth_db:
            return False
        del auth_db[code]
        save_data()
        return True


def purge_trash():
    now = datetime.now()
    remove = []
    with _db_lock:
        for code, d in auth_db.items():
            deleted_at = d.get("deletedAt")
            if not deleted_at:
                continue
            try:
                deleted = datetime.strptime(deleted_at, "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if now - deleted > timedelta(days=180):
                remove.append(code)
        for code in remove:
            del auth_db[code]
        if remove:
            save_data()


def activate_code(code: str):
    with _db_lock:
        if code not in auth_db:
            raise HTTPException(status_code=404, detail="code_not_found")
        data = auth_db[code]
        data["deletedAt"] = None
        data["enabled"] = True
        if data.get("status") != "approved":
            data["status"] = "approved"
        if not data.get("token"):
            data["token"] = secrets.token_hex(32)
        save_data()
        return data


def deactivate_code(code: str):
    with _db_lock:
        if code not in auth_db:
            raise HTTPException(status_code=404, detail="code_not_found")
        auth_db[code]["enabled"] = False
        save_data()
        return auth_db[code]


def set_category_for_code(code: str, category: str):
    category = clean_category(category)
    with _db_lock:
        if code not in auth_db:
            raise HTTPException(status_code=404, detail="code_not_found")
        auth_db[code]["category"] = category
        if category != "미지정" and category not in categories:
            categories.append(category)
            save_categories()
        save_data()
        return auth_db[code]


def update_code(req: UpdateAuthRequest):
    validate_phone(req.phoneLast4)
    old_code = req.originalCode.strip()
    new_code = req.code.strip()
    if not old_code or not new_code:
        raise HTTPException(status_code=400, detail="code_required")

    with _db_lock:
        if old_code not in auth_db:
            raise HTTPException(status_code=404, detail="code_not_found")
        if new_code != old_code and new_code in auth_db:
            raise HTTPException(status_code=409, detail="new_code_already_exists")

        data = dict(auth_db[old_code])
        data["name"] = req.name.strip()
        data["phone"] = req.phoneLast4
        data["delete_password"] = req.deletePassword
        data["category"] = clean_category(req.category)
        _normalize_record(data)

        if new_code != old_code:
            del auth_db[old_code]
            auth_db[new_code] = data
        else:
            auth_db[old_code] = data

        if data["category"] != "미지정" and data["category"] not in categories:
            categories.append(data["category"])
            save_categories()

        save_data()
        return new_code, data


def sorted_items(include_deleted: bool = True):
    with _db_lock:
        items = []
        for code, data in auth_db.items():
            if not include_deleted and data.get("deletedAt"):
                continue
            item = {"code": code, **dict(data)}
            item["category"] = clean_category(item.get("category"))
            item["enabled"] = bool(item.get("enabled", True))
            items.append(item)
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return items


purge_trash()


# ============================================================
#   관리자 API (기존 경로/형식 유지)
# ============================================================
@app.post("/register")
def register(req: RegisterRequest):
    global last_admin_code
    code = req.code.strip()
    validate_phone(req.phoneLast4)
    if not code:
        raise HTTPException(status_code=400, detail="code_required")
    last_admin_code = code

    with _db_lock:
        if code not in auth_db:
            auth_db[code] = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "name": req.name,
                "phone": req.phoneLast4,
                "status": "pending",
                "token": None,
                "delete_password": None,
                "deletedAt": None,
                "category": "미지정",
                "enabled": True,
            }
            save_data()

        _normalize_record(auth_db[code])
        return {"code": code, "status": auth_db[code]["status"]}


@app.post("/approve")
def approve(req: CodeRequest):
    global last_admin_code
    code = req.code.strip()
    last_admin_code = code

    with _db_lock:
        if code not in auth_db:
            return {"error": "code_not_found"}

        token = secrets.token_hex(32)
        auth_db[code]["status"] = "approved"
        auth_db[code]["token"] = token
        auth_db[code]["enabled"] = True
        save_data()
        return {"status": "approved", "token": token}


@app.get("/list")
def list_codes():
    # 기존 반환 형식(dict[code] = payload)을 그대로 유지합니다.
    with _db_lock:
        return auth_db


@app.post("/delete")
def delete(req: CodeRequest):
    code = req.code

    if code.lower() == "all":
        with _db_lock:
            auth_db.clear()
            save_data()
        # 기존 클라이언트 호환을 위해 응답 status 문자열은 유지합니다.
        return {"status": "all_moved_to_trash"}

    if move_to_trash(code):
        return {"status": "moved_to_trash"}
    return {"status": "not_found"}


@app.post("/delete_by_user")
def delete_by_user(req: UserDeleteRequest):
    with _db_lock:
        for code, data in auth_db.items():
            if data.get("name") == req.name and data.get("phone") == req.phoneLast4:
                move_to_trash(code)
                return {"status": "moved_to_trash"}
    return {"status": "not_found"}


@app.post("/set_delete_pwd")
def set_delete_pwd(req: PasswordRequest):
    global last_admin_code

    # 새 관리자 앱은 code를 함께 보내므로 동시 요청에서도 정확한 인증키에 비밀번호가 붙습니다.
    # 기존 앱이 password만 보내는 경우에는 기존 last_admin_code 방식으로 그대로 동작합니다.
    target_code = (req.code or last_admin_code or "").strip()
    if not target_code:
        return {"error": "no_last_code"}

    with _db_lock:
        if target_code not in auth_db:
            return {"error": "code_not_found"}
        auth_db[target_code]["delete_password"] = req.password
        last_admin_code = target_code
        save_data()
        return {"status": "ok"}


# ============================================================
#   앱 인증 API (기존 로직 유지 + 명시적 비활성 상태 추가)
# ============================================================
@app.post("/app/check")
def app_check(req: CodeRequest):
    global last_app_code
    code = req.code

    with _db_lock:
        if code not in auth_db:
            return {"status": "invalid"}

        data = auth_db[code]
        _normalize_record(data)

        if data.get("deletedAt"):
            return {"status": "deleted"}

        if not data.get("enabled", True):
            return {"status": "inactive"}

        if data.get("status") == "approved" and data.get("token"):
            last_app_code = code
            result = {"status": "approved", "token": data["token"]}

            # 일반 인증키만 인증 성공 즉시 비활성화합니다.
            # 기존 예외 규칙은 그대로 유지합니다:
            # - ALWAYS_ACTIVE_KEYS에 등록된 인증키
            # - #으로 시작하는 인증키
            # 위 예외 인증키는 인증 후에도 활성 상태를 유지합니다.
            if code not in ALWAYS_ACTIVE_KEYS and not code.startswith("#"):
                data["enabled"] = False
                save_data()

            return result

        return {"status": data.get("status", "pending")}


@app.get("/app/delete_password")
def app_delete_password(code: Optional[str] = None):
    # 기존 호출은 파라미터 없이 그대로 사용 가능.
    # 새 호출은 code를 지정하면 전역 last_app_code 경쟁 없이 안전하게 조회 가능.
    target = code or last_app_code
    with _db_lock:
        if target and target in auth_db:
            return {"password": auth_db[target].get("delete_password")}
    return {"password": None}


# ============================================================
#   iPhone 관리자용 확장 API
# ============================================================
def require_manager(admin: str):
    if admin != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/manage/categories")
def manage_categories(admin: str):
    require_manager(admin)
    return {"categories": ["미지정"] + sorted(categories, key=lambda x: x.lower())}


@app.post("/manage/categories")
def manage_add_category(req: CategoryRequest, admin: str):
    require_manager(admin)
    name = clean_category(req.name)
    if name == "미지정":
        return {"status": "ok", "category": "미지정"}
    with _db_lock:
        if name not in categories:
            categories.append(name)
            save_categories()
    return {"status": "ok", "category": name}


@app.post("/manage/category")
def manage_set_category(req: CodeCategoryRequest, admin: str):
    require_manager(admin)
    data = set_category_for_code(req.code, req.category)
    return {"status": "ok", "category": data.get("category", "미지정")}


@app.post("/manage/activate")
def manage_activate(req: CodeRequest, admin: str):
    require_manager(admin)
    activate_code(req.code)
    return {"status": "active"}


@app.post("/manage/deactivate")
def manage_deactivate(req: CodeRequest, admin: str):
    require_manager(admin)
    deactivate_code(req.code)
    return {"status": "inactive"}


@app.post("/manage/update")
def manage_update(req: UpdateAuthRequest, admin: str):
    require_manager(admin)
    new_code, _ = update_code(req)
    return {"status": "ok", "code": new_code}


@app.post("/manage/delete")
def manage_delete(req: CodeRequest, admin: str):
    require_manager(admin)
    if move_to_trash(req.code):
        return {"status": "moved_to_trash"}
    raise HTTPException(status_code=404, detail="code_not_found")


@app.post("/manage/full_register")
def manage_full_register(req: FullRegisterRequest, admin: str):
    """웹 관리자용. 내부 처리 순서는 기존과 동일: 등록 -> 승인 -> 비밀번호 -> 카테고리."""
    require_manager(admin)
    validate_phone(req.phoneLast4)
    register(RegisterRequest(name=req.name, phoneLast4=req.phoneLast4, code=req.code))
    approve(CodeRequest(code=req.code))
    set_delete_pwd(PasswordRequest(password=req.deletePassword, code=req.code))
    set_category_for_code(req.code, req.category)
    return {"status": "ok", "code": req.code}


@app.post("/manage/import-backup")
def manage_import_backup(req: BackupImportRequest, admin: str):
    """배포 전 /list 백업을 관리자 비밀번호로 복구/병합합니다.
    기본은 병합이며 replace=true일 때만 기존 DB를 비웁니다.
    """
    require_manager(admin)
    imported = 0
    with _db_lock:
        if req.replace:
            auth_db.clear()

        for code, raw in req.data.items():
            if not isinstance(code, str) or not isinstance(raw, dict):
                continue
            record = dict(raw)
            _normalize_record(record)
            auth_db[code] = record
            category = clean_category(record.get("category"))
            if category != "미지정" and category not in categories:
                categories.append(category)
            imported += 1

        save_categories()
        save_data()

    return {"status": "ok", "imported": imported, "total": len(auth_db)}


# ============================================================
#   엑셀 다운로드 (기존 경로 유지)
# ============================================================
@app.get("/tokens/export")
def export_excel(admin: str):
    if admin != ADMIN_PASSWORD:
        return {"error": "unauthorized"}

    wb = Workbook()
    ws = wb.active
    ws.title = "PocketBlackbox"
    ws.append(["날짜", "성함", "전화번호", "인증키", "비밀번호", "카테고리", "활성상태"])

    with _db_lock:
        for code, d in auth_db.items():
            if d.get("deletedAt"):
                continue
            ws.append([
                d.get("date", ""),
                d.get("name", ""),
                d.get("phone", ""),
                code,
                d.get("delete_password", ""),
                clean_category(d.get("category")),
                "활성" if d.get("enabled", True) else "비활성",
            ])

    file_path = "tokens.xlsx"
    wb.save(file_path)
    return FileResponse(
        file_path,
        filename="PocketBlackbox_Tokens.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================
#   휴지통 API (기존 경로 유지)
# ============================================================
@app.get("/trash")
def trash(admin: str):
    if admin != ADMIN_PASSWORD:
        return {"error": "unauthorized"}
    with _db_lock:
        return {c: d for c, d in auth_db.items() if d.get("deletedAt")}


@app.get("/restore")
def restore(code: str, admin: str):
    if admin != ADMIN_PASSWORD:
        return "Unauthorized"
    if code not in auth_db:
        return "Not found"

    activate_code(code)
    return f"""
    <html><meta charset="UTF-8">
    <body style="background:#111;color:#0f0;padding:40px">
    <h2>복원 완료</h2>
    <a href="/tokens?admin={ADMIN_PASSWORD}">돌아가기</a>
    </body></html>
    """


# ============================================================
#   기존 관리자 페이지 (/tokens) 유지
# ============================================================
@app.get("/tokens", response_class=HTMLResponse)
def admin_page(admin: str = None):
    if admin != ADMIN_PASSWORD:
        return """
        <html><meta charset="UTF-8">
        <body style="background:#111;color:#eee;padding:40px;font-family:Arial">
        <h2>🔐 관리자 로그인</h2>
        <form>
            <input type="password" name="admin" placeholder="비밀번호"/>
            <button type="submit">로그인</button>
        </form>
        </body></html>
        """

    html = """
    <html><head><meta charset="UTF-8"><title>Pocket Blackbox Admin</title>
    <style>
    body{background:#111;color:#eee;font-family:Arial;padding:20px}table{border-collapse:collapse;width:100%}
    th,td{border:1px solid #444;padding:10px;white-space:nowrap}th{background:#222}tr:nth-child(even){background:#1a1a1a}
    a{color:#4DB6AC;text-decoration:none}
    </style></head><body>
    <h1>🔐 Pocket Blackbox 관리자</h1>
    """
    html += f'<a href="/tokens/export?admin={ADMIN_PASSWORD}">📥 엑셀 다운로드</a>'
    html += """
    <h2>📌 인증키</h2><table><tr>
    <th>날짜</th><th>성함</th><th>전화번호</th><th>인증키</th><th>비밀번호</th><th>카테고리</th><th>상태</th>
    </tr>
    """
    with _db_lock:
        for code, d in auth_db.items():
            if d.get("deletedAt"):
                continue
            state = "활성" if d.get("enabled", True) else "비활성"
            html += f"<tr><td>{d.get('date','')}</td><td>{d.get('name','')}</td><td>{d.get('phone','')}</td><td>{code}</td><td>{d.get('delete_password','')}</td><td>{clean_category(d.get('category'))}</td><td>{state}</td></tr>"

        html += "</table><h2 style='margin-top:40px'>🗑 휴지통</h2><table><tr><th>삭제일</th><th>성함</th><th>전화번호</th><th>인증키</th><th>복원</th></tr>"
        for code, d in auth_db.items():
            if not d.get("deletedAt"):
                continue
            html += f"<tr><td>{d.get('deletedAt')}</td><td>{d.get('name','')}</td><td>{d.get('phone','')}</td><td>{code}</td><td><a href='/restore?admin={ADMIN_PASSWORD}&code={code}'>복원</a></td></tr>"
    html += "</table></body></html>"
    return html


# ============================================================
#   새 PC 웹 관리자 (/admin)
#   로그인: 서버에 실제 존재하는 인증키 중 문자열에 'kyh'가 포함된 승인 인증키
# ============================================================
def web_logged_in(request: Request) -> bool:
    code = request.session.get("admin_code")
    if not code:
        return False
    with _db_lock:
        data = auth_db.get(code)
        return bool(
            data
            and "kyh" in code.lower()
            and data.get("status") == "approved"
            and data.get("enabled", True)
            and not data.get("deletedAt")
        )


def require_web_login(request: Request):
    if not web_logged_in(request):
        raise HTTPException(status_code=401, detail="login_required")


@app.post("/admin/api/login")
async def web_login(req: CodeRequest, request: Request):
    code = req.code.strip()
    with _db_lock:
        data = auth_db.get(code)
        ok = bool(
            data
            and "kyh" in code.lower()
            and data.get("status") == "approved"
            and data.get("enabled", True)
            and not data.get("deletedAt")
        )
    if not ok:
        raise HTTPException(status_code=401, detail="login_failed")
    request.session["admin_code"] = code
    return {"status": "ok"}


@app.post("/admin/api/logout")
async def web_logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


@app.get("/admin/api/session")
async def web_session(request: Request):
    return {"loggedIn": web_logged_in(request)}


@app.get("/admin/api/list")
async def web_list(request: Request):
    require_web_login(request)
    return {"items": sorted_items(include_deleted=True)}


@app.get("/admin/api/categories")
async def web_categories(request: Request):
    require_web_login(request)
    return {"categories": ["미지정"] + sorted(categories, key=lambda x: x.lower())}


@app.post("/admin/api/categories")
async def web_add_category(req: CategoryRequest, request: Request):
    require_web_login(request)
    name = clean_category(req.name)
    if name != "미지정":
        with _db_lock:
            if name not in categories:
                categories.append(name)
                save_categories()
    return {"status": "ok", "category": name}


@app.post("/admin/api/register")
async def web_register(req: FullRegisterRequest, request: Request):
    require_web_login(request)
    validate_phone(req.phoneLast4)
    # 기존 순서 그대로
    register(RegisterRequest(name=req.name, phoneLast4=req.phoneLast4, code=req.code))
    approve(CodeRequest(code=req.code))
    set_delete_pwd(PasswordRequest(password=req.deletePassword, code=req.code))
    set_category_for_code(req.code, req.category)
    return {"status": "ok", "code": req.code}


@app.post("/admin/api/category")
async def web_category(req: CodeCategoryRequest, request: Request):
    require_web_login(request)
    data = set_category_for_code(req.code, req.category)
    return {"status": "ok", "category": data.get("category")}


@app.post("/admin/api/activate")
async def web_activate(req: CodeRequest, request: Request):
    require_web_login(request)
    activate_code(req.code)
    return {"status": "active"}


@app.post("/admin/api/deactivate")
async def web_deactivate(req: CodeRequest, request: Request):
    require_web_login(request)
    deactivate_code(req.code)
    return {"status": "inactive"}


@app.post("/admin/api/update")
async def web_update(req: UpdateAuthRequest, request: Request):
    require_web_login(request)
    new_code, _ = update_code(req)
    return {"status": "ok", "code": new_code}


@app.post("/admin/api/delete")
async def web_delete(req: CodeRequest, request: Request):
    require_web_login(request)
    if not move_to_trash(req.code):
        raise HTTPException(status_code=404, detail="code_not_found")
    return {"status": "moved_to_trash"}


@app.get("/admin/api/export-json")
async def web_export_json(request: Request):
    require_web_login(request)
    payload = {
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "auth_db": auth_db,
        "categories": categories,
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=PoketAuth_Backup.json"},
    )


ADMIN_HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poket 인증 관리자</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#171717;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:auto;padding:24px}.card{background:#242424;border:1px solid #494949;border-radius:18px;padding:20px;margin-bottom:16px}.row{display:flex;gap:10px;flex-wrap:wrap}.grow{flex:1;min-width:160px}
input,select,button{font:inherit;border-radius:11px;border:1px solid #555;background:#303030;color:#fff;padding:11px 12px}button{cursor:pointer;font-weight:700}button:hover{background:#3a3a3a}.primary{background:#f3f3f3;color:#111}.danger{border-color:#9e4848}.tabs{display:flex;gap:8px;overflow:auto;padding-bottom:8px}.tab.active{background:#f3f3f3;color:#111}.muted{color:#aaa}.hidden{display:none!important}
.list{display:grid;gap:8px}.item{padding:14px;border:1px solid #444;border-radius:13px;background:#202020;cursor:pointer}.item:hover{background:#2a2a2a}.itemtop{display:flex;justify-content:space-between;gap:10px}.badge{font-size:12px;padding:3px 8px;border:1px solid #555;border-radius:999px}.inactive{color:#ffb45e}.deleted{color:#ff7b68}.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.modal{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;padding:18px;z-index:20}.modalbox{width:min(620px,100%);max-height:90vh;overflow:auto;background:#222;border:1px solid #555;border-radius:18px;padding:20px}.actions{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}@media(max-width:720px){.actions{grid-template-columns:1fr 1fr}.wrap{padding:14px}}
label{display:block;font-size:13px;color:#aaa;margin:10px 0 5px}h1,h2,h3{margin-top:0}.spacer{height:8px}
</style></head>
<body><div class="wrap">
<div id="loginCard" class="card"><h2>🔐 관리자 로그인</h2><div class="row"><input id="loginCode" class="grow" type="password" placeholder="인증키"><button class="primary" onclick="login()">로그인</button></div><p id="loginMsg" class="deleted"></p></div>
<div id="app" class="hidden">
<div class="row" style="justify-content:space-between;align-items:center"><h1>Poket 인증 관리자</h1><div class="row"><button onclick="location.href='/admin/api/export-json'">JSON 백업</button><button onclick="logout()">로그아웃</button></div></div>
<div class="card"><h2>인증키 등록</h2><div class="row"><input id="rName" class="grow" placeholder="성함"><input id="rPhone" class="grow" inputmode="numeric" maxlength="4" placeholder="전화번호 끝 4자리"></div><div class="row" style="margin-top:10px"><select id="rCategory" class="grow"></select><button onclick="addCategory()">+ 카테고리 추가</button></div><div class="row" style="margin-top:10px"><input id="rCode" class="grow" placeholder="인증키"><input id="rPwd" class="grow" placeholder="삭제 비밀번호"></div><div class="row" style="margin-top:10px"><button class="primary" onclick="registerCode()">서버 업로드</button><button onclick="clearRegister()">입력값 지우기</button></div></div>
<div class="card"><div class="row" style="justify-content:space-between;align-items:center"><h2>인증키 목록</h2><button id="trashBtn" onclick="toggleTrash()">휴지통 보기</button></div><div id="tabs" class="tabs"></div><input id="search" style="width:100%;margin:8px 0 12px" placeholder="🔍 이름 / 전화번호 / 인증키 검색" oninput="renderList()"><div id="list" class="list"></div></div>
</div></div>
<div id="modal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between"><h2>인증키 상세</h2><button onclick="closeModal()">닫기</button></div><div id="detail"></div><div class="actions" id="detailActions"><button onclick="changeCategory()">카테고리</button><button onclick="activateSelected()">활성화</button><button onclick="deactivateSelected()">비활성화</button><button onclick="editSelected()">수정</button><button class="danger" onclick="deleteSelected()">삭제</button></div></div></div>
<script>
let items=[], categories=['미지정'], selectedCategory='전체', selected=null, showTrash=false;
async function api(path,opt={}){let r=await fetch(path,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let text=await r.text();let data={};try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));return data}
async function boot(){try{let s=await api('/admin/api/session');if(s.loggedIn){showApp();await refresh()}}catch(e){}}
function showApp(){loginCard.classList.add('hidden');app.classList.remove('hidden')}
async function login(){try{await api('/admin/api/login',{method:'POST',body:JSON.stringify({code:loginCode.value.trim()})});loginMsg.textContent='';showApp();await refresh()}catch(e){loginMsg.textContent='로그인 실패';}}
async function logout(){await api('/admin/api/logout',{method:'POST'});location.reload()}
async function refresh(){let [l,c]=await Promise.all([api('/admin/api/list'),api('/admin/api/categories')]);items=l.items||[];categories=c.categories||['미지정'];fillCategories();renderTabs();renderList()}
function fillCategories(){rCategory.innerHTML=categories.map(c=>`<option>${esc(c)}</option>`).join('')}
function renderTabs(){let names=['전체','미지정',...categories.filter(c=>c!=='미지정')];tabs.innerHTML=names.map(c=>`<button class="tab ${selectedCategory===c?'active':''}" onclick="selectCat('${js(c)}')">${esc(c)}</button>`).join('')+`<button class="tab" onclick="addCategory()">＋</button>`}
function selectCat(c){selectedCategory=c;renderTabs();renderList()}
function toggleTrash(){showTrash=!showTrash;trashBtn.textContent=showTrash?'인증키 보기':'휴지통 보기';renderList()}
function filtered(){let q=search.value.trim().toLowerCase();return items.filter(x=>{let deleted=!!x.deletedAt;if(showTrash!==deleted)return false;if(!showTrash&&selectedCategory!=='전체'&&(x.category||'미지정')!==selectedCategory)return false;return !q||[x.code,x.name,x.phone].some(v=>(v||'').toLowerCase().includes(q))})}
function renderList(){let a=filtered();list.innerHTML=a.length?a.map(x=>`<div class="item" onclick="openItem('${js(x.code)}')"><div class="itemtop"><b>${esc(x.name||'')}</b><span class="badge ${(x.deletedAt?'deleted':(!x.enabled?'inactive':''))}">${x.deletedAt?'삭제됨':(x.enabled?'활성':'비활성')}</span></div><div class="muted">${esc(x.phone||'')} · ${esc(x.category||'미지정')} · ${esc(x.date||'')}</div><div class="code">${esc(x.code)}</div></div>`).join(''):'<p class="muted">표시할 인증키가 없습니다.</p>'}
function openItem(code){selected=items.find(x=>x.code===code);if(!selected)return;detail.innerHTML=`<label>성함</label><div>${esc(selected.name||'')}</div><label>전화번호</label><div>${esc(selected.phone||'')}</div><label>인증키</label><div class="code">${esc(selected.code)}</div><label>삭제 비밀번호</label><div>${esc(selected.delete_password||'')}</div><label>카테고리</label><div>${esc(selected.category||'미지정')}</div><label>등록일</label><div>${esc(selected.date||'')}</div><label>상태</label><div>${selected.deletedAt?'삭제됨':(selected.enabled?'활성':'비활성')}</div>`;modal.classList.remove('hidden')}
function closeModal(){modal.classList.add('hidden');selected=null}
async function addCategory(){let n=prompt('추가할 카테고리 이름');if(!n||!n.trim())return;await api('/admin/api/categories',{method:'POST',body:JSON.stringify({name:n.trim()})});await refresh();rCategory.value=n.trim()}
async function changeCategory(){if(!selected)return;let n=prompt('변경할 카테고리\n현재: '+(selected.category||'미지정')+'\n\n기존 카테고리: '+categories.join(', '),selected.category||'미지정');if(n===null)return;n=n.trim()||'미지정';if(n!=='미지정'&&!categories.includes(n))await api('/admin/api/categories',{method:'POST',body:JSON.stringify({name:n})});await api('/admin/api/category',{method:'POST',body:JSON.stringify({code:selected.code,category:n})});closeModal();await refresh()}
async function activateSelected(){if(!selected)return;await api('/admin/api/activate',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
async function deactivateSelected(){if(!selected)return;await api('/admin/api/deactivate',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
async function editSelected(){if(!selected)return;let name=prompt('성함',selected.name||'');if(name===null)return;let phone=prompt('전화번호 끝 4자리',selected.phone||'');if(phone===null)return;let code=prompt('인증키',selected.code);if(code===null)return;let pwd=prompt('삭제 비밀번호',selected.delete_password||'');if(pwd===null)return;let cat=prompt('카테고리',selected.category||'미지정');if(cat===null)return;await api('/admin/api/update',{method:'POST',body:JSON.stringify({originalCode:selected.code,name:name.trim(),phoneLast4:phone.trim(),code:code.trim(),deletePassword:pwd,category:cat.trim()||'미지정'})});closeModal();await refresh()}
async function deleteSelected(){if(!selected||!confirm('이 인증키를 서버에서 완전히 삭제할까요?\n삭제 후에는 복구할 수 없습니다.'))return;await api('/admin/api/delete',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
async function registerCode(){let o={name:rName.value.trim(),phoneLast4:rPhone.value.trim(),category:rCategory.value||'미지정',code:rCode.value.trim(),deletePassword:rPwd.value};if(!o.name||!/^[0-9]{4}$/.test(o.phoneLast4)||!o.code||!o.deletePassword){alert('성함 / 전화번호 4자리 / 인증키 / 비밀번호를 모두 입력하세요.');return}try{await api('/admin/api/register',{method:'POST',body:JSON.stringify(o)});clearRegister();await refresh();alert('업로드 완료')}catch(e){alert('업로드 실패: '+e.message)}}
function clearRegister(){rName.value='';rPhone.value='';rCode.value='';rPwd.value='';rCategory.value='미지정'}
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function js(v){return String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}
boot();
</script></body></html>'''


@app.get("/admin", response_class=HTMLResponse)
def web_admin_page():
    return HTMLResponse(ADMIN_HTML)
