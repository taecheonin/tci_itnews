# IT News YouTube Collector (PyQt + MySQL)

개인용 IT 유튜브 수집/시청 관리 데스크톱 앱입니다.

## 핵심 기능

- `it` 키워드부터 시작해서 유튜브 영상을 최신순으로 수집
- 태그/채널 정보 저장, 태그를 키워드로 재수집(중복 제거)
- 태그가 없으면 AI(또는 로컬 fallback)로 핵심 키워드 추출
- 창이 열릴 때 자동 수집 + 채널 라이브 체크
- 라이브 감지 시 메인 화면에 표시하고 바로 재생(음소거 파라미터 적용)
- 영상 시청 상태 3단계 관리
  - 안봄(`UNWATCHED`)
  - 시청(`WATCHING`)
  - 다봄(`WATCHED`)
- 태그 랭킹 표시 및 클릭 검색
- 검색 자동완성
- 키워드 관리(추가/삭제)

## 요구 환경

- Python 3.10+
- MySQL 8+
- YouTube Data API Key
- (선택) GitHub PAT (`GITHUB_TOKEN`) 또는 OpenAI API Key — 태그가 없을 때 AI 키워드 추출

## 설치

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## DB 생성

```sql
CREATE DATABASE itnews CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

`config.example.toml`을 `config.toml`로 복사 후 환경에 맞게 수정하세요.

### AI 설정 (요청 반영)

- 기본값은 GitHub Models 방식입니다.
  - `provider = "github_models"`
  - `base_url = "https://models.github.ai/inference"`
  - `model = "openai/o4-mini"`
- 토큰은 아래 2가지 중 하나로 사용합니다.
  1. `config.toml`의 `ai.api_key`에 직접 입력
  2. 환경변수 `GITHUB_TOKEN` 설정 (권장)

```bash
export GITHUB_TOKEN=YOUR_GITHUB_PAT
```

## 실행

```bash
python app.py
```

## 참고

- 라이브 재생 임베드는 `QWebEngineView`를 사용합니다.
- 검색 자동완성은 저장된 키워드/태그 기반으로 동작합니다.
