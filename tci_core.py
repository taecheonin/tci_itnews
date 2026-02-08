# tci_core: IT News Collector 공통 백엔드 (Flask/PyQt 공용)
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable

_tci_log = logging.getLogger(__name__)

import mysql.connector
import requests
from dotenv import load_dotenv
from openai import OpenAI


@dataclass
class Config:
    youtube_api_key: str
    mysql: dict
    ai_enabled: bool
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    auth_totp_secret: str
    auth_app_name: str


class Database:
    def __init__(self, cfg: Config):
        self.conn = mysql.connector.connect(**cfg.mysql)
        self.conn.autocommit = True
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS keywords (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                keyword VARCHAR(255) NOT NULL UNIQUE,
                source ENUM('seed','tag','ai') NOT NULL DEFAULT 'seed',
                created_date DATE NOT NULL,
                updated_date DATE NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                video_id VARCHAR(64) NOT NULL UNIQUE,
                channel_id VARCHAR(64) NOT NULL,
                title VARCHAR(512) NOT NULL,
                description TEXT,
                published_at DATETIME NOT NULL,
                status ENUM('NEW','UNWATCHED','WATCHING','WATCHED') NOT NULL DEFAULT 'NEW',
                last_seen_at DATETIME NOT NULL,
                watch_updated_at DATETIME NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        try:
            cur.execute(
                "ALTER TABLE videos MODIFY COLUMN status ENUM('NEW','UNWATCHED','WATCHING','WATCHED') NOT NULL DEFAULT 'NEW'"
            )
        except Exception:
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS video_tags (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                video_id VARCHAR(64) NOT NULL,
                tag VARCHAR(255) NOT NULL,
                collected_date DATE NOT NULL,
                UNIQUE KEY uniq_video_tag (video_id, tag),
                INDEX idx_tag (tag)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS hidden_videos (
                video_id VARCHAR(64) PRIMARY KEY
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.close()

    def hide_video(self, video_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute("INSERT IGNORE INTO hidden_videos (video_id) VALUES (%s)", (video_id,))
        cur.close()

    def hide_videos_by_tag(self, tag: str) -> None:
        """해당 태그가 붙은 모든 영상을 숨김 처리."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO hidden_videos (video_id) SELECT DISTINCT video_id FROM video_tags WHERE tag = %s",
            (tag.strip().lower(),),
        )
        cur.close()

    def hide_videos_by_keyword(self, keyword: str) -> None:
        """키워드 검색 조건(제목 또는 태그 포함)에 맞는 모든 영상을 숨김 처리. 리스트 검색 결과와 동일 기준."""
        cur = self.conn.cursor()
        kw = keyword.strip().lower()
        cur.execute(
            """
            INSERT IGNORE INTO hidden_videos (video_id)
            SELECT DISTINCT v.video_id FROM videos v
            WHERE v.title LIKE CONCAT('%%', %s, '%%')
               OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%'))
            """,
            (kw, kw),
        )
        cur.close()

    def ensure_seed(self, keyword: str) -> None:
        today = dt.date.today()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO keywords (keyword, source, created_date, updated_date)
            VALUES (%s, 'seed', %s, %s)
            ON DUPLICATE KEY UPDATE updated_date = VALUES(updated_date)
            """,
            (keyword.lower(), today, today),
        )
        cur.close()

    def active_keywords(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT keyword FROM keywords ORDER BY updated_date DESC, id DESC")
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows

    def get_one_keyword_due_for_collect(self) -> str | None:
        """updated_date가 오늘 이전인 키워드 하나만 반환 (가장 오래된 것부터). 크론 한 건 수집용."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT keyword FROM keywords WHERE DATE(updated_date) < CURDATE() ORDER BY updated_date ASC, id ASC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None

    def set_keyword_updated_today(self, keyword: str) -> None:
        """해당 키워드의 updated_date를 오늘 날짜로 변경."""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE keywords SET updated_date = CURDATE() WHERE keyword = %s",
            (keyword.lower(),),
        )
        cur.close()

    def upsert_video(self, payload: dict) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO videos (video_id, channel_id, title, description, published_at, last_seen_at, watch_updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                title=VALUES(title),
                description=VALUES(description),
                published_at=VALUES(published_at),
                last_seen_at=NOW()
            """,
            (
                payload["video_id"],
                payload["channel_id"],
                payload["title"],
                payload.get("description", ""),
                payload["published_at"],
            ),
        )
        cur.close()

    def save_tags(self, video_id: str, tags: Iterable[str]) -> None:
        today = dt.date.today()
        cur = self.conn.cursor()
        for t in {t.strip().lower() for t in tags if t and len(t.strip()) >= 2}:
            cur.execute(
                """
                INSERT INTO video_tags (video_id, tag, collected_date)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE collected_date = VALUES(collected_date)
                """,
                (video_id, t, today),
            )
            cur.execute(
                """
                INSERT INTO keywords (keyword, source, created_date, updated_date)
                VALUES (%s, 'tag', %s, %s)
                ON DUPLICATE KEY UPDATE updated_date = VALUES(updated_date)
                """,
                (t, today, today),
            )
        cur.close()

    def save_ai_keywords(self, keywords: Iterable[str]) -> None:
        today = dt.date.today()
        cur = self.conn.cursor()
        for kw in {k.strip().lower() for k in keywords if k and len(k.strip()) >= 2}:
            cur.execute(
                """
                INSERT INTO keywords (keyword, source, created_date, updated_date)
                VALUES (%s, 'ai', %s, %s)
                ON DUPLICATE KEY UPDATE updated_date = VALUES(updated_date)
                """,
                (kw, today, today),
            )
        cur.close()

    def list_videos(
        self, query: str = "", status: str = "", limit: int | None = None, offset: int | None = None
    ) -> list[tuple]:
        cur = self.conn.cursor()
        sql = """
            SELECT v.video_id, v.title, v.channel_id, v.published_at, v.status
            FROM videos v
            WHERE (%s = '' OR v.title LIKE CONCAT('%%', %s, '%%')
               OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%')))
              AND (%s = '' OR v.status = %s)
              AND NOT EXISTS (SELECT 1 FROM hidden_videos h WHERE h.video_id = v.video_id)
            ORDER BY v.published_at DESC, FIELD(v.status, 'NEW','UNWATCHED','WATCHING','WATCHED')
        """
        if limit is not None and offset is not None:
            sql += " LIMIT %s OFFSET %s"
            cur.execute(sql, (query, query, query, status, status, limit, offset))
        else:
            cur.execute(sql, (query, query, query, status, status))
        rows = cur.fetchall()
        cur.close()
        return rows

    def list_videos_count(self, query: str = "", status: str = "") -> int:
        cur = self.conn.cursor()
        sql = """
            SELECT COUNT(*)
            FROM videos v
            WHERE (%s = '' OR v.title LIKE CONCAT('%%', %s, '%%')
               OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%')))
              AND (%s = '' OR v.status = %s)
              AND NOT EXISTS (SELECT 1 FROM hidden_videos h WHERE h.video_id = v.video_id)
        """
        cur.execute(sql, (query, query, query, status, status))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 0

    def set_watch_status(self, video_id: str, status: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE videos SET status=%s, watch_updated_at=NOW() WHERE video_id=%s",
            (status, video_id),
        )
        cur.close()

    def get_new_videos(self) -> list[tuple[str, str, str]]:
        """status가 NEW인 영상 목록 (video_id, title, description)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT video_id, title, COALESCE(description, '') FROM videos WHERE status = 'NEW' ORDER BY id ASC"
        )
        rows = cur.fetchall()
        cur.close()
        return rows

    def all_suggest_words(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT keyword FROM keywords")
        kws = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT tag FROM video_tags")
        tags = [r[0] for r in cur.fetchall()]
        cur.close()
        return sorted(set(kws + tags))

    def delete_keyword(self, keyword: str) -> None:
        kw = keyword.strip().lower()
        self.hide_videos_by_keyword(kw)
        cur = self.conn.cursor()
        cur.execute("DELETE FROM keywords WHERE keyword=%s", (kw,))
        cur.close()


class YouTubeService:
    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_latest(self, keyword: str, max_results: int = 15) -> list[dict]:
        try:
            r = requests.get(
                f"{self.BASE}/search",
                params={
                    "part": "snippet",
                    "q": keyword,
                    "type": "video",
                    "order": "date",
                    "maxResults": max_results,
                    "key": self.api_key,
                },
                timeout=20,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            _tci_log.warning(
                "YouTube search API 오류 keyword=%s status=%s %s",
                keyword,
                getattr(e.response, "status_code", None),
                getattr(e.response, "text", "")[:200],
            )
            return []
        items = r.json().get("items", [])
        return [
            {
                "video_id": it["id"]["videoId"],
                "channel_id": it["snippet"]["channelId"],
                "channel_title": it["snippet"]["channelTitle"],
                "title": it["snippet"]["title"],
                "description": it["snippet"].get("description", ""),
                "published_at": it["snippet"]["publishedAt"].replace("T", " ").replace("Z", ""),
            }
            for it in items
        ]

    def video_tags(self, video_id: str) -> list[str]:
        try:
            r = requests.get(
                f"{self.BASE}/videos",
                params={"part": "snippet", "id": video_id, "key": self.api_key},
                timeout=20,
            )
            r.raise_for_status()
        except requests.HTTPError:
            return []
        items = r.json().get("items", [])
        if not items:
            return []
        return items[0].get("snippet", {}).get("tags", [])


class KeywordAI:
    def __init__(self, enabled: bool, provider: str, api_key: str, model: str, base_url: str):
        self.enabled = enabled
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def _extract_with_openai(self, title: str, description: str) -> list[str]:
        client = OpenAI(api_key=self.api_key)
        prompt = (
            "다음 유튜브 메타 정보에서 IT 관련 핵심 키워드 5개 이내를 JSON 배열 문자열로 반환해줘. "
            "일반 단어 제외, 검색 가능한 짧은 명사 위주.\n\n"
            f"제목: {title}\n설명: {description[:1000]}"
        )
        res = client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=120,
        )
        text = res.output_text.strip()
        out = json.loads(text)
        return [str(x) for x in out] if isinstance(out, list) else []

    def _extract_with_github_models(self, title: str, description: str) -> list[str]:
        token = self.api_key or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return []
        base_url = self.base_url or "https://models.github.ai/inference"
        client = OpenAI(base_url=base_url, api_key=token)
        prompt = (
            "다음 유튜브 메타 정보에서 IT 관련 핵심 키워드 5개 이내를 JSON 배열 문자열로 반환해줘. "
            "일반 단어 제외, 검색 가능한 짧은 명사 위주.\n\n"
            f"제목: {title}\n설명: {description[:1000]}"
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "developer", "content": "항상 JSON 배열 문자열만 응답하세요."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = (response.choices[0].message.content or "").strip()
        out = json.loads(text)
        return [str(x) for x in out] if isinstance(out, list) else []

    def classify_technical(self, title: str, description: str) -> bool:
        """제목·설명이 IT/개발/코딩 등 기술 관련이면 True, 아니면 False."""
        if not self.enabled:
            return True
        text = (title + " " + (description or ""))[:1500]
        try:
            if self.provider == "github_models":
                token = self.api_key or os.environ.get("GITHUB_TOKEN", "")
                if not token:
                    return True
                base_url = self.base_url or "https://models.github.ai/inference"
                client = OpenAI(base_url=base_url, api_key=token)
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "developer", "content": "반드시 JSON만 응답하세요. 예: {\"technical\": true}"},
                        {"role": "user", "content": f"다음 영상 제목과 설명을 보고 IT, 개발, 코딩 등 기술 관련 내용이 있으면 {{\"technical\": true}}, 없으면 {{\"technical\": false}}만 답하세요.\n\n제목: {title}\n설명: {text}"},
                    ],
                    temperature=0.1,
                )
                raw = (response.choices[0].message.content or "").strip()
            elif self.provider == "openai" and self.api_key:
                client = OpenAI(api_key=self.api_key)
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "user", "content": f"다음 영상 제목과 설명을 보고 IT, 개발, 코딩 등 기술 관련 내용이 있으면 {{\"technical\": true}}, 없으면 {{\"technical\": false}}만 JSON으로 답하세요.\n\n제목: {title}\n설명: {text}"},
                    ],
                    temperature=0.1,
                )
                raw = (response.choices[0].message.content or "").strip()
            else:
                return True
            out = json.loads(raw)
            return bool(out.get("technical", True))
        except Exception:
            return True

    def extract(self, title: str, description: str) -> list[str]:
        if self.enabled:
            try:
                if self.provider == "github_models":
                    keywords = self._extract_with_github_models(title, description)
                elif self.provider == "openai" and self.api_key:
                    keywords = self._extract_with_openai(title, description)
                else:
                    keywords = []
                if keywords:
                    return keywords
            except Exception:
                pass
        text = f"{title} {description}".lower()
        words = re.findall(r"[a-zA-Z0-9가-힣+#\.]{2,}", text)
        stop = {"영상", "채널", "today", "news", "shorts", "youtube"}
        uniq = []
        for w in words:
            if w not in stop and w not in uniq:
                uniq.append(w)
        return uniq[:5]


def _tci_collect_for_keyword(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI, kw: str
) -> int:
    """한 키워드에 대해 검색·채널/영상 upsert·태그 저장. 수집한 영상 수 반환."""
    tci_count = 0
    for video in tci_yt.search_latest(kw, max_results=10):
        tci_db.upsert_video(video)
        tci_count += 1
        tags = tci_yt.video_tags(video["video_id"])
        if tags:
            tci_db.save_tags(video["video_id"], tags)
        else:
            ai_keywords = tci_ai.extract(video["title"], video["description"])
            tci_db.save_ai_keywords(ai_keywords)
    return tci_count


def tci_collect_impl(tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI) -> None:
    tci_db.ensure_seed("it")
    keywords = tci_db.active_keywords()[:30]
    tci_collected = 0
    for kw in keywords:
        tci_collected += _tci_collect_for_keyword(tci_db, tci_yt, tci_ai, kw)
    _tci_log.info("수집: 키워드 %d개, 영상 %d건", len(keywords), tci_collected)
    if not keywords:
        _tci_log.warning("수집할 키워드가 없습니다. 키워드 테이블을 확인하세요.")
    elif tci_collected == 0:
        _tci_log.warning("수집된 영상이 0건입니다. YouTube API 키/할당량을 확인하세요.")


def tci_collect_one_due_keyword(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI
) -> None:
    """크론용: updated_date가 오늘 이전인 키워드 한 개만 수집 후 해당 키워드 updated_date를 오늘로 갱신."""
    tci_db.ensure_seed("it")
    kw = tci_db.get_one_keyword_due_for_collect()
    if not kw:
        _tci_log.info("수집 대상 키워드 없음 (오늘 이미 갱신된 키워드만 있거나 키워드 없음)")
        return
    tci_collected = _tci_collect_for_keyword(tci_db, tci_yt, tci_ai, kw)
    tci_db.set_keyword_updated_today(kw)
    _tci_log.info("수집: 키워드 1개(%s), 영상 %d건, updated_date 오늘로 갱신", kw, tci_collected)
    if tci_collected == 0:
        _tci_log.warning("수집된 영상이 0건입니다. YouTube API 키/할당량을 확인하세요.")


def tci_classify_new_videos(tci_db: Database, tci_ai: KeywordAI) -> None:
    """status가 NEW인 영상을 한 건씩 AI로 판별해, 기술 관련이면 UNWATCHED로 변경."""
    for video_id, title, description in tci_db.get_new_videos():
        if tci_ai.classify_technical(title, description):
            tci_db.set_watch_status(video_id, "UNWATCHED")


def load_config(base_dir: str | None = None) -> Config:
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    tci_env_path = os.path.join(base_dir, ".env")
    load_dotenv(tci_env_path)
    try:
        mysql_port = int(os.environ.get("MYSQL_PORT", "3306"))
    except ValueError:
        mysql_port = 3306
    return Config(
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""),
        mysql={
            "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
            "port": mysql_port,
            "user": os.environ.get("MYSQL_USER", "root"),
            "password": os.environ.get("MYSQL_PASSWORD", ""),
            "database": os.environ.get("MYSQL_DATABASE", "tci_itnews"),
        },
        ai_enabled=os.environ.get("AI_ENABLED", "false").lower() in ("true", "1", "yes"),
        ai_provider=os.environ.get("AI_PROVIDER", "github_models"),
        ai_api_key=os.environ.get("AI_API_KEY", ""),
        ai_model=os.environ.get("AI_MODEL", "openai/o4-mini"),
        ai_base_url=os.environ.get("AI_BASE_URL", "https://models.github.ai/inference"),
        auth_totp_secret=os.environ.get("AUTH_TOTP_SECRET", ""),
        auth_app_name=os.environ.get("AUTH_APP_NAME", "TCI IT News"),
    )
