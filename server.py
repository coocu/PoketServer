from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import secrets
import json
import os
import shutil
import threading
import io
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional
import jwt
from jwt import PyJWKClient
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from openpyxl import Workbook
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()

# 기존 기본 경로를 그대로 유지합니다.
# Render Persistent Disk를 사용하는 경우 AUTH_DATA_FILE 환경변수로 경로만 바꿀 수 있습니다.
DATA_FILE = os.environ.get("AUTH_DATA_FILE", "auth_data.json")
CATEGORY_FILE = os.environ.get("AUTH_CATEGORY_FILE", "auth_categories.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Kim86110!@")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "poket-admin-session-v1-change-me")
APPLE_ADMIN_FILE = os.environ.get("APPLE_ADMIN_FILE", "apple_admins.json")
APPLE_CLIENT_ID = os.environ.get("APPLE_CLIENT_ID", "com.codenote.id")
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
DATA_FILE_EXISTED_AT_BOOT = os.path.exists(DATA_FILE)

KST = ZoneInfo("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

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
    "codenote.kyh",
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


def _normalize_apple_admin(record: dict) -> dict:
    record.setdefault("provider", "apple")
    record.setdefault("label", "")
    record.setdefault("registeredAt", now_kst().isoformat(timespec="seconds"))
    record.setdefault("allowedCategory", None)
    return record


def load_apple_admins():
    if not os.path.exists(APPLE_ADMIN_FILE):
        return {}
    try:
        with open(APPLE_ADMIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("apple_admins.json must be a JSON object")
        normalized = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError("invalid apple admin record")
            normalized[key] = _normalize_apple_admin(dict(value))
        return normalized
    except Exception as exc:
        raise RuntimeError(f"Apple 관리자 데이터 로드 실패: {exc}") from exc


def save_apple_admins():
    with _db_lock:
        _atomic_json_save(APPLE_ADMIN_FILE, apple_admins)


auth_db = load_data()
categories = load_categories()
apple_admins = load_apple_admins()


def ensure_bootstrap_developer_key():
    # 운영 데이터 파일 자체가 없는 완전 초기 상태에서만 기본 관리자 인증키를 생성합니다.
    if DATA_FILE_EXISTED_AT_BOOT:
        return
    with _db_lock:
        if "개발자" not in categories:
            categories.append("개발자")
        auth_db["kyh"] = {
            "date": now_kst().strftime("%Y-%m-%d %H:%M"),
            "name": "개발자",
            "phone": "0000",
            "status": "approved",
            "token": secrets.token_hex(32),
            "delete_password": "del",
            "deletedAt": None,
            "category": "개발자",
            "enabled": True,
        }
        save_categories()
        save_data()


ensure_bootstrap_developer_key()

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


class CategoryRenameRequest(BaseModel):
    oldName: str
    newName: str


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


class AppleIdentityRequest(BaseModel):
    identityToken: str
    nonce: str


class AppleAdminRegisterRequest(BaseModel):
    identityToken: str
    nonce: str
    label: str
    code: str


class AppleAdminManageAccessRequest(BaseModel):
    code: str


class AppleAdminUpdateRequest(BaseModel):
    userId: str
    label: str
    allowedCategory: Optional[str] = None


class AppleAdminDeleteRequest(BaseModel):
    userId: str


class AppleAdminUploadRequest(BaseModel):
    name: str
    phoneLast4: str
    code: str
    deletePassword: str
    category: str = "미지정"


# ============================================================
#   공통 유틸
# ============================================================
def clean_category(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value else "미지정"


def validate_phone(phone: str):
    if len(phone) != 4 or not phone.isdigit():
        raise HTTPException(status_code=400, detail="phoneLast4 must be exactly 4 digits")


def manager_list_access_allowed(code: str) -> bool:
    """인증키 목록 접근용 서버 검증.
    클라이언트에는 조건을 노출하지 않고 서버 데이터만으로 판정합니다.
    """
    code = (code or "").strip()
    if not code:
        return False
    with _db_lock:
        data = auth_db.get(code)
        if not data:
            return False
        _normalize_record(data)
        category = clean_category(data.get("category"))
        return bool(
            "kyh" in code.lower()
            and "개발자" in category
            and data.get("status") == "approved"
            and data.get("enabled", True)
            and not data.get("deletedAt")
        )


def _registration_code_uses_existing_exception_rule(code: str) -> bool:
    # 기존 /app/check의 예외 규칙을 그대로 재사용합니다.
    return code in ALWAYS_ACTIVE_KEYS or code.startswith("#")


def validate_and_consume_kyh_code(code: str) -> dict:
    code = (code or "").strip()
    if not code or "kyh" not in code.lower():
        raise HTTPException(status_code=401, detail="invalid_registration_code")
    with _db_lock:
        data = auth_db.get(code)
        if not data:
            raise HTTPException(status_code=401, detail="invalid_registration_code")
        _normalize_record(data)
        if data.get("deletedAt") or data.get("status") != "approved" or not data.get("enabled", True):
            raise HTTPException(status_code=401, detail="invalid_registration_code")
        if not _registration_code_uses_existing_exception_rule(code):
            data["enabled"] = False
            save_data()
        return dict(data)


_apple_jwk_client = PyJWKClient(APPLE_JWKS_URL)
_apple_session_serializer = URLSafeSerializer(SESSION_SECRET, salt="codenote-apple-admin-session-v1")
_apple_manage_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="codenote-apple-admin-manage-v1")


def verify_apple_identity_token(identity_token: str, nonce: str) -> str:
    token = (identity_token or "").strip()
    expected_nonce = (nonce or "").strip()
    if not token or not expected_nonce:
        raise HTTPException(status_code=401, detail="invalid_apple_credential")
    try:
        signing_key = _apple_jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer="https://appleid.apple.com",
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_apple_credential") from exc
    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=401, detail="invalid_apple_nonce")
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_apple_user")
    return user_id


def issue_apple_session(user_id: str) -> str:
    return _apple_session_serializer.dumps({"provider": "apple", "userId": user_id})


def apple_admin_profile(user_id: str) -> dict:
    record = apple_admins.get(user_id)
    if not record:
        raise HTTPException(status_code=401, detail="apple_admin_not_registered")
    record = _normalize_apple_admin(record)
    return {
        "userId": user_id,
        "provider": "apple",
        "label": record.get("label", ""),
        "registeredAt": record.get("registeredAt", ""),
        "allowedCategory": record.get("allowedCategory"),
    }


def require_apple_session_token(token: str) -> tuple[str, dict]:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="apple_login_required")
    try:
        payload = _apple_session_serializer.loads(token)
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="invalid_apple_session") from exc
    user_id = str(payload.get("userId") or "").strip() if isinstance(payload, dict) else ""
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_apple_session")
    with _db_lock:
        if user_id not in apple_admins:
            raise HTTPException(status_code=401, detail="apple_admin_removed")
        return user_id, apple_admin_profile(user_id)


def require_apple_session(request: Request) -> tuple[str, dict]:
    return require_apple_session_token(request.headers.get("X-Apple-Session", ""))


def issue_manage_token(user_id: str) -> str:
    return _apple_manage_serializer.dumps({"userId": user_id})


def require_manage_token(request: Request, user_id: str):
    token = request.headers.get("X-Manage-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail="manage_access_required")
    try:
        payload = _apple_manage_serializer.loads(token, max_age=60 * 15)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=401, detail="manage_access_expired") from exc
    if not isinstance(payload, dict) or payload.get("userId") != user_id:
        raise HTTPException(status_code=401, detail="manage_access_denied")


def validate_upload_category(profile: dict, requested: str) -> str:
    allowed = profile.get("allowedCategory")
    if allowed is None or str(allowed).strip() == "":
        raise HTTPException(status_code=403, detail="upload_category_not_assigned")
    requested = clean_category(requested)
    if allowed == "전체":
        return requested
    if requested != allowed:
        raise HTTPException(status_code=403, detail="category_not_allowed")
    return str(allowed)


@app.middleware("http")
async def enforce_registered_apple_admin_for_iphone_requests(request: Request, call_next):
    # 기존 앱은 이 헤더를 보내지 않으므로 기존 API 동작은 그대로 유지됩니다.
    token = request.headers.get("X-Apple-Session", "")
    if token:
        try:
            require_apple_session_token(token)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


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
    now = now_kst().replace(tzinfo=None)
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


def rename_category_and_reassign(old_name: str, new_name: str) -> int:
    """PC 웹 관리자 전용 카테고리 이름 수정.
    해당 카테고리의 인증키는 그대로 유지하고 category 값만 새 이름으로 변경합니다.
    새 이름이 이미 존재하면 그 카테고리로 병합합니다.
    """
    old_name = clean_category(old_name)
    new_name = clean_category(new_name)

    if old_name == "미지정":
        raise HTTPException(status_code=400, detail="cannot_rename_unspecified")
    if new_name == "미지정":
        raise HTTPException(status_code=400, detail="cannot_rename_to_unspecified")
    if old_name == new_name:
        return 0

    with _db_lock:
        if old_name not in categories:
            raise HTTPException(status_code=404, detail="category_not_found")

        moved = 0
        for data in auth_db.values():
            if clean_category(data.get("category")) == old_name:
                data["category"] = new_name
                moved += 1

        # 기존 카테고리는 제거하고, 새 이름이 없을 때만 추가합니다.
        categories[:] = [c for c in categories if c != old_name]
        if new_name not in categories:
            categories.append(new_name)

        for admin_record in apple_admins.values():
            if admin_record.get("allowedCategory") == old_name:
                admin_record["allowedCategory"] = new_name
        save_data()
        save_categories()
        save_apple_admins()
        return moved


def delete_category_and_reassign(name: str) -> int:
    """PC 웹 관리자 전용 카테고리 삭제.
    카테고리 자체만 삭제하고 해당 인증키는 삭제하지 않으며 모두 '미지정'으로 이동합니다.
    """
    name = clean_category(name)
    if name == "미지정":
        raise HTTPException(status_code=400, detail="cannot_delete_unspecified")

    with _db_lock:
        if name not in categories:
            raise HTTPException(status_code=404, detail="category_not_found")

        moved = 0
        for data in auth_db.values():
            if clean_category(data.get("category")) == name:
                data["category"] = "미지정"
                moved += 1

        categories[:] = [c for c in categories if c != name]
        # 먼저 인증키 데이터를 저장하고, 이후 카테고리 목록을 저장합니다.
        # 각 저장 함수는 기존 파일을 .bak로 남깁니다.
        for admin_record in apple_admins.values():
            if admin_record.get("allowedCategory") == name:
                admin_record["allowedCategory"] = None
        save_data()
        save_categories()
        save_apple_admins()
        return moved


def build_full_backup_zip() -> bytes:
    """인증키/비밀번호/토큰/상태/카테고리 등 운영 데이터를 ZIP 하나로 백업합니다."""
    with _db_lock:
        auth_snapshot = {code: dict(data) for code, data in auth_db.items()}
        category_snapshot = list(categories)
        apple_admin_snapshot = {user_id: dict(data) for user_id, data in apple_admins.items()}

    manifest = {
        "format": "codenote-auth-backup",
        "version": 1,
        "exportedAt": now_kst().isoformat(timespec="seconds"),
        "records": len(auth_snapshot),
        "categories": len(category_snapshot),
        "appleAdmins": len(apple_admin_snapshot),
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("auth_data.json", json.dumps(auth_snapshot, ensure_ascii=False, indent=2))
        zf.writestr("auth_categories.json", json.dumps(category_snapshot, ensure_ascii=False, indent=2))
        zf.writestr("apple_admins.json", json.dumps(apple_admin_snapshot, ensure_ascii=False, indent=2))
    return out.getvalue()


def restore_full_backup_zip(raw: bytes) -> tuple[int, int]:
    """백업 ZIP을 검증한 뒤 서버 운영 데이터를 해당 시점으로 전체 복원합니다."""
    if not raw:
        raise HTTPException(status_code=400, detail="empty_backup")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="backup_too_large")

    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            # 외부 ZIP 파일명은 사용하지 않고 압축 내부 JSON을 기준으로 복원합니다.
            members = {Path(name).name.lower(): name for name in zf.namelist() if not name.endswith("/")}
            required = {"manifest.json", "auth_data.json", "auth_categories.json"}
            if not required.issubset(members):
                raise ValueError("required_json_files_missing")

            manifest = json.loads(zf.read(members["manifest.json"]).decode("utf-8-sig"))
            incoming_db = json.loads(zf.read(members["auth_data.json"]).decode("utf-8-sig"))
            incoming_categories = json.loads(zf.read(members["auth_categories.json"]).decode("utf-8-sig"))
            incoming_apple_admins = (
                json.loads(zf.read(members["apple_admins.json"]).decode("utf-8-sig"))
                if "apple_admins.json" in members else None
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_backup: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("format") != "codenote-auth-backup" or manifest.get("version") != 1:
        raise HTTPException(status_code=400, detail="unsupported_backup")
    if not isinstance(incoming_db, dict):
        raise HTTPException(status_code=400, detail="invalid_auth_data")
    if not isinstance(incoming_categories, list):
        raise HTTPException(status_code=400, detail="invalid_categories")

    normalized_db: dict[str, dict] = {}
    for code, record in incoming_db.items():
        if not isinstance(code, str) or not code.strip() or not isinstance(record, dict):
            raise HTTPException(status_code=400, detail="invalid_record")
        item = dict(record)
        _normalize_record(item)
        item["category"] = clean_category(item.get("category"))
        normalized_db[code] = item

    normalized_categories: list[str] = []
    for value in incoming_categories:
        name = clean_category(str(value))
        if name != "미지정" and name not in normalized_categories:
            normalized_categories.append(name)

    # 백업 파일의 인증키 레코드에만 존재하는 카테고리도 유실하지 않습니다.
    for item in normalized_db.values():
        name = clean_category(item.get("category"))
        if name != "미지정" and name not in normalized_categories:
            normalized_categories.append(name)

    normalized_apple_admins = None
    if incoming_apple_admins is not None:
        if not isinstance(incoming_apple_admins, dict):
            raise HTTPException(status_code=400, detail="invalid_apple_admins")
        normalized_apple_admins = {}
        for user_id, record in incoming_apple_admins.items():
            if not isinstance(user_id, str) or not user_id.strip() or not isinstance(record, dict):
                raise HTTPException(status_code=400, detail="invalid_apple_admin_record")
            normalized_apple_admins[user_id] = _normalize_apple_admin(dict(record))

    with _db_lock:
        auth_db.clear()
        auth_db.update(normalized_db)
        categories[:] = normalized_categories
        if normalized_apple_admins is not None:
            apple_admins.clear()
            apple_admins.update(normalized_apple_admins)
        # _atomic_json_save가 현재 서버 파일을 .bak로 남긴 뒤 교체합니다.
        save_data()
        save_categories()
        if normalized_apple_admins is not None:
            save_apple_admins()

    return len(normalized_db), len(normalized_categories)



def _normalize_restore_payload(incoming_db, incoming_categories=None) -> tuple[dict[str, dict], list[str]]:
    """JSON/ZIP 공통 복원 검증. 기존 인증키의 모든 필드는 그대로 보존합니다."""
    if not isinstance(incoming_db, dict):
        raise HTTPException(status_code=400, detail="invalid_auth_data")

    normalized_db: dict[str, dict] = {}
    for code, record in incoming_db.items():
        if not isinstance(code, str) or not code.strip() or not isinstance(record, dict):
            raise HTTPException(status_code=400, detail="invalid_record")
        item = dict(record)
        _normalize_record(item)
        item["category"] = clean_category(item.get("category"))
        normalized_db[code] = item

    normalized_categories: list[str] = []
    if incoming_categories is not None:
        if not isinstance(incoming_categories, list):
            raise HTTPException(status_code=400, detail="invalid_categories")
        for value in incoming_categories:
            name = clean_category(str(value))
            if name != "미지정" and name not in normalized_categories:
                normalized_categories.append(name)

    # 카테고리 배열이 없거나 누락된 카테고리가 있어도 인증키 데이터에서 자동 복원합니다.
    for item in normalized_db.values():
        name = clean_category(item.get("category"))
        if name != "미지정" and name not in normalized_categories:
            normalized_categories.append(name)

    return normalized_db, normalized_categories


def _replace_server_data(normalized_db: dict[str, dict], normalized_categories: list[str]) -> tuple[int, int]:
    """현재 운영 데이터를 백업 내용으로 전체 복원합니다. 저장 시 기존 파일은 .bak로 남습니다."""
    with _db_lock:
        auth_db.clear()
        auth_db.update(normalized_db)
        categories[:] = normalized_categories
        save_data()
        save_categories()
    return len(normalized_db), len(normalized_categories)


def restore_full_backup_json(raw: bytes) -> tuple[int, int]:
    """기존 PC JSON 백업 또는 raw auth_data.json을 전체 복원합니다."""
    if not raw:
        raise HTTPException(status_code=400, detail="empty_backup")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="backup_too_large")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json_backup: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_json_backup")

    # 현재 사용 중인 PC JSON 백업 형식:
    # {"exportedAt":..., "auth_db": {...}, "categories": [...]}
    if "auth_db" in payload:
        incoming_db = payload.get("auth_db")
        incoming_categories = payload.get("categories", [])
    else:
        # 과거 auth_data.json 자체를 백업한 파일도 복원 가능하게 유지합니다.
        incoming_db = payload
        incoming_categories = None

    normalized_db, normalized_categories = _normalize_restore_payload(incoming_db, incoming_categories)
    return _replace_server_data(normalized_db, normalized_categories)


def restore_backup_auto(raw: bytes, filename: str = "", content_type: str = "") -> tuple[int, int, str]:
    """파일 확장자/내용을 판별해 JSON 또는 ZIP 백업을 복원합니다."""
    # 파일명/확장자와 무관하게 실제 ZIP 시그니처로 판별합니다.
    is_zip = raw[:4] == b"PK\x03\x04"
    if is_zip:
        records, category_count = restore_full_backup_zip(raw)
        return records, category_count, "zip"
    records, category_count = restore_full_backup_json(raw)
    return records, category_count, "json"


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
                "date": now_kst().strftime("%Y-%m-%d %H:%M"),
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
#   Apple 로그인 / Apple 관리자 권한 API (기존 API와 분리)
# ============================================================
@app.post("/apple-admin/login")
def apple_admin_login(req: AppleIdentityRequest):
    user_id = verify_apple_identity_token(req.identityToken, req.nonce)
    with _db_lock:
        if user_id not in apple_admins:
            return {"registered": False}
        profile = apple_admin_profile(user_id)
    return {"registered": True, "sessionToken": issue_apple_session(user_id), "profile": profile}


@app.post("/apple-admin/register")
def apple_admin_register(req: AppleAdminRegisterRequest):
    user_id = verify_apple_identity_token(req.identityToken, req.nonce)
    label = (req.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label_required")
    validate_and_consume_kyh_code(req.code)
    with _db_lock:
        if user_id not in apple_admins:
            apple_admins[user_id] = {
                "provider": "apple",
                "label": label,
                "registeredAt": now_kst().isoformat(timespec="seconds"),
                "allowedCategory": None,
            }
        else:
            apple_admins[user_id]["label"] = label
        save_apple_admins()
        profile = apple_admin_profile(user_id)
    return {"status": "ok", "sessionToken": issue_apple_session(user_id), "profile": profile}


@app.get("/apple-admin/session")
def apple_admin_session(request: Request):
    _, profile = require_apple_session(request)
    return {"status": "ok", "profile": profile}


@app.get("/apple-admin/upload-categories")
def apple_admin_upload_categories(request: Request):
    _, profile = require_apple_session(request)
    allowed = profile.get("allowedCategory")
    if allowed is None or str(allowed).strip() == "":
        return {"categories": [], "allowedCategory": None, "canAddCategory": False}
    if allowed == "전체":
        return {"categories": ["미지정"] + sorted(categories, key=lambda x: x.lower()), "allowedCategory": "전체", "canAddCategory": True}
    return {"categories": [str(allowed)], "allowedCategory": str(allowed), "canAddCategory": False}


@app.post("/apple-admin/upload-categories")
def apple_admin_add_upload_category(req: CategoryRequest, request: Request):
    _, profile = require_apple_session(request)
    if profile.get("allowedCategory") != "전체":
        raise HTTPException(status_code=403, detail="category_add_not_allowed")
    name = clean_category(req.name)
    if name != "미지정":
        with _db_lock:
            if name not in categories:
                categories.append(name)
                save_categories()
    return {"status": "ok", "category": name}


@app.post("/apple-admin/upload")
def apple_admin_upload(req: AppleAdminUploadRequest, request: Request):
    _, profile = require_apple_session(request)
    validate_phone(req.phoneLast4)
    category = validate_upload_category(profile, req.category)
    # 기존 서버 등록 순서를 그대로 실행합니다.
    register(RegisterRequest(name=req.name, phoneLast4=req.phoneLast4, code=req.code))
    approve(CodeRequest(code=req.code))
    set_delete_pwd(PasswordRequest(password=req.deletePassword, code=req.code))
    set_category_for_code(req.code, category)
    return {"status": "ok", "code": req.code, "category": category}


@app.post("/apple-admin/manage-access")
def apple_admin_manage_access(req: AppleAdminManageAccessRequest, request: Request):
    user_id, _ = require_apple_session(request)
    validate_and_consume_kyh_code(req.code)
    return {"status": "ok", "manageToken": issue_manage_token(user_id)}


@app.get("/apple-admin/admins")
def apple_admin_list(request: Request):
    user_id, _ = require_apple_session(request)
    require_manage_token(request, user_id)
    with _db_lock:
        items = [apple_admin_profile(uid) for uid in apple_admins.keys()]
    items.sort(key=lambda x: x.get("registeredAt") or "", reverse=True)
    return {"items": items}


@app.post("/apple-admin/admins/update")
def apple_admin_update(req: AppleAdminUpdateRequest, request: Request):
    user_id, _ = require_apple_session(request)
    require_manage_token(request, user_id)
    label = (req.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label_required")
    allowed = req.allowedCategory
    if allowed is not None:
        allowed = str(allowed).strip()
        if not allowed:
            allowed = None
    if allowed not in (None, "전체", "미지정") and allowed not in categories:
        raise HTTPException(status_code=400, detail="category_not_found")
    with _db_lock:
        if req.userId not in apple_admins:
            raise HTTPException(status_code=404, detail="apple_admin_not_found")
        apple_admins[req.userId]["label"] = label
        apple_admins[req.userId]["allowedCategory"] = allowed
        save_apple_admins()
        profile = apple_admin_profile(req.userId)
    return {"status": "ok", "profile": profile}


@app.post("/apple-admin/admins/delete")
def apple_admin_delete(req: AppleAdminDeleteRequest, request: Request):
    user_id, _ = require_apple_session(request)
    require_manage_token(request, user_id)
    with _db_lock:
        if req.userId not in apple_admins:
            raise HTTPException(status_code=404, detail="apple_admin_not_found")
        del apple_admins[req.userId]
        save_apple_admins()
    return {"status": "ok", "deletedSelf": req.userId == user_id}


# ============================================================
#   iPhone 관리자용 확장 API
# ============================================================
def require_manager(admin: str):
    if admin != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/manage/access-check")
def manage_access_check(req: CodeRequest):
    if not manager_list_access_allowed(req.code):
        raise HTTPException(status_code=401, detail="access_denied")
    return {"status": "ok"}


@app.get("/manage/list-secure")
def manage_list_secure(access: str):
    if not manager_list_access_allowed(access):
        raise HTTPException(status_code=401, detail="access_denied")
    # 기존 /list와 같은 dict[code] = payload 형식을 유지합니다.
    with _db_lock:
        return {code: dict(data) for code, data in auth_db.items()}


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
#   로그인: 모바일/웹 공통 서버 검증 규칙 사용
# ============================================================
def web_logged_in(request: Request) -> bool:
    code = request.session.get("admin_code")
    return manager_list_access_allowed(code or "")


def require_web_login(request: Request):
    if not web_logged_in(request):
        raise HTTPException(status_code=401, detail="login_required")


@app.post("/admin/api/login")
async def web_login(req: CodeRequest, request: Request):
    code = req.code.strip()
    if not manager_list_access_allowed(code):
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


@app.post("/admin/api/categories/rename")
async def web_rename_category(req: CategoryRenameRequest, request: Request):
    require_web_login(request)
    moved = rename_category_and_reassign(req.oldName, req.newName)
    return {
        "status": "ok",
        "oldCategory": req.oldName.strip(),
        "newCategory": clean_category(req.newName),
        "moved": moved,
    }


@app.post("/admin/api/categories/delete")
async def web_delete_category(req: CategoryRequest, request: Request):
    require_web_login(request)
    moved = delete_category_and_reassign(req.name)
    return {"status": "ok", "deletedCategory": req.name.strip(), "movedToUnspecified": moved}


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


@app.get("/admin/api/apple-admins")
async def web_apple_admins(request: Request):
    require_web_login(request)
    with _db_lock:
        items = [apple_admin_profile(uid) for uid in apple_admins.keys()]
    items.sort(key=lambda x: x.get("registeredAt") or "", reverse=True)
    return {"items": items}


@app.post("/admin/api/apple-admins/update")
async def web_apple_admin_update(req: AppleAdminUpdateRequest, request: Request):
    require_web_login(request)
    label = (req.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label_required")
    allowed = req.allowedCategory
    if allowed is not None:
        allowed = str(allowed).strip() or None
    if allowed not in (None, "전체", "미지정") and allowed not in categories:
        raise HTTPException(status_code=400, detail="category_not_found")
    with _db_lock:
        if req.userId not in apple_admins:
            raise HTTPException(status_code=404, detail="apple_admin_not_found")
        apple_admins[req.userId]["label"] = label
        apple_admins[req.userId]["allowedCategory"] = allowed
        save_apple_admins()
    return {"status": "ok"}


@app.post("/admin/api/apple-admins/delete")
async def web_apple_admin_delete(req: AppleAdminDeleteRequest, request: Request):
    require_web_login(request)
    with _db_lock:
        if req.userId not in apple_admins:
            raise HTTPException(status_code=404, detail="apple_admin_not_found")
        del apple_admins[req.userId]
        save_apple_admins()
    return {"status": "ok"}


@app.get("/admin/api/export-json")
async def web_export_json(request: Request):
    # 기존 직접 호출 호환을 위해 경로는 유지하지만 PC UI에서는 ZIP 백업을 사용합니다.
    require_web_login(request)
    payload = {
        "exportedAt": now_kst().isoformat(timespec="seconds"),
        "auth_db": auth_db,
        "categories": categories,
        "apple_admins": apple_admins,
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=PoketAuth_Backup.json"},
    )


@app.get("/admin/api/backup-zip")
async def web_backup_zip(request: Request):
    require_web_login(request)
    raw = build_full_backup_zip()
    stamp = now_kst().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        io.BytesIO(raw),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="CodeNoteAuth_Backup_{stamp}.zip"'},
    )


@app.post("/admin/api/restore-backup")
async def web_restore_backup(request: Request):
    require_web_login(request)
    raw = await request.body()
    filename = request.headers.get("X-Backup-Filename", "")
    content_type = request.headers.get("Content-Type", "")
    records, category_count, backup_type = restore_backup_auto(raw, filename, content_type)
    return {"status": "ok", "records": records, "categories": category_count, "type": backup_type}


# 이전 ZIP 전용 경로도 호환을 위해 유지합니다.
@app.post("/admin/api/restore-zip")
async def web_restore_zip(request: Request):
    require_web_login(request)
    raw = await request.body()
    records, category_count = restore_full_backup_zip(raw)
    return {"status": "ok", "records": records, "categories": category_count, "type": "zip"}


ADMIN_HTML = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>코드노트 인증키</title>
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
<div class="row" style="justify-content:space-between;align-items:center"><h1>코드노트 인증키</h1><div class="row"><button onclick="openBackupModal()">백업 / 복원</button><input id="restoreBackup" type="file" class="hidden" onchange="restoreBackupFile(this)"><button onclick="logout()">로그아웃</button></div></div>
<div class="card"><h2>인증키 등록</h2><div class="row"><input id="rName" class="grow" placeholder="성함"><input id="rPhone" class="grow" inputmode="numeric" maxlength="4" placeholder="전화번호 끝 4자리"></div><div class="row" style="margin-top:10px"><select id="rCategory" class="grow"></select><button onclick="addCategory()">+ 카테고리 추가</button></div><div class="row" style="margin-top:10px"><input id="rCode" class="grow" placeholder="인증키"><input id="rPwd" class="grow" placeholder="삭제 비밀번호"></div><div class="row" style="margin-top:10px"><button class="primary" onclick="registerCode()">서버 업로드</button><button onclick="clearRegister()">입력값 지우기</button></div></div>
<div class="card"><div class="row" style="justify-content:space-between;align-items:center"><h2>인증키 목록</h2><div class="row"><button onclick="openCategoryManager()">카테고리 관리</button><button onclick="openAppleAdminManager()">인증 등록 내역</button></div></div><div id="tabs" class="tabs"></div><input id="search" style="width:100%;margin:8px 0 12px" placeholder="🔍 이름 / 전화번호 / 인증키 검색" oninput="renderList()"><div id="list" class="list"></div></div>
</div></div>
<div id="modal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between"><h2>인증키 상세</h2><button onclick="closeModal()">닫기</button></div><div id="detail"></div><div class="actions" id="detailActions"><button onclick="changeCategory()">카테고리</button><button onclick="activateSelected()">활성화</button><button onclick="deactivateSelected()">비활성화</button><button onclick="editSelected()">수정</button><button class="danger" onclick="deleteSelected()">삭제</button></div></div></div>
<div id="editAuthModal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between;align-items:center"><h2>인증키 수정</h2><button onclick="closeEditAuthModal()">닫기</button></div><label>성함</label><input id="eName" style="width:100%"><label>전화번호 끝 4자리</label><input id="ePhone" style="width:100%" inputmode="numeric" maxlength="4"><label>인증키</label><input id="eCode" style="width:100%"><label>삭제 비밀번호</label><input id="ePwd" style="width:100%"><label>카테고리</label><select id="eCategory" style="width:100%"></select><div class="row" style="margin-top:16px"><button class="primary grow" onclick="saveEditSelected()">저장</button><button class="grow" onclick="closeEditAuthModal()">취소</button></div></div></div>
<div id="categoryModal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between;align-items:center"><h2>카테고리 관리</h2><button onclick="closeCategoryManager()">닫기</button></div><div id="categoryManageList" class="list"></div><p class="muted" style="margin:14px 0 0">카테고리를 삭제하면 인증키는 삭제되지 않고 미지정으로 이동합니다.</p></div></div>
<div id="backupModal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between;align-items:center"><h2>백업 / 복원</h2><button onclick="closeBackupModal()">닫기</button></div><div class="row"><button class="primary grow" onclick="location.href='/admin/api/backup-zip'">ZIP 백업</button><button class="grow" onclick="document.getElementById('restoreBackup').click()">ZIP 복원</button></div></div></div>
<div id="appleAdminModal" class="modal hidden"><div class="modalbox"><div class="row" style="justify-content:space-between;align-items:center"><h2>인증 등록 내역</h2><button onclick="closeAppleAdminManager()">닫기</button></div><div id="appleAdminList" class="list"></div></div></div>
<script>
let items=[], categories=['미지정'], selectedCategory='전체', selected=null;
async function api(path,opt={}){let r=await fetch(path,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let text=await r.text();let data={};try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));return data}
async function boot(){try{let s=await api('/admin/api/session');if(s.loggedIn){showApp();await refresh()}}catch(e){}}
function showApp(){loginCard.classList.add('hidden');app.classList.remove('hidden')}
async function login(){try{await api('/admin/api/login',{method:'POST',body:JSON.stringify({code:loginCode.value.trim()})});loginMsg.textContent='';showApp();await refresh()}catch(e){loginMsg.textContent='로그인 실패';}}
async function logout(){await api('/admin/api/logout',{method:'POST'});location.reload()}
async function refresh(){let [l,c]=await Promise.all([api('/admin/api/list'),api('/admin/api/categories')]);items=l.items||[];categories=c.categories||['미지정'];fillCategories();renderTabs();renderList()}
function fillCategories(){rCategory.innerHTML=categories.map(c=>`<option>${esc(c)}</option>`).join('')}
function renderTabs(){let names=['전체','미지정',...categories.filter(c=>c!=='미지정')];tabs.innerHTML=names.map(c=>`<button class="tab ${selectedCategory===c?'active':''}" onclick="selectCat('${js(c)}')">${esc(c)}</button>`).join('')+`<button class="tab" onclick="addCategory()">＋</button>`}
function selectCat(c){selectedCategory=c;renderTabs();renderList()}
function filtered(){let q=search.value.trim().toLowerCase();return items.filter(x=>{if(x.deletedAt)return false;if(selectedCategory!=='전체'&&(x.category||'미지정')!==selectedCategory)return false;return !q||[x.code,x.name,x.phone].some(v=>(v||'').toLowerCase().includes(q))})}
function renderList(){let a=filtered();list.innerHTML=a.length?a.map(x=>`<div class="item" onclick="openItem('${js(x.code)}')"><div class="itemtop"><b>${esc(x.name||'')}</b><span class="badge ${(x.deletedAt?'deleted':(!x.enabled?'inactive':''))}">${x.deletedAt?'삭제됨':(x.enabled?'활성':'비활성')}</span></div><div class="muted">${esc(x.phone||'')} · ${esc(x.category||'미지정')} · ${esc(x.date||'')}</div><div class="code">${esc(x.code)}</div></div>`).join(''):'<p class="muted">표시할 인증키가 없습니다.</p>'}
function openItem(code){selected=items.find(x=>x.code===code);if(!selected)return;detail.innerHTML=`<label>성함</label><div>${esc(selected.name||'')}</div><label>전화번호</label><div>${esc(selected.phone||'')}</div><label>인증키</label><div class="code">${esc(selected.code)}</div><label>삭제 비밀번호</label><div>${esc(selected.delete_password||'')}</div><label>카테고리</label><div>${esc(selected.category||'미지정')}</div><label>등록일</label><div>${esc(selected.date||'')}</div><label>상태</label><div>${selected.deletedAt?'삭제됨':(selected.enabled?'활성':'비활성')}</div>`;modal.classList.remove('hidden')}
function closeModal(){modal.classList.add('hidden');selected=null}
async function addCategory(){let n=prompt('추가할 카테고리 이름');if(!n||!n.trim())return;await api('/admin/api/categories',{method:'POST',body:JSON.stringify({name:n.trim()})});await refresh();rCategory.value=n.trim()}
function openCategoryManager(){renderCategoryManager();categoryModal.classList.remove('hidden')}
function closeCategoryManager(){categoryModal.classList.add('hidden')}
function categoryCount(name){return items.filter(x=>(x.category||'미지정')===name).length}
function renderCategoryManager(){let names=['미지정',...categories.filter(c=>c!=='미지정')];categoryManageList.innerHTML=names.map(n=>{let isDefault=n==='미지정';return `<div class="item" style="cursor:default"><div class="row" style="justify-content:space-between;align-items:center"><div><b>${esc(n)}</b><div class="muted">인증키 ${categoryCount(n)}개</div></div><div class="row">${isDefault?'<span class="muted">기본 카테고리</span>':`<button onclick="renameCategory('${js(n)}')">수정</button><button class="danger" onclick="deleteCategory('${js(n)}')">삭제</button>`}</div></div></div>`}).join('')}
async function renameCategory(oldName){let n=prompt('새 카테고리 이름',oldName);if(n===null)return;n=n.trim();if(!n||n===oldName)return;try{let r=await api('/admin/api/categories/rename',{method:'POST',body:JSON.stringify({oldName:oldName,newName:n})});if(selectedCategory===oldName)selectedCategory=n;await refresh();renderCategoryManager();alert('카테고리 수정 완료\n인증키 '+(r.moved||0)+'개가 '+n+' 카테고리로 이동했습니다.')}catch(e){alert('카테고리 수정 실패: '+e.message)}}
async function deleteCategory(n){if(!n||n==='미지정')return;if(!confirm('카테고리 '+n+' 을(를) 삭제할까요?\n안에 있는 인증키는 삭제되지 않고 미지정으로 이동합니다.'))return;try{let r=await api('/admin/api/categories/delete',{method:'POST',body:JSON.stringify({name:n})});if(selectedCategory===n)selectedCategory='전체';await refresh();renderCategoryManager();alert('카테고리 삭제 완료\n인증키 '+(r.movedToUnspecified||0)+'개가 미지정으로 이동했습니다.')}catch(e){alert('카테고리 삭제 실패: '+e.message)}}
async function restoreBackupFile(input){let f=input.files&&input.files[0];if(!f)return;try{if(!confirm('선택한 백업 ZIP 내부 JSON 기준으로 전체 서버 내용을 복원할까요?\n현재 서버 내용은 백업 내용으로 교체됩니다.')){input.value='';return}let raw=await f.arrayBuffer();let r=await fetch('/admin/api/restore-backup',{method:'POST',headers:{'Content-Type':f.type||'application/octet-stream','X-Backup-Filename':f.name},body:raw});let text=await r.text();let data={};try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));alert((data.type==='json'?'JSON':'ZIP')+' 복원 완료\n인증키 '+(data.records||0)+'개 / 카테고리 '+(data.categories||0)+'개');await refresh()}catch(e){alert('백업 복원 실패: '+e.message)}finally{input.value=''}}
async function changeCategory(){if(!selected)return;let n=prompt('변경할 카테고리\n현재: '+(selected.category||'미지정')+'\n\n기존 카테고리: '+categories.join(', '),selected.category||'미지정');if(n===null)return;n=n.trim()||'미지정';if(n!=='미지정'&&!categories.includes(n))await api('/admin/api/categories',{method:'POST',body:JSON.stringify({name:n})});await api('/admin/api/category',{method:'POST',body:JSON.stringify({code:selected.code,category:n})});closeModal();await refresh()}
async function activateSelected(){if(!selected)return;await api('/admin/api/activate',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
async function deactivateSelected(){if(!selected)return;await api('/admin/api/deactivate',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
function editSelected(){if(!selected)return;eName.value=selected.name||'';ePhone.value=selected.phone||'';eCode.value=selected.code||'';ePwd.value=selected.delete_password||'';eCategory.innerHTML=categories.map(c=>`<option>${esc(c)}</option>`).join('');eCategory.value=selected.category||'미지정';editAuthModal.classList.remove('hidden')}
function closeEditAuthModal(){editAuthModal.classList.add('hidden')}
async function saveEditSelected(){if(!selected)return;let name=eName.value.trim(),phone=ePhone.value.trim(),code=eCode.value.trim(),pwd=ePwd.value,cat=eCategory.value||'미지정';if(!name||!/^[0-9]{4}$/.test(phone)||!code||!pwd){alert('성함 / 전화번호 4자리 / 인증키 / 비밀번호를 모두 입력하세요.');return}try{await api('/admin/api/update',{method:'POST',body:JSON.stringify({originalCode:selected.code,name:name,phoneLast4:phone,code:code,deletePassword:pwd,category:cat})});closeEditAuthModal();closeModal();await refresh();alert('수정 완료')}catch(e){alert('수정 실패: '+e.message)}}
async function deleteSelected(){if(!selected||!confirm('이 인증키를 서버에서 완전히 삭제할까요?\n삭제 후에는 복구할 수 없습니다.'))return;await api('/admin/api/delete',{method:'POST',body:JSON.stringify({code:selected.code})});closeModal();await refresh()}
async function registerCode(){let o={name:rName.value.trim(),phoneLast4:rPhone.value.trim(),category:rCategory.value||'미지정',code:rCode.value.trim(),deletePassword:rPwd.value};if(!o.name||!/^[0-9]{4}$/.test(o.phoneLast4)||!o.code||!o.deletePassword){alert('성함 / 전화번호 4자리 / 인증키 / 비밀번호를 모두 입력하세요.');return}try{await api('/admin/api/register',{method:'POST',body:JSON.stringify(o)});clearRegister();await refresh();alert('업로드 완료')}catch(e){alert('업로드 실패: '+e.message)}}
function clearRegister(){rName.value='';rPhone.value='';rCode.value='';rPwd.value='';rCategory.value='미지정'}
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function js(v){return String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}
function openBackupModal(){backupModal.classList.remove('hidden')}
function closeBackupModal(){backupModal.classList.add('hidden')}
async function openAppleAdminManager(){try{let r=await api('/admin/api/apple-admins');renderAppleAdminManager(r.items||[]);appleAdminModal.classList.remove('hidden')}catch(e){alert('인증 등록 내역 불러오기 실패: '+e.message)}}
function closeAppleAdminManager(){appleAdminModal.classList.add('hidden')}
function renderAppleAdminManager(a){appleAdminList.innerHTML=a.length?a.map(x=>`<div class="item" style="cursor:default"><div class="itemtop"><b>${esc(x.label||'')}</b><span class="badge">${esc(x.allowedCategory||'권한 미설정')}</span></div><div class="muted">${esc(x.registeredAt||'')}</div><div class="code">${esc(x.userId||'')}</div><div class="row" style="margin-top:10px"><button onclick="editAppleAdmin('${js(x.userId)}','${js(x.label||'')}','${js(x.allowedCategory||'') }')">수정</button><button class="danger" onclick="deleteAppleAdmin('${js(x.userId)}')">삭제</button></div></div>`).join(''):'<p class="muted">등록된 Apple 관리자가 없습니다.</p>'}
async function editAppleAdmin(userId,label,currentCategory){let newLabel=prompt('등록 문구',label);if(newLabel===null)return;newLabel=newLabel.trim();if(!newLabel)return alert('등록 문구를 입력하세요.');let guide='허용 카테고리 입력\n전체 또는 카테고리 이름\n비워두면 권한 미설정\n\n현재 카테고리: '+categories.join(', ');let allowed=prompt(guide,currentCategory||'');if(allowed===null)return;allowed=allowed.trim();let payload={userId:userId,label:newLabel,allowedCategory:allowed||null};try{await api('/admin/api/apple-admins/update',{method:'POST',body:JSON.stringify(payload)});await openAppleAdminManager();alert('수정 완료')}catch(e){alert('수정 실패: '+e.message)}}
async function deleteAppleAdmin(userId){if(!confirm('이 관리자 등록을 삭제할까요?\n해당 계정은 다음 요청부터 앱을 사용할 수 없습니다.'))return;try{await api('/admin/api/apple-admins/delete',{method:'POST',body:JSON.stringify({userId:userId})});await openAppleAdminManager();alert('삭제 완료')}catch(e){alert('삭제 실패: '+e.message)}}
boot();
</script></body></html>'''


@app.get("/admin", response_class=HTMLResponse)
def web_admin_page():
    return HTMLResponse(ADMIN_HTML)
