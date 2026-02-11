# 크론탭용 수집 전용 스크립트. 주기적으로 실행해 영상 수집.
# 사용 예: 0 */6 * * * cd /path/to/tci_itnews && python tci_cron_collect.py >> logs/cron.log 2>&1
# --reset-due: 모든 키워드의 updated_date를 어제로 되돌려 수집 대상으로 만듦 (최초 1회 또는 이전 버그 복구용)
from __future__ import annotations

import logging
import os
import sys

from tci_core import (
    Database,
    KeywordAI,
    YouTubeService,
    load_config,
    tci_classify_new_videos,
    tci_collect_one_due_keyword,
)

# 스크립트 기준 디렉터리에서 .env 로드
_TCI_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _tci_setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main() -> int:
    _tci_setup_logging()
    log = logging.getLogger(__name__)
    log.info("tci_cron_collect 시작")

    try:
        cfg = load_config(_TCI_SCRIPT_DIR)
        db = Database(cfg)
        if "--reset-due" in sys.argv:
            cur = db.conn.cursor()
            cur.execute("UPDATE keywords SET updated_date = CURDATE() - INTERVAL 1 DAY")
            kw_count = cur.rowcount
            cur.execute("UPDATE channels SET updated_date = CURDATE() - INTERVAL 1 DAY")
            ch_count = cur.rowcount
            cur.close()
            log.info(
                "--reset-due: 키워드 %d개, 채널 %d개 updated_date를 어제로 초기화함",
                kw_count,
                ch_count,
            )
        yt = YouTubeService(cfg.youtube_api_key)
        ai = KeywordAI(
            cfg.ai_enabled,
            cfg.ai_provider,
            cfg.ai_api_key,
            cfg.ai_model,
            cfg.ai_base_url,
        )
        tci_collect_one_due_keyword(db, yt, ai)
        log.info("수집 완료")
        tci_classify_new_videos(db, ai)
        log.info("NEW 영상 분류 완료")
        db.conn.close()
        log.info("tci_cron_collect 정상 종료")
        return 0
    except Exception as e:
        log.exception("tci_cron_collect 오류: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
