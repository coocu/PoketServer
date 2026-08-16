Poket Server 최소수정 배포 안내

[중요 - 기존 인증키 보존]
현재 서버는 인증키를 auth_data.json 로컬 파일에 저장합니다.
Render에서 Persistent Disk가 없는 서비스는 재배포/재시작 시 로컬 파일이 보존되지 않을 수 있으므로,
코드 배포 전에 반드시 현재 운영 데이터를 먼저 백업하세요.

1) 배포 전에 현재 운영 인증키 백업
   python3 backup_live_data.py
   -> auth_data_backup_날짜.json 생성

2) Render에 Persistent Disk를 사용한다면 디스크 경로 아래에 데이터 파일을 두세요.
   예시 환경변수:
   AUTH_DATA_FILE=/var/data/auth_data.json
   AUTH_CATEGORY_FILE=/var/data/auth_categories.json
   (실제 디스크 Mount Path에 맞게 지정)

3) 새 배포 후 데이터가 비어 있다면 백업 병합 복구
   python3 restore_backup.py auth_data_backup_날짜.json
   /manage/import-backup API는 기존 ADMIN_PASSWORD로 보호되며 기본 동작은 병합입니다.

[기존 API 호환]
아래 기존 API 경로는 유지됩니다.
/register -> /approve -> /set_delete_pwd
/app/check
/app/delete_password
/list /delete /delete_by_user /trash /restore /tokens /tokens/export

[새 PC 관리자]
https://poketserver.onrender.com/admin
서버에 실제 등록되어 있고 문자열에 kyh가 포함되며 approved + 활성 상태인 인증키만 로그인됩니다.
로그인 자체는 /app/check를 호출하지 않으므로 기존 1회 사용 로직을 실행하지 않습니다.

[새 관리 데이터]
기존 auth_data.json 레코드는 삭제하거나 초기화하지 않습니다.
category가 없는 기존 인증키는 '미지정'으로 취급합니다.
enabled가 없는 기존 인증키는 true로 취급합니다.
새 카테고리 목록은 auth_categories.json에 저장합니다.
server.py는 저장 전에 기존 데이터 파일의 .bak 백업도 남깁니다.

[환경변수]
ADMIN_PASSWORD: 지정하지 않으면 기존 값 Kim86110!@ 유지
SESSION_SECRET: 웹 로그인 세션용. Render 환경변수에 긴 임의 문자열 권장
AUTH_DATA_FILE: 인증키 JSON 경로. 미지정 시 기존 auth_data.json
AUTH_CATEGORY_FILE: 카테고리 JSON 경로. 미지정 시 auth_categories.json
