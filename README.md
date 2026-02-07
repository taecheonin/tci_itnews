# IT News YouTube Collector (PyQt + MySQL)

개인 1인 사용을 위한 **IT 유튜브 수집/시청 관리 데스크톱 앱**입니다.

## 주요 동작 요약

앱을 실행하면(창이 열리면) 아래가 자동 수행됩니다.

1. `it` 시드 키워드 보장
2. 키워드 최신순 수집(`updated_date` 기준)
3. YouTube 검색 결과(최신순) 저장
4. 영상 태그 저장 + 태그를 키워드로 승격(중복 제거)
5. 태그가 없으면 AI 키워드 추출 후 키워드 저장
6. 채널 라이브 여부 체크
7. 라이브가 있으면 메인에 표시 + 음소거 자동 재생
8. 라이브가 없으면 메인에서 제거

## 기능 목록

- `it` 키워드부터 시작해서 유튜브 영상을 최신순 수집
- 태그/채널 정보 저장, 태그 기반 키워드 확장 수집(중복 제거)
- 태그 미존재 시 AI(또는 로컬 fallback) 키워드 추출
- 시청 상태 3단계 분류
  - 안봄(`UNWATCHED`)
  - 시청(`WATCHING`) - 중간까지 시청
  - 다봄(`WATCHED`) - 처음~끝 시청 완료
- 태그 랭킹 표시 + 클릭 즉시 검색
- 검색 자동완성(저장 키워드/태그 기반)
- 키워드 추가/삭제 관리

---

## 요구 환경

- Python 3.10+
- MySQL 8+
- YouTube Data API Key
- (선택) GitHub PAT(`GITHUB_TOKEN`) 또는 OpenAI API Key

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

## 설정 파일 만들기

```bash
cp config.example.toml config.toml
```

`config.toml`에서 최소 아래 값은 필수로 채우세요.

- `youtube.api_key`
- `mysql.host / port / user / password / database`

## AI 설정

기본값은 **GitHub Models + OpenAI SDK** 방식입니다.

- `provider = "github_models"`
- `base_url = "https://models.github.ai/inference"`
- `model = "openai/o4-mini"`

토큰 사용 우선순위:

1. `config.toml`의 `ai.api_key`
2. 환경변수 `GITHUB_TOKEN`

예시:

```bash
export GITHUB_TOKEN=YOUR_GITHUB_PAT
```

OpenAI 직접 호출을 쓰고 싶다면:

- `provider = "openai"`
- `ai.api_key`에 OpenAI API Key 설정

## 실행

```bash
python app.py
```

## UI 사용법

- 상단 검색창: 자동완성으로 키워드/태그 검색
- 상태 필터: 안봄/시청/다봄 필터링
- 랭킹 클릭: 해당 태그로 즉시 검색
- 테이블 행 선택: 자동으로 `시청(WATCHING)` 처리
- 하단 버튼: `안봄/시청/다봄` 수동 변경
- 우측 키워드 관리: 수집 키워드 추가/삭제

## 참고 사항

- 라이브 재생 임베드는 `QWebEngineView`가 있을 때 표시됩니다.
- API 할당량/네트워크 상태에 따라 수집 속도 및 결과가 달라질 수 있습니다.
- 본 앱은 로그인 기능 없이 단일 사용자 환경을 전제로 합니다.
