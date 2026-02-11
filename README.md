# IT News Collector

YouTube IT 영상을 수집·검색·시청 상태로 관리하는 **Flask 웹 앱**입니다.

## 주요 기능

- **키워드/태그 기반 수집**: DB 키워드로 YouTube 최신 영상 검색 후 저장 (영상·태그, 태그 없으면 AI 키워드 추출)
- **채널 수집**: YouTube 채널 ID를 수동으로 등록해 최신 영상을 자동 수집
- **영상 목록**: 제목/태그 검색, 상태(전체·안봄·시청·다봄) 필터, 페이지네이션
- **키워드 관리**: 키워드 추가·삭제. 키워드 클릭 시 해당 키워드로 검색. 삭제 시 해당 검색 결과 영상 전부 숨김 처리
- **시청 상태**: 안봄 / 시청 / 다봄. 리스트 행 클릭 시 모달에서 재생, 상태는 시청으로 저장
- **숨기기**: 모달에서 숨기기 시 리스트에서 제외. 숨긴 영상은 `hidden_videos`로 관리
- **Google Authenticator (TOTP) 로그인**: `.env`에 `AUTH_TOTP_SECRET` 설정 시 로그인 필수
- **크론 수집**: `tci_cron_collect.py`로 주기 수집 (오늘 이전 갱신 키워드 1개씩 수집)

## 요구 환경

- Python 3.10+
- MySQL 8+
- YouTube Data API v3 키
- (선택) AI 키워드 추출: GitHub PAT 또는 OpenAI API 키

## 설치

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## DB 생성

```sql
CREATE DATABASE tci_itnews CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

앱 실행 시 `tci_core`가 필요한 테이블(`keywords`, `videos`, `video_tags`, `hidden_videos`, `channels`)을 자동 생성합니다.

## 설정

프로젝트 루트에 `.env` 파일을 두고 환경 변수를 설정합니다. `.env.example`을 복사해 사용하세요.

```bash
cp .env.example .env
# .env 편집
```

| 변수 | 설명 |
|------|------|
| `YOUTUBE_API_KEY` | YouTube Data API v3 키 |
| `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` | MySQL 접속 정보 |
| `AUTH_TOTP_SECRET` | TOTP 시크릿 (비우면 로그인 비활성). `python -c "import pyotp; print(pyotp.random_base32())"` 로 생성 |
| `AUTH_APP_NAME` | Authenticator 앱에 표시할 이름 |
| `AI_ENABLED`, `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, `AI_BASE_URL` | AI 키워드 추출 (선택) |
| `SECRET_KEY` | Flask 세션용 (선택, 기본값 있음) |

- **YouTube**: [Google Cloud Console](https://console.cloud.google.com/)에서 YouTube Data API v3 사용 설정 후 API 키 발급
- **로그인**: `AUTH_TOTP_SECRET`을 설정한 뒤 `/auth/setup`에서 QR 코드를 스캔해 Google Authenticator에 등록

## 실행

```bash
python app.py
```

브라우저에서 `http://127.0.0.1:5002/` 로 접속합니다.

## 채널 수동 추가

- 메인 화면 우측 `채널 관리` 섹션에서 **YouTube 채널 ID(예: `UC_x5XG1OV2P6uZZ5FSM9Ttw`)**를 입력하고 `채널 추가` 버튼을 누르면 등록됩니다.
- 등록 시 YouTube Data API로 채널 메타 정보를 조회하며, 존재하지 않는 ID는 추가되지 않습니다.
- 리스트의 `삭제` 버튼을 누르면 해당 채널이 제거되고 이후 수집 대상에서 제외됩니다.
- 등록된 채널은 키워드와 함께 자동 수집 대상에 포함되어 최신 영상이 주기적으로 저장됩니다.

## 수집 동작 정책

- 수집 사이클마다 **updated_date가 가장 오래된 키워드 1개와 채널 1개**를 선택해 수집합니다.
- YouTube Search/Channel API에 페이지가 여러 개 존재하면 끝까지 순회하며, 각 페이지 요청 사이에 약 **90초 대기**해 할당량을 분산합니다.
- 키워드와 채널이 모두 오늘 이미 수집된 상태라면 수집은 건너뛰고 로그만 남습니다.

## 크론 수집 (선택)

수집만 주기적으로 실행하려면 `tci_cron_collect.py`를 크론으로 등록합니다.

```bash
mkdir -p logs
crontab -e
```

예: 6시간마다 수집

```
0 */6 * * * cd /path/to/tci_itnews && /path/to/python tci_cron_collect.py >> logs/cron.log 2>&1
```

더 많은 예시는 `cron.example.txt`를 참고하세요.

## 프로젝트 구조

| 파일/폴더 | 설명 |
|-----------|------|
| `app.py` | Flask 앱. 라우트(메인·API)·전역 서비스·백그라운드 수집 |
| `tci_core.py` | 공통 백엔드: Config, Database, YouTubeService, KeywordAI, 수집/NEW 분류 |
| `tci_cron_collect.py` | 크론용 수집 스크립트 (키워드 1개 수집 + NEW 영상 분류) |
| `templates/index.html` | 메인 화면 (검색·목록·페이지·모달·키워드/채널 관리) |
| `templates/login.html` | TOTP 로그인 페이지 |
| `templates/setup.html` | Google Authenticator QR 코드 설정 페이지 |
| `.env` | 환경 변수 설정 (Git 제외, `.env.example` 참고) |
| `cron.example.txt` | 크론 등록 예시 |

## 참고

- 수집/검색은 YouTube Data API v3 할당량에 따라 제한될 수 있습니다.
- TOTP 로그인 사용 시 `.env`에 `AUTH_TOTP_SECRET`을 설정하세요.
