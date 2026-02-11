# tci_core: IT News Collector 공통 백엔드 (Flask/PyQt 공용)
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

_tci_log = logging.getLogger(__name__)

import pymysql
from pymysql.constants import CLIENT
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
    _tci_schema_lock = threading.Lock()
    _tci_schema_initialized = False

    def __init__(self, cfg: Config):
        self.conn = pymysql.connect(
            host=cfg.mysql.get("host"),
            port=cfg.mysql.get("port"),
            user=cfg.mysql.get("user"),
            password=cfg.mysql.get("password"),
            database=cfg.mysql.get("database"),
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
            cursorclass=pymysql.cursors.Cursor,
            client_flag=CLIENT.MULTI_STATEMENTS | CLIENT.MULTI_RESULTS,
        )
        with Database._tci_schema_lock:
            if not Database._tci_schema_initialized:
                self._init_schema()
                Database._tci_schema_initialized = True

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
        try:
            # 수동 입력 구분을 위해 source 열 ENUM 항목 확장.
            cur.execute(
                "ALTER TABLE keywords MODIFY COLUMN source ENUM('seed','manual','tag','ai') NOT NULL DEFAULT 'seed'"
            )
        except Exception:
            # 이미 확장된 경우에는 에러를 무시.
            pass
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id VARCHAR(64) PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                created_date DATE NOT NULL,
                updated_date DATE NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def hide_video(self, video_id: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("INSERT IGNORE INTO hidden_videos (video_id) VALUES (%s)", (video_id,))
        finally:
            cur.close()

    def hide_videos_by_tag(self, tag: str) -> None:
        """해당 태그가 붙은 모든 영상을 숨김 처리."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT IGNORE INTO hidden_videos (video_id) SELECT DISTINCT video_id FROM video_tags WHERE tag = %s",
                (tag.strip().lower(),),
            )
        finally:
            cur.close()

    def hide_videos_by_keyword(self, keyword: str) -> None:
        """키워드 검색 조건(제목 또는 태그 포함)에 맞는 모든 영상을 숨김 처리. 리스트 검색 결과와 동일 기준."""
        cur = self.conn.cursor()
        kw = keyword.strip().lower()
        try:
            cur.execute(
                """
                INSERT IGNORE INTO hidden_videos (video_id)
                SELECT DISTINCT v.video_id FROM videos v
                WHERE v.title LIKE CONCAT('%%', %s, '%%')
                   OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%'))
                """,
                (kw, kw),
            )
        finally:
            cur.close()

    def ensure_seed(self, keyword: str) -> None:
        """시드 키워드가 없으면 추가. 이미 있으면 updated_date를 건드리지 않음 (크론 수집 대상 유지)."""
        today = dt.date.today()
        yesterday = today - dt.timedelta(days=1)
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO keywords (keyword, source, created_date, updated_date)
                VALUES (%s, 'seed', %s, %s)
                ON DUPLICATE KEY UPDATE keyword = keyword
                """,
                (keyword.lower(), today, yesterday),
            )
        finally:
            cur.close()

    def save_channel(self, channel_id: str, title: str) -> None:
        cid = (channel_id or "").strip()
        if not cid:
            return
        today = dt.date.today()
        yesterday = today - dt.timedelta(days=1)
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO channels (channel_id, title, created_date, updated_date)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE title = VALUES(title)
                """,
                (cid, (title or "").strip(), today, yesterday),
            )
        finally:
            cur.close()

    def update_channel_title(self, channel_id: str, title: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("UPDATE channels SET title = %s WHERE channel_id = %s", ((title or "").strip(), channel_id))
        finally:
            cur.close()

    def list_channels(self, limit: int | None = None) -> list[tuple[str, str, dt.date, dt.date]]:
        cur = self.conn.cursor()
        try:
            sql = (
                "SELECT channel_id, title, created_date, updated_date "
                "FROM channels ORDER BY updated_date DESC, created_date DESC"
            )
            if limit is not None:
                sql += " LIMIT %s"
                cur.execute(sql, (limit,))
            else:
                cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def delete_channel(self, channel_id: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("DELETE FROM channels WHERE channel_id = %s", (channel_id,))
        finally:
            cur.close()

    def get_one_channel_due_for_collect(self) -> tuple[str, str] | None:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT channel_id, title FROM channels WHERE DATE(updated_date) < CURDATE() "
                "ORDER BY updated_date ASC, created_date ASC LIMIT 1"
            )
            row = cur.fetchone()
            return row if row else None
        finally:
            cur.close()

    def set_channel_updated_today(self, channel_id: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("UPDATE channels SET updated_date = CURDATE() WHERE channel_id = %s", (channel_id,))
        finally:
            cur.close()

    def active_keywords(self) -> list[str]:
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT keyword FROM keywords ORDER BY updated_date DESC, id DESC")
            rows = [r[0] for r in cur.fetchall()]
            return rows
        finally:
            cur.close()

    def get_one_keyword_due_for_collect(self) -> str | None:
        """updated_date가 오늘 이전인 키워드 하나만 반환 (가장 오래된 것부터). 크론 한 건 수집용."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT keyword FROM keywords WHERE DATE(updated_date) < CURDATE() ORDER BY updated_date ASC, id ASC LIMIT 1"
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()

    def set_keyword_updated_today(self, keyword: str) -> None:
        """해당 키워드의 updated_date를 오늘 날짜로 변경."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                "UPDATE keywords SET updated_date = CURDATE() WHERE keyword = %s",
                (keyword.lower(),),
            )
        finally:
            cur.close()

    def upsert_video(self, payload: dict) -> None:
        cur = self.conn.cursor()
        try:
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
        finally:
            cur.close()

    def save_tags(self, video_id: str, tags: Iterable[str]) -> None:
        today = dt.date.today()
        cur = self.conn.cursor()
        try:
            for raw in {t for t in tags if t and len(t.strip()) >= 2}:
                tag = raw.strip().lower()[:255]
                if len(tag) < 2:
                    continue
                cur.execute(
                    """
                    INSERT INTO video_tags (video_id, tag, collected_date)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE collected_date = VALUES(collected_date)
                    """,
                    (video_id, tag, today),
                )
        finally:
            cur.close()

    def save_manual_keywords(self, keywords: Iterable[str]) -> None:
        today = dt.date.today()
        cur = self.conn.cursor()
        try:
            for kw in {k.strip().lower() for k in keywords if k and len(k.strip()) >= 2}:
                cur.execute(
                    """
                    INSERT INTO keywords (keyword, source, created_date, updated_date)
                    VALUES (%s, 'manual', %s, %s)
                    ON DUPLICATE KEY UPDATE source = VALUES(source), updated_date = VALUES(updated_date)
                    """,
                    (kw, today, today),
                )
        finally:
            cur.close()

    def get_video_tags(self, video_id: str) -> list[str]:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT tag FROM video_tags WHERE video_id = %s ORDER BY tag ASC",
                (video_id,),
            )
            tags = [row[0] for row in cur.fetchall()]
            return tags
        finally:
            cur.close()

    def list_videos(
        self, query: str = "", status: str = "", limit: int | None = None, offset: int | None = None
    ) -> list[tuple]:
        cur = self.conn.cursor()
        try:
            sql = """
                SELECT v.video_id, v.title, v.channel_id, v.published_at, v.status
                FROM videos v
                WHERE (%s = '' OR v.title LIKE CONCAT('%%', %s, '%%')
                   OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%')))
                  AND (
                        %s = ''
                        OR (
                            %s = 'UNWATCHED'
                            AND v.status IN ('NEW','UNWATCHED')
                        )
                        OR (%s <> 'UNWATCHED' AND v.status = %s)
                  )
                  AND NOT EXISTS (SELECT 1 FROM hidden_videos h WHERE h.video_id = v.video_id)
                ORDER BY v.published_at DESC, FIELD(v.status, 'NEW','UNWATCHED','WATCHING','WATCHED')
            """
            params = (query, query, query, status, status, status, status)
            if limit is not None and offset is not None:
                sql += " LIMIT %s OFFSET %s"
                params += (limit, offset)
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def list_videos_count(self, query: str = "", status: str = "") -> int:
        cur = self.conn.cursor()
        try:
            sql = """
                SELECT COUNT(*)
                FROM videos v
                WHERE (%s = '' OR v.title LIKE CONCAT('%%', %s, '%%')
                   OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%')))
                  AND (
                        %s = ''
                        OR (
                            %s = 'UNWATCHED'
                            AND v.status IN ('NEW','UNWATCHED')
                        )
                        OR (%s <> 'UNWATCHED' AND v.status = %s)
                  )
                  AND NOT EXISTS (SELECT 1 FROM hidden_videos h WHERE h.video_id = v.video_id)
            """
            cur.execute(sql, (query, query, query, status, status, status, status))
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            cur.close()

    def set_watch_status(self, video_id: str, status: str) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "UPDATE videos SET status=%s, watch_updated_at=NOW() WHERE video_id=%s",
                (status, video_id),
            )
        finally:
            cur.close()

    def get_new_videos(self) -> list[tuple[str, str, str]]:
        """status가 NEW인 영상 목록 (video_id, title, description)."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT video_id, title, COALESCE(description, '') FROM videos WHERE status = 'NEW' ORDER BY id ASC"
            )
            return cur.fetchall()
        finally:
            cur.close()

    def all_suggest_words(self) -> list[str]:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT keyword FROM keywords WHERE source IN ('seed','manual')"
            )
            kws = [r[0] for r in cur.fetchall()]
            return sorted(set(kws))
        finally:
            cur.close()

    def delete_keyword(self, keyword: str) -> None:
        kw = keyword.strip().lower()
        self.hide_videos_by_keyword(kw)
        cur = self.conn.cursor()
        try:
            cur.execute("DELETE FROM keywords WHERE keyword=%s", (kw,))
        finally:
            cur.close()


class YouTubeService:
    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_latest_paginated(
        self, keyword: str, max_results: int = 50, max_pages: int | None = None, delay_sec: int = 90
    ) -> list[dict]:
        """YouTube 검색 결과를 페이지 단위로 끝까지 수집."""
        results: list[dict] = []
        next_token: str | None = None
        page = 0
        while True:
            page += 1
            if max_pages is not None and page > max_pages:
                break
            params = {
                "part": "snippet",
                "q": keyword,
                "type": "video",
                "order": "date",
                "maxResults": min(max_results, 50),
                "key": self.api_key,
            }
            if next_token:
                params["pageToken"] = next_token
            try:
                r = requests.get(f"{self.BASE}/search", params=params, timeout=20)
                r.raise_for_status()
            except requests.HTTPError as e:
                _tci_log.warning(
                    "YouTube search API 오류 keyword=%s status=%s %s",
                    keyword,
                    getattr(e.response, "status_code", None),
                    getattr(e.response, "text", "")[:200],
                )
                break
            payload = r.json()
            items = payload.get("items", [])
            for it in items:
                info = it.get("id", {})
                video_id = info.get("videoId")
                if not video_id:
                    continue
                snippet = it.get("snippet", {})
                results.append(
                    {
                        "video_id": video_id,
                        "channel_id": snippet.get("channelId", ""),
                        "channel_title": snippet.get("channelTitle", ""),
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "published_at": snippet.get("publishedAt", "").replace("T", " ").replace("Z", ""),
                    }
                )
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            time.sleep(delay_sec)
        return results

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

    def channel_details(self, channel_id: str) -> dict | None:
        try:
            r = requests.get(
                f"{self.BASE}/channels",
                params={"part": "snippet", "id": channel_id, "key": self.api_key},
                timeout=20,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            _tci_log.warning(
                "YouTube channel API 오류 channel=%s status=%s %s",
                channel_id,
                getattr(e.response, "status_code", None),
                getattr(e.response, "text", "")[:200],
            )
            return None
        items = r.json().get("items", [])
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        return {
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
        }

    def channel_latest_paginated(
        self, channel_id: str, max_results: int = 50, max_pages: int | None = None, delay_sec: int = 90
    ) -> list[dict]:
        """채널의 최신 영상을 페이지 단위로 끝까지 수집."""
        results: list[dict] = []
        next_token: str | None = None
        page = 0
        while True:
            page += 1
            if max_pages is not None and page > max_pages:
                break
            params = {
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "maxResults": min(max_results, 50),
                "key": self.api_key,
            }
            if next_token:
                params["pageToken"] = next_token
            try:
                r = requests.get(f"{self.BASE}/search", params=params, timeout=20)
                r.raise_for_status()
            except requests.HTTPError as e:
                _tci_log.warning(
                    "YouTube channel search 오류 channel=%s status=%s %s",
                    channel_id,
                    getattr(e.response, "status_code", None),
                    getattr(e.response, "text", "")[:200],
                )
                break
            payload = r.json()
            items = payload.get("items", [])
            for it in items:
                info = it.get("id", {})
                video_id = info.get("videoId")
                if not video_id:
                    continue
                snippet = it.get("snippet", {})
                results.append(
                    {
                        "video_id": video_id,
                        "channel_id": snippet.get("channelId", channel_id),
                        "channel_title": snippet.get("channelTitle", ""),
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "published_at": snippet.get("publishedAt", "").replace("T", " ").replace("Z", ""),
                    }
                )
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            time.sleep(delay_sec)
        return results


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
    for video in tci_yt.search_latest_paginated(kw, max_results=50):
        tci_db.upsert_video(video)
        tci_count += 1
        tags = tci_yt.video_tags(video["video_id"])
        if tags:
            tci_db.save_tags(video["video_id"], tags)
    return tci_count


def _tci_collect_for_channel(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI, channel_id: str
) -> int:
    """한 채널의 최신 영상을 수집하고 태그 저장. 수집한 영상 수 반환."""
    meta = tci_yt.channel_details(channel_id)
    if meta and meta.get("title"):
        tci_db.update_channel_title(channel_id, meta.get("title", ""))
    tci_count = 0
    for video in tci_yt.channel_latest_paginated(channel_id, max_results=50):
        tci_db.upsert_video(video)
        tci_count += 1
        tags = tci_yt.video_tags(video["video_id"])
        if tags:
            tci_db.save_tags(video["video_id"], tags)
    return tci_count


def _tci_collect_next_keyword(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI
) -> tuple[str | None, int]:
    kw = tci_db.get_one_keyword_due_for_collect()
    if not kw:
        _tci_log.info("수집 대상 키워드 없음 (오늘 이미 갱신된 키워드만 있거나 키워드 없음)")
        return None, 0
    tci_collected = _tci_collect_for_keyword(tci_db, tci_yt, tci_ai, kw)
    tci_db.set_keyword_updated_today(kw)
    _tci_log.info("수집: 키워드 %s, 영상 %d건", kw, tci_collected)
    if tci_collected == 0:
        _tci_log.warning("키워드 %s 수집 결과 0건입니다. YouTube API 키/할당량을 확인하세요.", kw)
    return kw, tci_collected


def _tci_collect_next_channel(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI
) -> tuple[str | None, int]:
    channel = tci_db.get_one_channel_due_for_collect()
    if not channel:
        _tci_log.info("수집 대상 채널 없음 (오늘 이미 갱신된 채널만 있거나 채널 없음)")
        return None, 0
    channel_id, title = channel
    tci_collected = _tci_collect_for_channel(tci_db, tci_yt, tci_ai, channel_id)
    tci_db.set_channel_updated_today(channel_id)
    _tci_log.info(
        "수집: 채널 %s, 영상 %d건",
        title or channel_id,
        tci_collected,
    )
    if tci_collected == 0:
        _tci_log.warning("채널 %s 수집 결과 0건입니다. YouTube API 키/할당량을 확인하세요.", title or channel_id)
    return channel_id, tci_collected


def tci_collect_impl(tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI) -> None:
    tci_db.ensure_seed("it")
    kw, kw_count = _tci_collect_next_keyword(tci_db, tci_yt, tci_ai)
    ch, ch_count = _tci_collect_next_channel(tci_db, tci_yt, tci_ai)
    total = kw_count + ch_count
    _tci_log.info(
        "수집 요약: 키워드=%s(%d건) 채널=%s(%d건) 총 %d건",
        kw or "없음",
        kw_count,
        ch or "없음",
        ch_count,
        total,
    )
    if not kw and not ch:
        _tci_log.warning("수집 대상 키워드/채널이 없습니다. 테이블을 확인하세요.")


def tci_collect_one_due_keyword(
    tci_db: Database, tci_yt: YouTubeService, tci_ai: KeywordAI
) -> None:
    """크론용: updated_date가 오늘 이전인 키워드와 채널을 각 1개씩 시도."""
    tci_collect_impl(tci_db, tci_yt, tci_ai)


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
