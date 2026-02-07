from __future__ import annotations

import datetime as dt
import json
import importlib
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import mysql.connector
import requests
import tomli
from openai import OpenAI
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if importlib.util.find_spec("PyQt6.QtWebEngineWidgets"):
    QWebEngineView = importlib.import_module("PyQt6.QtWebEngineWidgets").QWebEngineView
else:
    QWebEngineView = None


@dataclass
class Config:
    youtube_api_key: str
    mysql: dict
    ai_enabled: bool
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str


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
            CREATE TABLE IF NOT EXISTS channels (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                channel_id VARCHAR(64) NOT NULL UNIQUE,
                title VARCHAR(512) NOT NULL,
                is_live TINYINT(1) NOT NULL DEFAULT 0,
                live_video_id VARCHAR(64) NULL,
                updated_at DATETIME NOT NULL
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
                status ENUM('UNWATCHED','WATCHING','WATCHED') NOT NULL DEFAULT 'UNWATCHED',
                last_seen_at DATETIME NOT NULL,
                watch_updated_at DATETIME NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
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

    def upsert_channel(self, channel_id: str, title: str, is_live: bool, live_video_id: str | None) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO channels (channel_id, title, is_live, live_video_id, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                title=VALUES(title),
                is_live=VALUES(is_live),
                live_video_id=VALUES(live_video_id),
                updated_at=NOW()
            """,
            (channel_id, title, int(is_live), live_video_id),
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

    def list_videos(self, query: str = "", status: str = "") -> list[tuple]:
        cur = self.conn.cursor()
        sql = """
            SELECT v.video_id, v.title, v.channel_id, v.published_at, v.status
            FROM videos v
            WHERE (%s = '' OR v.title LIKE CONCAT('%%', %s, '%%')
               OR EXISTS (SELECT 1 FROM video_tags t WHERE t.video_id = v.video_id AND t.tag LIKE CONCAT('%%', %s, '%%')))
              AND (%s = '' OR v.status = %s)
            ORDER BY v.published_at DESC, FIELD(v.status, 'UNWATCHED','WATCHING','WATCHED')
        """
        cur.execute(sql, (query, query, query, status, status))
        rows = cur.fetchall()
        cur.close()
        return rows

    def set_watch_status(self, video_id: str, status: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE videos SET status=%s, watch_updated_at=NOW() WHERE video_id=%s",
            (status, video_id),
        )
        cur.close()

    def tag_rankings(self, limit: int = 20) -> list[tuple]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT tag, COUNT(*) AS cnt
            FROM video_tags
            GROUP BY tag
            ORDER BY cnt DESC, tag ASC
            LIMIT %s
            """,
            (limit,),
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

    def all_channels(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT channel_id FROM channels")
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows

    def current_live(self) -> tuple[str, str] | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT channel_id, live_video_id FROM channels WHERE is_live=1 AND live_video_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        return row


class YouTubeService:
    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_latest(self, keyword: str, max_results: int = 15) -> list[dict]:
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
        r = requests.get(
            f"{self.BASE}/videos",
            params={"part": "snippet", "id": video_id, "key": self.api_key},
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return []
        return items[0].get("snippet", {}).get("tags", [])

    def has_live_now(self, channel_id: str) -> tuple[bool, str | None]:
        r = requests.get(
            f"{self.BASE}/search",
            params={
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 1,
                "key": self.api_key,
            },
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return False, None
        return True, items[0]["id"]["videoId"]


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


class MainWindow(QMainWindow):
    def __init__(self, db: Database, yt: YouTubeService, ai: KeywordAI):
        super().__init__()
        self.db = db
        self.yt = yt
        self.ai = ai
        self.setWindowTitle("IT News Collector")
        self.resize(1400, 900)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("검색(자동완성): 예) ai, 반도체, 클라우드")
        self.status_filter = QComboBox()
        self.status_filter.addItems(["", "UNWATCHED", "WATCHING", "WATCHED"])
        btn_refresh = QPushButton("새로고침")
        btn_refresh.clicked.connect(self.refresh_all)
        top.addWidget(QLabel("검색"))
        top.addWidget(self.search)
        top.addWidget(QLabel("상태"))
        top.addWidget(self.status_filter)
        top.addWidget(btn_refresh)
        layout.addLayout(top)

        mid = QHBoxLayout()
        self.video_table = QTableWidget(0, 5)
        self.video_table.setHorizontalHeaderLabels(["VideoID", "제목", "채널", "게시일", "상태"])
        self.video_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.video_table.itemSelectionChanged.connect(self.on_row_selected)
        mid.addWidget(self.video_table, stretch=3)

        side = QVBoxLayout()
        self.rank_list = QListWidget()
        self.rank_list.itemClicked.connect(self.on_rank_clicked)

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("키워드 추가")
        btn_add_kw = QPushButton("키워드 추가")
        btn_add_kw.clicked.connect(self.add_keyword)

        self.keyword_list = QListWidget()
        btn_del_kw = QPushButton("선택 키워드 삭제")
        btn_del_kw.clicked.connect(self.delete_keyword)

        side.addWidget(QLabel("태그 랭킹"))
        side.addWidget(self.rank_list)
        side.addWidget(QLabel("키워드 관리"))
        side.addWidget(self.keyword_input)
        side.addWidget(btn_add_kw)
        side.addWidget(self.keyword_list)
        side.addWidget(btn_del_kw)
        mid.addLayout(side, stretch=2)
        layout.addLayout(mid)

        ctrl = QHBoxLayout()
        btn_unwatched = QPushButton("안봄")
        btn_watching = QPushButton("시청")
        btn_watched = QPushButton("다봄")
        btn_unwatched.clicked.connect(lambda: self.update_status("UNWATCHED"))
        btn_watching.clicked.connect(lambda: self.update_status("WATCHING"))
        btn_watched.clicked.connect(lambda: self.update_status("WATCHED"))
        ctrl.addWidget(btn_unwatched)
        ctrl.addWidget(btn_watching)
        ctrl.addWidget(btn_watched)
        layout.addLayout(ctrl)

        self.live_label = QLabel("라이브: 없음")
        layout.addWidget(self.live_label)

        self.player = QWebEngineView() if QWebEngineView is not None else None
        if self.player is not None:
            self.player.setMinimumHeight(300)
            layout.addWidget(self.player)

        self.search.textChanged.connect(self.refresh_video_table)
        self.status_filter.currentTextChanged.connect(self.refresh_video_table)

        self.refresh_all()

    def refresh_all(self) -> None:
        try:
            self.collect()
            self.check_live()
            self.refresh_video_table()
            self.refresh_rankings()
            self.refresh_keyword_list()
            self.refresh_completer()
        except Exception as e:
            QMessageBox.warning(self, "오류", str(e))

    def collect(self) -> None:
        self.db.ensure_seed("it")
        for kw in self.db.active_keywords()[:30]:
            for video in self.yt.search_latest(kw, max_results=10):
                self.db.upsert_channel(video["channel_id"], video["channel_title"], False, None)
                self.db.upsert_video(video)
                tags = self.yt.video_tags(video["video_id"])
                if tags:
                    self.db.save_tags(video["video_id"], tags)
                else:
                    ai_keywords = self.ai.extract(video["title"], video["description"])
                    self.db.save_ai_keywords(ai_keywords)

    def check_live(self) -> None:
        for channel_id in self.db.all_channels():
            is_live, live_video_id = self.yt.has_live_now(channel_id)
            self.db.upsert_channel(channel_id, channel_id, is_live, live_video_id)

        live = self.db.current_live()
        if live:
            _, vid = live
            self.live_label.setText(f"라이브 감지됨: {vid}")
            if self.player is not None:
                self.player.setHtml(
                    f"""
                    <html><body style='margin:0;background:#000;'>
                    <iframe width='100%' height='100%' src='https://www.youtube.com/embed/{vid}?autoplay=1&mute=1'
                    frameborder='0' allow='autoplay; encrypted-media' allowfullscreen></iframe>
                    </body></html>
                    """
                )
        else:
            self.live_label.setText("라이브: 없음")
            if self.player is not None:
                self.player.setHtml("<html><body></body></html>")

    def refresh_video_table(self) -> None:
        rows = self.db.list_videos(self.search.text().strip(), self.status_filter.currentText().strip())
        self.video_table.setRowCount(0)
        for r, row in enumerate(rows):
            self.video_table.insertRow(r)
            for c, value in enumerate(row):
                self.video_table.setItem(r, c, QTableWidgetItem(str(value)))

    def refresh_rankings(self) -> None:
        self.rank_list.clear()
        for tag, cnt in self.db.tag_rankings():
            self.rank_list.addItem(f"{tag} ({cnt})")

    def refresh_keyword_list(self) -> None:
        self.keyword_list.clear()
        for kw in self.db.active_keywords()[:200]:
            self.keyword_list.addItem(kw)

    def refresh_completer(self) -> None:
        comp = QCompleter(self.db.all_suggest_words())
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search.setCompleter(comp)

    def add_keyword(self) -> None:
        keyword = self.keyword_input.text().strip().lower()
        if not keyword:
            return
        self.db.save_ai_keywords([keyword])
        self.keyword_input.clear()
        self.refresh_keyword_list()
        self.refresh_completer()

    def delete_keyword(self) -> None:
        item = self.keyword_list.currentItem()
        if not item:
            return
        kw = item.text()
        cur = self.db.conn.cursor()
        cur.execute("DELETE FROM keywords WHERE keyword=%s", (kw,))
        cur.close()
        self.refresh_keyword_list()
        self.refresh_completer()

    def on_rank_clicked(self, item: QListWidgetItem) -> None:
        tag = item.text().split(" (")[0]
        self.search.setText(tag)
        self.refresh_video_table()

    def on_row_selected(self) -> None:
        row = self.video_table.currentRow()
        if row < 0:
            return
        self.db.set_watch_status(self.video_table.item(row, 0).text(), "WATCHING")
        self.refresh_video_table()

    def update_status(self, status: str) -> None:
        row = self.video_table.currentRow()
        if row < 0:
            return
        self.db.set_watch_status(self.video_table.item(row, 0).text(), status)
        self.refresh_video_table()


def load_config() -> Config:
    with open("config.toml", "rb") as f:
        raw = tomli.load(f)
    return Config(
        youtube_api_key=raw["youtube"]["api_key"],
        mysql=raw["mysql"],
        ai_enabled=bool(raw.get("ai", {}).get("enabled", False)),
        ai_provider=raw.get("ai", {}).get("provider", "github_models"),
        ai_api_key=raw.get("ai", {}).get("api_key", ""),
        ai_model=raw.get("ai", {}).get("model", "openai/o4-mini"),
        ai_base_url=raw.get("ai", {}).get("base_url", "https://models.github.ai/inference"),
    )


def main() -> None:
    cfg = load_config()
    app = QApplication(sys.argv)
    db = Database(cfg)
    yt = YouTubeService(cfg.youtube_api_key)
    ai = KeywordAI(cfg.ai_enabled, cfg.ai_provider, cfg.ai_api_key, cfg.ai_model, cfg.ai_base_url)
    win = MainWindow(db, yt, ai)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
