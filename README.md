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
- (선택) OpenAI API Key — 태그가 없을 때 키워드 추출 품질 향상

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

## 실행

```bash
python app.py
```

## 참고

- 라이브 재생 임베드는 `QWebEngineView`를 사용합니다.
- 검색 자동완성은 저장된 키워드/태그 기반으로 동작합니다.
