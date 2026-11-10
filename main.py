from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import sys
import textwrap
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "monitor_state.sqlite3"
BOARD_URL = "https://gall.dcinside.com/mgallery/board/lists/?id=retrogame"
CONCEPT_BOARD_URL = BOARD_URL + "&exception_mode=recommend"
BOARD_ID = "retrogame"
BASE_URL = "https://gall.dcinside.com"
_SITE_REQUEST_LOCK = threading.Lock()
_LAST_SITE_REQUEST_AT = 0.0
_BOARD_CACHE_LOCK = threading.Lock()
_BOARD_CACHE_AT = 0.0
_BOARD_CACHE_POSTS: list["Post"] | None = None
BOARD_CACHE_SECONDS = 30
_CONCEPT_CACHE_LOCK = threading.Lock()
_CONCEPT_CACHE_AT = 0.0
_CONCEPT_CACHE_POSTS: list["Post"] | None = None
CONCEPT_CACHE_SECONDS = 30
# Use two blank lines between all top-level Telegram message blocks.
BLOCK_SEPARATOR = "\n\n\n"


def normalize_author_name(author: str) -> str:
    """Normalize DCInside floating nicknames while retaining the IP identity.

    A floating nickname can be rendered as ``레갤러1(1.2.3.4)`` and the
    trailing number is only a display disambiguator.  Remove that number when
    the parenthesized value is an IP address, but keep the IP itself so two
    users with the same visible nickname remain distinguishable.  Semianon
    identifiers such as ``(abc123)`` are left unchanged.
    """
    value = str(author or "").strip()
    match = re.fullmatch(r"(.+?)\((\d{1,3}(?:\.\d{1,3}){1,3})\)", value)
    if not match:
        return value
    nickname, ip = match.groups()
    nickname = re.sub(r"\d+$", "", nickname).strip() or nickname
    return f"{nickname}({ip})"


def author_key(author: str) -> str:
    return normalize_author_name(author).casefold()


def highlight_keywords(text: str, keywords: list[str]) -> str:
    """Wrap matched keyword text with Telegram emphasis markers."""
    value = str(text or "")
    words = sorted({str(word).strip() for word in keywords if str(word).strip()}, key=len, reverse=True)
    if not value or not words:
        return value
    pattern = re.compile("|".join(re.escape(word) for word in words), re.IGNORECASE)
    return pattern.sub(lambda match: f"‼️{match.group(0)}‼️", value)


PROFANITY_TERMS = (
    # Korean profanity and common obfuscated spellings.
    "시발", "씨발", "시이발", "씨이발", "시발놈", "시발년", "씨발놈", "씨발년",
    "ㅅㅂ", "ㅆㅂ", "ㅅㅂㄴ", "ㅅㅂㄴㅇ", "병신", "븅신", "병쉰", "븅쉰", "등신", "ㅂㅅ",
    "개새끼", "개새", "개년", "개놈", "개같다", "개같네", "개같은", "개좆",
    "새끼", "새꺄", "씹", "씹새", "씹새끼", "씹년", "씹놈", "씹할",
    "좆", "좇", "좆같", "좆같다", "좆같네", "좆까", "좆나", "좆밥", "좆됨", "좆돼",
    "지랄", "지럴", "염병", "육시랄", "썅", "썅년", "썅놈", "쌍년", "쌍놈",
    "미친놈", "미친년", "미친새끼", "또라이", "돌아이", "빡대가리", "빡친놈",
    "느금마", "느금", "니애미", "니엄마", "애미", "엠창", "앰창", "창녀", "걸레",
    "보지", "자지", "꼬추", "후장", "똥꼬", "씹질", "닥쳐", "꺼져", "꺼지라",
    "뒤져", "뒤질", "죽어", "죽여", "자살", "호구", "찐따", "한심한놈",
    # English terms commonly mixed into Korean comments.
    "fuck", "fucking", "shit", "bullshit", "bitch", "asshole", "motherfucker",
    "bastard", "cunt", "dick", "pussy", "dumbass", "jackass", "crap",
)

# Characters frequently used to evade simple word filters.  These are applied
# before punctuation/spacing removal, so the detector remains local and rule based.
PROFANITY_REPLACEMENTS = str.maketrans({
    "ㅆ": "ㅅ",
    "ㅄ": "ㅂㅅ",
    "쒸": "씨",
    "쉬": "시",
    "썅": "쌍",
    "숑": "송",
})


def normalize_profanity_text(text: str) -> str:
    """Reduce common spacing/symbol/number obfuscation without AI."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().translate(PROFANITY_REPLACEMENTS)
    value = re.sub(r"[0-9\s\W_]+", "", value, flags=re.UNICODE)
    # Collapse repeated characters such as ``씨이이발`` or ``ㅅㅅㅂ``.
    value = re.sub(r"(.)\1+", r"\1", value)
    return value


def contains_profanity(text: str) -> bool:
    normalized = normalize_profanity_text(text)
    return any(term in normalized for term in PROFANITY_TERMS)


DEFAULT_CONFIG = {
    "board_url": BOARD_URL,
    "topic_interval_minutes": 30,
    "topic_alert_enabled": True,
    "topic_batch_count": 30,
    "keyword_interval_minutes": 5,
    "new_title_interval_minutes": 5,
    "new_title_alert_enabled": True,
    "new_title_batch_count": 10,
    "newbie_title_alert_enabled": True,
    "newbie_title_batch_count": 5,
    "concept_alert_enabled": True,
    "concept_batch_count": 3,
    "title_keywords": [],
    "body_keywords": [],
    "comment_keywords": [],
    "include_embedded_links": True,
    "newbie_alert_enabled": True,
    "comment_batch_alert_enabled": False,
    "comment_batch_count": 50,
    "profanity_alert_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "recent_posts_to_scan": 30,
    "request_gap_seconds": 5,
    "request_timeout_seconds": 20,
}
LOG = logging.getLogger("retrogame_monitor")
STOP_WORDS = {
    "이거", "그거", "저거", "오늘", "어제", "요즘", "지금", "진짜", "근데", "그냥",
    "혹시", "어떻게", "하는", "하는데", "했는데", "있음", "없음", "가능", "질문",
    "생각", "사람", "이유", "정도", "관련", "때문", "하나", "아니", "같은", "같음",
}


@dataclass
class Post:
    post_id: str
    title: str
    url: str
    category: str = ""
    author: str = ""


def read_minutes(label: str, default: int) -> int:
    while True:
        raw = input(f"{label} (분, 기본 {default}): ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value >= 1:
                return value
        except ValueError:
            pass
        print("1 이상의 정수로 입력해 주세요.")


def create_config() -> dict[str, Any]:
    print("\n처음 실행 설정입니다. 주기는 나중에 config.json에서도 바꿀 수 있습니다.")
    config = dict(DEFAULT_CONFIG)
    config["topic_interval_minutes"] = read_minutes("주제 알림 주기", 30)
    config["keyword_interval_minutes"] = read_minutes("키워드 검색 주기", 5)
    raw_keywords = input("제목 키워드 (쉼표로 구분, 비우면 건너뜀): ").strip()
    config["title_keywords"] = [item.strip() for item in raw_keywords.split(",") if item.strip()]
    raw_body = input("본문 키워드 (쉼표로 구분, 비우면 건너뜀): ").strip()
    config["body_keywords"] = [item.strip() for item in raw_body.split(",") if item.strip()]
    raw_comments = input("댓글 키워드 (쉼표로 구분, 비우면 건너뜀): ").strip()
    config["comment_keywords"] = [item.strip() for item in raw_comments.split(",") if item.strip()]
    config["telegram_bot_token"] = input("Telegram Bot Token: ").strip()
    config["telegram_chat_id"] = input("Telegram Chat ID: ").strip()
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return create_config()
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"config.json을 읽을 수 없습니다: {exc}") from exc
    merged = {**DEFAULT_CONFIG, **config}
    merged["board_url"] = str(merged.get("board_url", BOARD_URL)).strip() or BOARD_URL
    for key in ("topic_interval_minutes", "keyword_interval_minutes", "new_title_interval_minutes"):
        try:
            merged[key] = max(1, int(merged[key]))
        except (ValueError, TypeError):
            merged[key] = DEFAULT_CONFIG[key]
    for key in ("topic_batch_count", "new_title_batch_count", "newbie_title_batch_count", "concept_batch_count", "comment_batch_count"):
        try:
            merged[key] = max(1, int(merged[key]))
        except (ValueError, TypeError):
            merged[key] = DEFAULT_CONFIG[key]
    try:
        merged["recent_posts_to_scan"] = min(200, max(1, int(merged.get("recent_posts_to_scan", 30))))
    except (ValueError, TypeError):
        merged["recent_posts_to_scan"] = DEFAULT_CONFIG["recent_posts_to_scan"]
    try:
        merged["request_gap_seconds"] = max(1, int(merged.get("request_gap_seconds", 5)))
    except (ValueError, TypeError):
        merged["request_gap_seconds"] = DEFAULT_CONFIG["request_gap_seconds"]
    # Keep compatibility with the previous single-keyword setting.
    legacy_keywords = merged.get("keywords", [])
    merged["title_keywords"] = [str(word).strip() for word in merged.get("title_keywords", legacy_keywords) if str(word).strip()]
    merged["body_keywords"] = [str(word).strip() for word in merged.get("body_keywords", []) if str(word).strip()]
    merged["comment_keywords"] = [str(word).strip() for word in merged.get("comment_keywords", []) if str(word).strip()]
    merged["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", merged.get("telegram_bot_token", ""))
    merged["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", merged.get("telegram_chat_id", ""))
    return merged


class State:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("CREATE TABLE IF NOT EXISTS seen (item_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS sent_titles (post_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, sent_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS post_title_alerts (post_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, reason TEXT NOT NULL, sent_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS title_pending (post_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, author TEXT NOT NULL, first_seen TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS concept_sent_titles (post_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, sent_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS concept_pending (post_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, author TEXT NOT NULL, first_seen TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS notification_history (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL, sent_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS author_history (author_key TEXT PRIMARY KEY, author TEXT NOT NULL, first_seen TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS newbie_history (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, kind TEXT NOT NULL, post_title TEXT NOT NULL, comment TEXT NOT NULL, post_url TEXT NOT NULL, detected_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS topic_posts (post_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, included_at TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS comment_history (post_id TEXT NOT NULL, comment_id TEXT NOT NULL, post_title TEXT NOT NULL, author TEXT NOT NULL, comment TEXT NOT NULL, post_url TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(post_id, comment_id))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS post_body_cache (post_id TEXT PRIMARY KEY, body TEXT NOT NULL, cached_at TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS comment_batch_pending (post_id TEXT NOT NULL, comment_id TEXT NOT NULL, post_title TEXT NOT NULL, author TEXT NOT NULL, comment TEXT NOT NULL, post_url TEXT NOT NULL, collected_at TEXT NOT NULL, PRIMARY KEY(post_id, comment_id))")
        self.conn.commit()

    def is_new(self, item_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen WHERE item_id = ?", (item_id,)).fetchone()
        if row:
            return False
        self.conn.execute("INSERT INTO seen(item_id, seen_at) VALUES (?, ?)", (item_id, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return True

    def has_seen(self, item_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE item_id = ?", (item_id,)).fetchone() is not None

    def close(self) -> None:
        self.conn.close()

    def title_tracking_initialized(self) -> bool:
        return self.conn.execute("SELECT value FROM meta WHERE key = 'title_tracking_initialized'").fetchone() is not None

    def startup_notification_done(self) -> bool:
        return self.conn.execute("SELECT value FROM meta WHERE key = 'startup_notification_done'").fetchone() is not None

    def mark_startup_notification_done(self) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('startup_notification_done', '1')")
        self.conn.commit()

    def observe_topic_posts(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO topic_posts(post_id, first_seen, included_at) VALUES (?, ?, NULL)",
            [(post.post_id, now) for post in posts],
        )
        self.conn.commit()

    def pending_topic_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM topic_posts WHERE included_at IS NULL").fetchone()
        return int(row[0] if row else 0)

    def mark_topic_alerted(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("UPDATE topic_posts SET included_at = ? WHERE included_at IS NULL", (now,))
        self.conn.commit()

    def initialize_topic_tracking(self, posts: list[Post]) -> None:
        self.observe_topic_posts(posts)
        self.mark_topic_alerted()

    def record_comments(self, post: Post, comments: list[tuple[str, str, str]]) -> None:
        if not comments:
            return
        # Read known IDs once and insert only comments that have never been
        # recorded.  This avoids repeated INSERT attempts on every poll.
        unique_comments: list[tuple[str, str, str]] = []
        seen_ids: set[str] = set()
        for comment_id, author, comment in comments:
            comment_id = str(comment_id)
            if comment_id and comment_id not in seen_ids:
                unique_comments.append((comment_id, author, comment))
                seen_ids.add(comment_id)
        if not unique_comments:
            return
        placeholders = ",".join("?" for _ in unique_comments)
        rows = self.conn.execute(
            f"SELECT comment_id FROM comment_history WHERE post_id = ? AND comment_id IN ({placeholders})",
            [post.post_id, *seen_ids],
        ).fetchall()
        known_ids = {str(row[0]) for row in rows}
        new_comments = [item for item in unique_comments if item[0] not in known_ids]
        if not new_comments:
            return
        now = datetime.now().isoformat(timespec="seconds")
        display_post_title = f"{post.author} : {post.title}" if post.author else post.title
        self.conn.executemany(
            "INSERT INTO comment_history(post_id, comment_id, post_title, author, comment, post_url, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(post.post_id, comment_id, post.title, author, comment, post.url, now) for comment_id, author, comment in new_comments],
        )
        self.conn.executemany(
            "INSERT INTO comment_batch_pending(post_id, comment_id, post_title, author, comment, post_url, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(post.post_id, comment_id, display_post_title, author, comment, post.url, now) for comment_id, author, comment in new_comments],
        )
        self.conn.commit()

    def save_post_body(self, post_id: str, body: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO post_body_cache(post_id, body, cached_at) VALUES (?, ?, ?)",
            (post_id, body, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_post_body(self, post_id: str) -> str:
        row = self.conn.execute("SELECT body FROM post_body_cache WHERE post_id = ?", (post_id,)).fetchone()
        return str(row[0]) if row else ""

    def pending_comment_batch(self, limit: int = 100) -> list[tuple[str, str, str, str, str, str]]:
        return self.conn.execute(
            "SELECT post_id, comment_id, post_title, author, comment, post_url "
            "FROM comment_batch_pending ORDER BY collected_at, rowid LIMIT ?", (limit,)
        ).fetchall()

    def mark_comment_batch_sent(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        self.conn.executemany(
            "DELETE FROM comment_batch_pending WHERE post_id = ? AND comment_id = ?",
            [(row[0], row[1]) for row in rows],
        )
        self.conn.commit()

    def initialize_title_tracking(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO sent_titles(post_id, title, url, sent_at) VALUES (?, ?, ?, ?)", [(p.post_id, p.title, p.url, now) for p in posts])
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('title_tracking_initialized', '1')")
        self.conn.commit()

    def post_title_alerted(self, post_id: str) -> bool:
        # sent_titles and concept_sent_titles also contain baseline rows that
        # were observed without necessarily being sent to Telegram.  Only the
        # unified table represents an actual post-title notification. Concept
        # alerts are intentionally independent and may repeat a title already
        # sent by another post alert.
        row = self.conn.execute(
            "SELECT 1 FROM post_title_alerts WHERE post_id = ? AND reason <> 'concept' LIMIT 1",
            (post_id,),
        ).fetchone()
        return row is not None

    def claim_post_title_alert(self, post: Post, reason: str) -> bool:
        """Claim a post title so later alert reasons cannot resend it."""
        if self.post_title_alerted(post.post_id):
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO post_title_alerts(post_id, title, url, reason, sent_at) VALUES (?, ?, ?, ?, ?)",
            (post.post_id, post.title, post.url, reason, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unsent_titles(self, posts: list[Post]) -> list[Post]:
        sent = {row[0] for row in self.conn.execute("SELECT post_id FROM sent_titles")}
        return [post for post in posts if post.post_id not in sent]

    def mark_titles_sent(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO sent_titles(post_id, title, url, sent_at) VALUES (?, ?, ?, ?)", [(p.post_id, p.title, p.url, now) for p in posts])
        self.conn.executemany("INSERT OR IGNORE INTO post_title_alerts(post_id, title, url, reason, sent_at) VALUES (?, ?, ?, 'new_title', ?)", [(p.post_id, p.title, p.url, now) for p in posts])
        self.conn.executemany("DELETE FROM title_pending WHERE post_id = ?", [(p.post_id,) for p in posts])
        self.conn.commit()

    def observe_title_candidates(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO title_pending(post_id, title, url, author, first_seen) "
            "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM sent_titles WHERE post_id = ?)",
            [(p.post_id, p.title, p.url, p.author, now, p.post_id) for p in posts],
        )
        self.conn.commit()

    def pending_titles(self) -> list[Post]:
        rows = self.conn.execute("SELECT post_id, title, url, author FROM title_pending ORDER BY first_seen, rowid").fetchall()
        return [Post(post_id, title, url, "", author) for post_id, title, url, author in rows]

    def initialize_concept_tracking(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO concept_sent_titles(post_id, title, url, sent_at) VALUES (?, ?, ?, ?)", [(p.post_id, p.title, p.url, now) for p in posts])
        self.conn.commit()

    def observe_concept_candidates(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO concept_pending(post_id, title, url, author, first_seen) "
            "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM concept_sent_titles WHERE post_id = ?)",
            [(p.post_id, p.title, p.url, p.author, now, p.post_id) for p in posts],
        )
        self.conn.commit()

    def pending_concept_titles(self) -> list[Post]:
        rows = self.conn.execute("SELECT post_id, title, url, author FROM concept_pending ORDER BY first_seen, rowid").fetchall()
        return [Post(post_id, title, url, "", author) for post_id, title, url, author in rows]

    def mark_concept_titles_sent(self, posts: list[Post]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO concept_sent_titles(post_id, title, url, sent_at) VALUES (?, ?, ?, ?)", [(p.post_id, p.title, p.url, now) for p in posts])
        self.conn.executemany("DELETE FROM concept_pending WHERE post_id = ?", [(p.post_id,) for p in posts])
        self.conn.commit()

    def log_notification(self, kind: str, message: str) -> None:
        self.conn.execute("INSERT INTO notification_history(kind, message, sent_at) VALUES (?, ?, ?)", (kind, message, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def author_tracking_initialized(self) -> bool:
        return self.conn.execute("SELECT value FROM meta WHERE key = 'author_tracking_initialized'").fetchone() is not None

    def newbie_baseline_initialized(self) -> bool:
        marker = self.conn.execute("SELECT value FROM meta WHERE key = 'newbie_baseline_500_initialized'").fetchone() is not None
        if not marker:
            return False
        # Recover automatically from older databases where the marker was
        # written even though the author rows were not committed.
        row = self.conn.execute("SELECT COUNT(*) FROM author_history").fetchone()
        return bool(row and int(row[0]) > 0)

    def replace_newbie_baseline(self, authors: set[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM author_history")
        self.conn.executemany(
            "INSERT OR IGNORE INTO author_history(author_key, author, first_seen) VALUES (?, ?, ?)",
            [(author_key(author), normalize_author_name(author), now) for author in authors if author.strip()],
        )
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('author_tracking_initialized', '1')")
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('newbie_baseline_500_initialized', '1')")
        self.conn.commit()

    def observe_author(self, author: str) -> bool:
        author = normalize_author_name(author)
        if not author:
            return False
        key = author_key(author)
        row = self.conn.execute("SELECT 1 FROM author_history WHERE author_key = ?", (key,)).fetchone()
        if row:
            return False
        # Migrate rows created before nickname normalization was introduced.
        legacy_rows = self.conn.execute("SELECT author FROM author_history").fetchall()
        for (stored_author,) in legacy_rows:
            if author_key(str(stored_author)) == key:
                self.conn.execute(
                    "INSERT OR IGNORE INTO author_history(author_key, author, first_seen) VALUES (?, ?, ?)",
                    (key, author, datetime.now().isoformat(timespec="seconds")),
                )
                self.conn.commit()
                return False
        self.conn.execute("INSERT INTO author_history(author_key, author, first_seen) VALUES (?, ?, ?)", (key, author, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return True

    def author_known(self, author: str) -> bool:
        normalized = normalize_author_name(author)
        if not normalized:
            return True
        key = author_key(normalized)
        return self.conn.execute("SELECT 1 FROM author_history WHERE author_key = ?", (key,)).fetchone() is not None

    def initialize_authors(self, authors: set[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO author_history(author_key, author, first_seen) VALUES (?, ?, ?)", [(author_key(author), normalize_author_name(author), now) for author in authors if author.strip()])
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('author_tracking_initialized', '1')")
        self.conn.commit()

    def log_newbie(self, author: str, kind: str, post_title: str, comment: str, post_url: str) -> None:
        self.conn.execute(
            "INSERT INTO newbie_history(author, kind, post_title, comment, post_url, detected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (author, kind, post_title, comment, post_url, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def is_newbie_author(self, author: str) -> bool:
        """Return whether this author has previously been detected as a newbie."""
        normalized = normalize_author_name(author)
        if not normalized:
            return False
        rows = self.conn.execute("SELECT author FROM newbie_history").fetchall()
        return any(author_key(str(row[0])) == author_key(normalized) for row in rows)


class Monitor:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.board_url = str(config.get("board_url", BOARD_URL)).strip() or BOARD_URL
        parsed_board = urlparse(self.board_url)
        self.board_id = parse_qs(parsed_board.query).get("id", [BOARD_ID])[0]
        separator = "&" if "?" in self.board_url else "?"
        self.concept_board_url = self.board_url + separator + "exception_mode=recommend"
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Referer": self.board_url,
        })
        self.state = State(DB_PATH)
        self.request_gap_seconds = max(1, int(config.get("request_gap_seconds", 5)))
        self._body_summary_cache: dict[str, str] = {}

    def get_html(self, url: str) -> str:
        # Keep all requests to the board at least ``request_gap_seconds`` apart.
        # A keyword scan visits one detail page per recent post, so this prevents bursts.
        global _LAST_SITE_REQUEST_AT
        with _SITE_REQUEST_LOCK:
            wait = self.request_gap_seconds - (time.monotonic() - _LAST_SITE_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
            response = self.http.get(url, timeout=int(self.config["request_timeout_seconds"]))
            _LAST_SITE_REQUEST_AT = time.monotonic()
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def get_comments(self, post: Post) -> list[tuple[str, str, str]]:
        """Load comments through DCInside's AJAX endpoint.

        Comments are rendered by JavaScript on the desktop post page, so they
        are usually absent from the HTML returned by ``get_html(post.url)``.
        The mobile AJAX endpoint returns the same comment markup and includes
        replies, which makes comment keyword monitoring reliable.
        """
        global _LAST_SITE_REQUEST_AT
        endpoint = "https://m.dcinside.com/ajax/response-comment"
        payload = {
            "id": self.board_id,
            "no": post.post_id,
            "cpage": "1",
            "managerskill": "",
            "del_scope": "1",
            "csort": "",
        }
        headers = {
            "Referer": post.url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html, */*; q=0.01",
        }
        with _SITE_REQUEST_LOCK:
            wait = self.request_gap_seconds - (time.monotonic() - _LAST_SITE_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
            response = self.http.post(endpoint, data=payload, headers=headers,
                                      timeout=int(self.config["request_timeout_seconds"]))
            _LAST_SITE_REQUEST_AT = time.monotonic()
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        comments = self.extract_comments(BeautifulSoup(response.text, "html.parser"))
        LOG.debug("댓글 API 수집: post=%s count=%d", post.post_id, len(comments))
        return comments

    def get_board_posts(self) -> list[Post]:
        """Share a recent board-list crawl between topic/title/keyword jobs."""
        global _BOARD_CACHE_AT, _BOARD_CACHE_POSTS
        now = time.monotonic()
        with _BOARD_CACHE_LOCK:
            if _BOARD_CACHE_POSTS is not None and now - _BOARD_CACHE_AT < BOARD_CACHE_SECONDS:
                return list(_BOARD_CACHE_POSTS)
            posts = self.extract_posts(self.get_html(self.board_url))
            _BOARD_CACHE_POSTS = posts
            _BOARD_CACHE_AT = time.monotonic()
            return list(posts)

    def get_recent_posts(self, limit: int) -> list[Post]:
        """Fetch up to 200 recent posts across board-list pages."""
        limit = max(1, min(200, int(limit)))
        posts = self.get_board_posts()
        seen_ids = {post.post_id for post in posts}
        page = 2
        while len(posts) < limit and page <= 20:
            separator = "&" if "?" in self.board_url else "?"
            page_url = f"{self.board_url}{separator}page={page}"
            try:
                page_posts = self.extract_posts(self.get_html(page_url))
            except requests.RequestException as exc:
                LOG.warning("게시글 목록 페이지 %d를 가져오지 못함: %s", page, exc)
                break
            if not page_posts:
                break
            added = 0
            for post in page_posts:
                if post.post_id in seen_ids:
                    continue
                posts.append(post)
                seen_ids.add(post.post_id)
                added += 1
                if len(posts) >= limit:
                    break
            if not added:
                break
            page += 1
        return posts[:limit]

    def get_concept_posts(self) -> list[Post]:
        global _CONCEPT_CACHE_AT, _CONCEPT_CACHE_POSTS
        now = time.monotonic()
        with _CONCEPT_CACHE_LOCK:
            if _CONCEPT_CACHE_POSTS is not None and now - _CONCEPT_CACHE_AT < CONCEPT_CACHE_SECONDS:
                return list(_CONCEPT_CACHE_POSTS)
            posts = self.extract_posts(self.get_html(self.concept_board_url))
            _CONCEPT_CACHE_POSTS = posts
            _CONCEPT_CACHE_AT = time.monotonic()
            return list(posts)

    def send_telegram(self, text: str) -> None:
        token = self.config.get("telegram_bot_token")
        chat_id = self.config.get("telegram_chat_id")
        if not token or not chat_id:
            LOG.warning("Telegram 설정이 없어 메시지를 보내지 못했습니다. config.json의 token/chat_id를 설정하세요.")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Telegram text messages are limited; chunk while retaining complete records.
        for start in range(0, len(text), 3900):
            response = self.http.post(url, json={"chat_id": chat_id, "text": text[start:start + 3900], "disable_web_page_preview": True}, timeout=20)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram 전송 실패: {result}")
        LOG.info("텔레그램 전송 완료 (%d자)\n%s", len(text), text)

    @staticmethod
    def extract_author(container: Tag | None) -> str:
        """Read nickname plus DCInside's anonymous/semianonymous identifier."""
        if not container:
            return ""
        author_node = container.select_one(
            ".gall_writer, .nickname, .nick, .writer, .cmt_nick, .cmt_nickbox, .ginfo-area .nick"
        ) or container
        author = author_node.get_text(" ", strip=True)
        if not author:
            author = str(
                author_node.get("data-nick", "") or container.get("data-nick", "")
            ).strip()
        if "(" in author:
            return normalize_author_name(author)
        anonymous = author.startswith("ㅇㅇ")

        # The identifier has appeared in several attributes over time.
        attrs = ("data-uid", "data-ip", "data-code", "data-info", "data-nick") if anonymous else ("data-ip",)
        candidates: list[str] = []
        nodes = [author_node, container, *container.select("*")]
        for node in nodes:
            for attr in attrs:
                value = str(node.get(attr, "")).strip()
                if value:
                    candidates.append(value)
            # Older markup exposes the IP only as visible text beside ㅇㅇ.
            text = node.get_text(" ", strip=True)
            match = re.search(r"\((\d{1,3}(?:\.\d{1,3}){1,3})\)", text)
            if match:
                candidates.append(match.group(1).strip())
        for value in candidates:
            ip_match = re.search(r"\b\d{1,3}(?:\.\d{1,3}){1,3}\b", value)
            if ip_match:
                return normalize_author_name(f"{author}({ip_match.group(0)})")
            value = value.split("|")[-1].strip()
            if value and value != "ㅇㅇ" and (anonymous or "." in value) and re.fullmatch(r"[A-Za-z0-9_*.-]{3,40}", value):
                return normalize_author_name(f"{author}({value.strip('() ')})")
        return normalize_author_name(author)

    @staticmethod
    def is_recent_post_row(row: Tag, now: datetime | None = None) -> bool:
        """Return whether a board-list row was posted within the last 48 hours."""
        date_node = row.select_one("td.gall_date")
        raw = date_node.get_text(" ", strip=True) if date_node else ""
        if not raw:
            # If the site omits a date, keep the row rather than dropping a
            # potentially valid post because of an HTML variation.
            return True
        current = now or datetime.now()
        posted_at: datetime | None = None
        time_match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if time_match:
            posted_at = current.replace(
                hour=int(time_match.group(1)), minute=int(time_match.group(2)),
                second=0, microsecond=0,
            )
            # A time later than the current clock belongs to the previous day.
            if posted_at > current:
                posted_at -= timedelta(days=1)
        else:
            for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%m.%d", "%m-%d"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                except ValueError:
                    continue
                posted_at = parsed if "%Y" in fmt else parsed.replace(year=current.year)
                if posted_at > current + timedelta(days=1):
                    posted_at = posted_at.replace(year=posted_at.year - 1)
                break
        if posted_at is None:
            return True
        return current - posted_at <= timedelta(hours=48)

    @staticmethod
    def extract_posts(html: str) -> list[Post]:
        soup = BeautifulSoup(html, "html.parser")
        posts: list[Post] = []
        for row in soup.select("tr.ub-content"):
            number = row.get("data-no", "").strip()
            title_link = row.select_one("td.gall_tit a[href]") or row.select_one("a[href*='/board/view/']")
            if not number or not title_link:
                continue
            category_node = row.select_one("td.gall_subject")
            category = category_node.get_text(" ", strip=True) if category_node else ""
            author = Monitor.extract_author(row.select_one("td.gall_writer"))
            # Fixed/notice rows are not part of current-topic frequency or recent scanning.
            if category in {"공지", "AD", "설문"}:
                continue
            if not Monitor.is_recent_post_row(row):
                continue
            href = urljoin(BASE_URL, str(title_link.get("href", "")))
            posts.append(Post(number, title_link.get_text(" ", strip=True), href, category, author))
        return posts

    def summarize_post_body(self, post: Post) -> str:
        if post.post_id in self._body_summary_cache:
            return self._body_summary_cache[post.post_id]
        body = self.state.get_post_body(post.post_id)
        if not body:
            return ""
        body = re.sub(r"\s+", " ", body).strip()
        summary = "\n".join(textwrap.wrap(body, width=80, max_lines=3, placeholder="...")) if body else ""
        self._body_summary_cache[post.post_id] = summary
        return summary

    def format_post_notification(self, post: Post, prefix: str) -> str:
        author_title = f"{post.author} : {post.title}" if post.author else post.title
        body_summary = self.summarize_post_body(post)
        # Keep the summary visually separated from the title and URL block.
        body_line = f"\n\n본문 : {body_summary}" if body_summary else ""
        return f"{prefix} {author_title}{body_line}\n{post.url}"

    @staticmethod
    def topic_related_posts(posts: list[Post]) -> list[Post]:
        """Return the up-to-five titles actually included in a topic alert."""
        tokens: dict[str, int] = {}
        for post in posts:
            for token in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+.-]{1,}|[0-9]{2,}", post.title):
                folded = token.casefold()
                if folded not in STOP_WORDS and len(folded) > 1:
                    tokens[folded] = tokens.get(folded, 0) + 1
        top = sorted(tokens.items(), key=lambda item: (-item[1], item[0]))[:5]
        if not any(count >= 3 for _, count in top):
            return []
        return [post for post in posts if any(word.casefold() in post.title.casefold() for word, _ in top)][:5]

    def title_topic(self, posts: list[Post]) -> str:
        tokens: dict[str, int] = {}
        for post in posts:
            for token in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+.-]{1,}|[0-9]{2,}", post.title):
                folded = token.casefold()
                if folded not in STOP_WORDS and len(folded) > 1:
                    tokens[folded] = tokens.get(folded, 0) + 1
        if not tokens:
            return "제목에서 반복되는 핵심 단어를 찾지 못했습니다."
        top = sorted(tokens.items(), key=lambda item: (-item[1], item[0]))[:5]
        # No meaningful topic is present when no word appears at least three
        # times across the checked titles.  Keep the periodic topic alert,
        # but avoid sending unrelated title examples in that case.
        if not any(count >= 3 for _, count in top):
            return "📊 1페이지 요약/평화로운 상태"
        examples = self.topic_related_posts(posts)
        summary = ", ".join(f"{word}({count})" for word, count in top)
        lines = [f"📊 1페이지 요약/반복 단어: {summary}"]
        if examples:
            related_titles = [self.format_post_notification(p, "📝") for p in examples]
            lines.append(BLOCK_SEPARATOR.join(related_titles))
        lines.append("※ 첫 페이지 제목 빈도 기반 자동 요약")
        return BLOCK_SEPARATOR.join(lines)

    def check_topics(self) -> None:
        if not self.config.get("topic_alert_enabled", True):
            LOG.info("주제 알림이 꺼져 있어 주제 확인을 건너뜁니다.")
            return
        posts = self.get_board_posts()
        if posts:
            self.state.observe_topic_posts(posts)
            pending = self.state.pending_topic_count()
            threshold = int(self.config.get("topic_batch_count", 30))
            if pending < threshold:
                LOG.info("주제 알림 대기 중: 새 글 %d/%d개", pending, threshold)
                return
            message = self.title_topic(posts)
            self.send_telegram(message)
            # Topic alerts also expose these post titles. Claim them in the
            # unified ledger so a later keyword/link alert cannot resend them.
            for post in self.topic_related_posts(posts):
                self.state.claim_post_title_alert(post, "topic")
            self.state.log_notification("topic", message)
            self.state.mark_topic_alerted()
            LOG.info("주제 요약 전송 (%d개 제목 확인)", len(posts))
        else:
            LOG.warning("게시글 목록을 찾지 못했습니다. 사이트 HTML 변경 여부를 확인하세요.")

    def run_startup_notifications(self) -> None:
        """Send the first-run topic and title notifications, ignoring history."""
        if self.state.startup_notification_done():
            # Concept titles are intentionally checked on every application
            # start. Keep this independent from the topic/title notification so
            # a failure in either one cannot suppress the other.
            try:
                self.check_topics()
            finally:
                self.send_startup_concept_titles()
            return
        posts = self.get_recent_posts(int(self.config["recent_posts_to_scan"]))
        if not posts:
            LOG.warning("최초 실행 알림을 위해 게시글 목록을 찾지 못했습니다.")
            return
        self.state.initialize_topic_tracking(posts)
        initial_title_count = max(1, int(self.config.get("new_title_batch_count", 10)))
        initial_title_posts = posts[:initial_title_count]
        try:
            if self.config.get("topic_alert_enabled", True):
                topic_message = self.title_topic(posts)
                self.send_telegram(topic_message)
                for post in self.topic_related_posts(posts):
                    self.state.claim_post_title_alert(post, "topic")
                self.state.log_notification("topic_first_run", topic_message)
            if self.config.get("new_title_alert_enabled", True):
                title_lines = []
                title_lines.extend(self.format_post_notification(post, "🆕") for post in initial_title_posts)
                title_message = BLOCK_SEPARATOR.join(title_lines)
                self.send_telegram(title_message)
                self.state.mark_titles_sent(initial_title_posts)
                self.state.log_notification("new_titles_first_run", title_message)
            else:
                self.state.mark_titles_sent(posts)
            self.state.initialize_title_tracking(posts)
            self.state.mark_startup_notification_done()
        finally:
            # Always attempt the startup concept notification even if another
            # startup message failed.
            self.send_startup_concept_titles()
        LOG.info("최초 실행 주제 및 제목 알림 전송 (제목 %d개, 기준점 %d개)", len(initial_title_posts), len(posts))

    def send_startup_concept_titles(self) -> None:
        """Send the ten most recent concept posts whenever the app starts."""
        if not self.config.get("concept_alert_enabled", True):
            return
        posts = self.get_concept_posts()[:10]
        if not posts:
            LOG.warning("최초 개념글 알림을 위해 개념글 목록을 찾지 못했습니다.")
            return
        lines = [
            self.format_post_notification(post, "👍")
            for post in posts
        ]
        message = BLOCK_SEPARATOR.join(lines)
        self.send_telegram(message)
        self.state.initialize_concept_tracking(posts)
        self.state.mark_concept_titles_sent(posts)
        self.state.log_notification("concept_titles_startup", message)
        LOG.info("프로그램 시작 개념글 %d건 전송", len(posts))

    def scan_new_titles(self) -> None:
        new_title_enabled = bool(self.config.get("new_title_alert_enabled", True))
        newbie_title_enabled = bool(self.config.get("newbie_title_alert_enabled", True))
        if not (new_title_enabled or newbie_title_enabled):
            LOG.info("새 글 모아보기와 뉴비 새 글 모아보기가 모두 꺼져 있어 제목 확인을 건너뜁니다.")
            return
        posts = self.get_recent_posts(int(self.config["recent_posts_to_scan"]))
        if not posts:
            LOG.warning("새 글 제목 목록을 찾지 못했습니다.")
            return
        if not self.state.title_tracking_initialized():
            self.state.initialize_title_tracking(posts)
            LOG.info("새 글 제목 기준점 저장 (%d개)", len(posts))
            return
        self.state.observe_title_candidates(posts)
        new_posts = self.state.pending_titles()
        new_title_threshold = int(self.config.get("new_title_batch_count", 10))
        newbie_title_threshold = int(self.config.get("newbie_title_batch_count", 5))
        lines = []
        rendered = []
        newbie_rendered = []
        newbie_posts = []
        author_tracking_ready = self.state.author_tracking_initialized()
        for post in new_posts:
            author = post.author
            is_new_author = False
            if author and author_tracking_ready:
                is_new_author = not self.state.author_known(author)
                if is_new_author:
                    newbie_posts.append(post)
            display_author = author
            if self.config.get("newbie_alert_enabled", True) and is_new_author:
                display_author = f"👶 {author}"
            post_for_alert = Post(post.post_id, post.title, post.url, post.category, display_author)
            rendered.append(self.format_post_notification(post_for_alert, "🆕"))
            if is_new_author:
                newbie_rendered.append(self.format_post_notification(Post(post.post_id, post.title, post.url, post.category, f"👶 {author}"), "🆕"))
        general_ready = new_title_enabled and len(new_posts) >= new_title_threshold
        newbie_ready = newbie_title_enabled and len(newbie_posts) >= newbie_title_threshold
        if not (general_ready or newbie_ready):
            LOG.info("새 글 모아보기 대기 중: 일반 %d/%d개, 뉴비 %d/%d개", len(new_posts), new_title_threshold, len(newbie_posts), newbie_title_threshold)
            return
        for post in newbie_posts:
            if self.state.observe_author(post.author):
                self.state.log_newbie(post.author, "post", post.title, "", post.url)
        if general_ready:
            lines.extend(rendered)
            message = BLOCK_SEPARATOR.join(lines)
            self.send_telegram(message)
            self.state.log_notification("new_titles", message)
        if newbie_title_enabled and newbie_rendered and (newbie_ready or general_ready):
            newbie_message = BLOCK_SEPARATOR.join(newbie_rendered)
            self.send_telegram(newbie_message)
            self.state.log_notification("newbie_titles", newbie_message)
            LOG.info("뉴비 새 글 모아보기 %d건 전송", len(newbie_posts))
        if general_ready:
            self.state.mark_titles_sent(new_posts)
            LOG.info("새 글 모아보기 %d건 전송", len(new_posts))
        else:
            self.state.mark_titles_sent(newbie_posts)

    def scan_concept_titles(self) -> None:
        if not self.config.get("concept_alert_enabled", True):
            LOG.info("개념글 알림이 꺼져 있어 확인을 건너뜁니다.")
            return
        posts = self.get_concept_posts()[:int(self.config["recent_posts_to_scan"])]
        if not posts:
            LOG.warning("개념글 목록을 찾지 못했습니다.")
            return
        if not self.state.conn.execute("SELECT 1 FROM concept_sent_titles LIMIT 1").fetchone():
            self.state.initialize_concept_tracking(posts)
            LOG.info("개념글 기준점 저장 (%d개)", len(posts))
            return
        self.state.observe_concept_candidates(posts)
        new_posts = self.state.pending_concept_titles()
        threshold = int(self.config.get("concept_batch_count", 3))
        if len(new_posts) < threshold:
            LOG.info("개념글 알림 대기 중: %d/%d개", len(new_posts), threshold)
            return
        lines = []
        rendered = []
        author_tracking_ready = self.state.author_tracking_initialized()
        newbie_enabled = bool(self.config.get("newbie_alert_enabled", True))
        for post in new_posts:
            author = post.author
            if author and author_tracking_ready:
                is_new_author = self.state.observe_author(author)
                if newbie_enabled and is_new_author:
                    self.state.log_newbie(author, "concept", post.title, "", post.url)
                    author = f"👶 {author}"
            post_for_alert = Post(post.post_id, post.title, post.url, post.category, author)
            rendered.append(self.format_post_notification(post_for_alert, "👍"))
        lines.extend(rendered)
        message = BLOCK_SEPARATOR.join(lines)
        self.send_telegram(message)
        self.state.mark_concept_titles_sent(new_posts)
        self.state.log_notification("concept_titles", message)
        LOG.info("개념글 %d건 전송", len(new_posts))

    @staticmethod
    def extract_comments(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
        comments: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        selectors = (
            "li.ub-content", "li.reply", ".reply_box li", ".cmt_list li",
            ".comment_list li", ".reply_list li", "[data-comment-no]",
            "[data-comment-id]", "div.comment", "div.reply",
            # Mobile comment AJAX response format (current DCInside markup).
            "li.comment[no]", "li.reply[no]",
        )
        for node in soup.select(", ".join(selectors)):
            if not isinstance(node, Tag):
                continue
            info_node = node.select_one(
                ".cmt_info[data-no], .comment_info[data-no], [data-no], "
                "li.comment[no], li.reply[no]"
            )
            node_id = str(node.get("id", "")).strip()
            # A comment row must have DCInside's comment metadata.  Without
            # this guard, generic ub-content/list containers from the AJAX
            # response can be mistaken for comments and their whole text is
            # stored as a hash-based fake comment.
            if not info_node and not re.match(r"^(comment|reply)(?:_li|_cnt)_\d+$", node_id):
                continue
            comment_id = str(
                (info_node.get("data-no") if info_node else None)
                or (info_node.get("no") if info_node else None)
                or node.get("data-no") or node.get("data-comment-no")
                or node.get("data-comment-id") or node.get("no") or node.get("id") or ""
            ).strip()
            # Only use selectors known to contain the actual comment body.
            # A generic `.txt` selector also matches DCInside's controls and
            # labels (for example, the comment-tail options), which polluted
            # the stored comment text and broke keyword matching.
            text_node = None
            for candidate in node.select(
                ".usertxt, .ub-word, .reply_text, .cmt_txt, .comment_text, "
                ".comment-content, .comment > .txt, .reply > .txt, "
                ".ginfo-area ~ .txt"
            ):
                candidate_text = candidate.get_text(" ", strip=True)
                if candidate_text:
                    text_node = candidate
                    break
            text = text_node.get_text(" ", strip=True) if text_node else ""
            node_links = [urljoin(BASE_URL, str(a.get("href", "")).strip()) for a in node.select("a[href]") if str(a.get("href", "")).strip().startswith(("http://", "https://"))]
            if not text:
                # Some versions put the text directly in the comment node;
                # remove metadata elements before reading the remaining text.
                clone = BeautifulSoup(str(node), "html.parser")
                for metadata in clone.select(".cmt_info, .comment_info, .cmt_nickbox, .cmt_nick, .nickname, .writer, .date, .comment-tail, .comment_tail, .reply_info, .cmt_btn, .cmt_menu, .reply, .txt, button, script"):
                    metadata.decompose()
                # Do not accept a whole comment row when it only contains
                # metadata or action controls.
                text = clone.get_text(" ", strip=True)
            author = Monitor.extract_author(node)
            if not comment_id and text:
                comment_id = "hash-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
            for link in node_links:
                if link not in text:
                    text = f"{text}\n{link}".strip()
            if comment_id and text and comment_id not in seen:
                comments.append((comment_id, author, text))
                seen.add(comment_id)
        return comments

    @staticmethod
    def extract_body(soup: BeautifulSoup) -> str:
        for selector in (".write_div", ".writing_view_box", ".view_content_wrap", ".gallview_contents"):
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                links = [urljoin(BASE_URL, str(a.get("href", "")).strip()) for a in node.select("a[href]") if str(a.get("href", "")).strip().startswith(("http://", "https://"))]
                for link in links:
                    if link not in text:
                        text = f"{text}\n{link}".strip()
                return text
        return ""

    @staticmethod
    def extract_links(text: str) -> list[str]:
        links: list[str] = []
        for link in re.findall(r"https?://[^\s<>\"']+", text):
            link = link.rstrip(".,;:!?)]}>")
            # Lottery and YouTube links are not treated as link-alert items.
            # Keep this filter shared by both post-body and comment scanning.
            folded_link = link.casefold()
            if "dclottery" in folded_link or "youtube" in folded_link or "youtu.be" in folded_link:
                continue
            if link and link not in links:
                links.append(link)
        return links

    def recent_posts_for_newbie_baseline(self, limit: int = 500) -> list[Post]:
        """Fetch enough list pages to build the initial author baseline."""
        posts = self.get_board_posts()
        seen_ids = {post.post_id for post in posts}
        page = 2
        while len(posts) < limit and page <= 20:
            separator = "&" if "?" in self.board_url else "?"
            url = f"{self.board_url}{separator}page={page}"
            try:
                page_posts = self.extract_posts(self.get_html(url))
            except requests.RequestException as exc:
                LOG.warning("뉴비 기준점 페이지 %d를 가져오지 못함: %s", page, exc)
                break
            if not page_posts:
                break
            added = 0
            for post in page_posts:
                if post.post_id not in seen_ids:
                    posts.append(post)
                    seen_ids.add(post.post_id)
                    added += 1
                    if len(posts) >= limit:
                        break
            if not added:
                break
            page += 1
        return posts[:limit]

    def scan_recent(self) -> None:
        title_keywords = self.config.get("title_keywords", [])
        body_keywords = self.config.get("body_keywords", [])
        comment_keywords = self.config.get("comment_keywords", [])
        link_check_enabled = bool(self.config.get("include_embedded_links", True))
        profanity_enabled = bool(self.config.get("profanity_alert_enabled", False))
        comment_batch_enabled = bool(self.config.get("comment_batch_alert_enabled", False))
        body_check_enabled = bool(body_keywords or link_check_enabled)
        comment_check_enabled = bool(comment_keywords or link_check_enabled or profanity_enabled or comment_batch_enabled)
        if not (title_keywords or body_check_enabled or comment_check_enabled):
            LOG.info("본문·댓글·제목 알림 조건이 없어 상세 크롤링을 건너뜁니다.")
            return
        if not title_keywords and not body_keywords and not comment_keywords:
            LOG.info("키워드가 설정되지 않아 키워드 일치 검사는 건너뛰고 뉴비·링크 검사를 진행합니다.")
        # Revisit the configured recent posts every cycle so new comments on already
        # known posts are still detected. The seen table suppresses duplicates.
        posts = self.get_recent_posts(int(self.config["recent_posts_to_scan"]))
        keyword_matches: list[str] = []
        profanity_matches: list[str] = []
        newbie_comment_authors: set[str] = set()
        sent_any = False
        def flush_matches() -> None:
            nonlocal sent_any
            if keyword_matches:
                message = BLOCK_SEPARATOR.join(keyword_matches)
                self.send_telegram(message)
                self.state.log_notification("keyword", message)
                LOG.info("🔎 키워드 감지 %d건 전송", len(keyword_matches))
                keyword_matches.clear()
                sent_any = True

        def flush_profanity_matches() -> None:
            nonlocal sent_any
            if not profanity_matches:
                return
            message = BLOCK_SEPARATOR.join(profanity_matches)
            self.send_telegram(message)
            self.state.log_notification("profanity_comment", message)
            LOG.info("🤬 욕설 댓글 감지 %d건 전송", len(profanity_matches))
            profanity_matches.clear()
            sent_any = True

        def flush_comment_batch() -> None:
            if not self.config.get("comment_batch_alert_enabled", False):
                return
            threshold = int(self.config.get("comment_batch_count", 50))
            rows = self.state.pending_comment_batch(threshold)
            if len(rows) < threshold:
                LOG.info("댓글 묶음 알림 대기 중: %d/%d개", len(rows), threshold)
                return
            grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
            for _post_id, _comment_id, title, author, comment, post_url in rows:
                key = (title, post_url)
                excerpt = comment if len(comment) <= 500 else comment[:497] + "..."
                grouped.setdefault(key, []).append((author or "작성자 미상", excerpt))
            blocks = [f"💬 수집 댓글 {threshold}개"]
            for (title, post_url), comments_for_post in grouped.items():
                comment_lines = []
                for author, excerpt in comments_for_post:
                    display_author = author
                    if self.config.get("newbie_alert_enabled", True) and (
                        normalize_author_name(author) in newbie_comment_authors
                        or self.state.is_newbie_author(author)
                    ):
                        display_author = f"👶 {author}"
                    comment_lines.append(f"💬 {display_author} : {excerpt}")
                blocks.append(f"📝 {title}\n{post_url}\n\n" + BLOCK_SEPARATOR.join(comment_lines))
            message = BLOCK_SEPARATOR.join(blocks)
            self.send_telegram(message)
            self.state.mark_comment_batch_sent(rows)
            self.state.log_notification("comment_batch", message)
            LOG.info("댓글 %d개 묶음 전송 완료", threshold)
        if self.config.get("newbie_alert_enabled", True) and not self.state.newbie_baseline_initialized():
            baseline_posts = self.recent_posts_for_newbie_baseline(500)
            baseline_authors = {post.author for post in baseline_posts if post.author}
            self.state.replace_newbie_baseline(baseline_authors)
            LOG.info("뉴비 기준점 저장: 최근 게시글 %d개, 작성자 %d명", len(baseline_posts), len(baseline_authors))
        author_ready = self.state.author_tracking_initialized() if self.config.get("newbie_alert_enabled", True) else True
        authors_to_seed: set[str] = set()
        for post in posts:
            if post.author:
                if not author_ready:
                    authors_to_seed.add(post.author)
            soup = None
            post_content_seen = self.state.has_seen(f"post-content-check:{post.post_id}")
            if body_check_enabled and not post_content_seen:
                try:
                    html = self.get_html(post.url)
                except requests.RequestException as exc:
                    LOG.warning("게시글 %s를 가져오지 못함: %s", post.post_id, exc)
                else:
                    soup = BeautifulSoup(html, "html.parser")
                    body = self.extract_body(soup)
                    if body:
                        self.state.save_post_body(post.post_id, body)
                    # 게시글 본문은 최초 성공 시 한 번만 검사한다.
                    matched_title_words = [word for word in title_keywords if word.casefold() in post.title.casefold()]
                    embedded_post_links = self.extract_links(body)
                    matched_body_words = [word for word in body_keywords if word.casefold() in body.casefold()]
                    link_check_new = self.state.is_new(f"link-check:post:{post.post_id}") if link_check_enabled else False
                    post_link_trigger = bool(link_check_new and embedded_post_links)
                    matched_post_words = matched_title_words + matched_body_words
                    # Title alerts use the global post-title ledger. Body and
                    # link alerts have their own seen keys because their
                    # conditions cannot be determined from the title page.
                    title_alert_new = bool(matched_title_words and self.state.claim_post_title_alert(post, "keyword"))
                    body_alert_new = bool(matched_body_words and self.state.is_new(f"post-body-alert:{post.post_id}"))
                    link_alert_new = bool(post_link_trigger and self.state.is_new(f"post-link-alert:{post.post_id}"))
                    post_is_new = title_alert_new or body_alert_new or link_alert_new
                    if post_is_new:
                        displayed_title_words = matched_title_words if title_alert_new else []
                        displayed_body_words = matched_body_words if body_alert_new else []
                        highlighted_title = highlight_keywords(post.title, displayed_title_words)
                        display_title = f"{post.author} : {highlighted_title}" if post.author else highlighted_title
                        reasons = []
                        if displayed_title_words:
                            reasons.append("제목: " + ", ".join(displayed_title_words))
                        if displayed_body_words:
                            reasons.append("본문: " + ", ".join(displayed_body_words))
                        if reasons:
                            body_line = ""
                            if displayed_body_words and body:
                                body_preview = re.sub(r"\s+", " ", body).strip()
                                if len(body_preview) > 300:
                                    body_preview = body_preview[:297] + "..."
                                body_line = f"\n\n본문 : {highlight_keywords(body_preview, displayed_body_words)}"
                            message = f"📝 {display_title}{body_line}\n{post.url}"
                        else:
                            message = f"🔗 {display_title}\n{post.url}"
                        if embedded_post_links:
                            message += "\n\n첨부 링크:\n" + "\n".join(embedded_post_links)
                        keyword_matches.append(message)
                    self.state.is_new(f"post-content-check:{post.post_id}")
            elif not body_check_enabled and not post_content_seen:
                # Title-only keyword monitoring does not need a detail-page request.
                matched_title_words = [word for word in title_keywords if word.casefold() in post.title.casefold()]
                if matched_title_words and self.state.claim_post_title_alert(post, "keyword"):
                    highlighted_title = highlight_keywords(post.title, matched_title_words)
                    display_title = f"{post.author} : {highlighted_title}" if post.author else highlighted_title
                    keyword_matches.append(f"📝 {display_title}\n{post.url}")
            # Desktop comments are populated asynchronously.  Always query the
            # AJAX endpoint, falling back to embedded markup for older pages.
            if not comment_check_enabled:
                continue
            LOG.info("댓글 수집 시작: post=%s 제목=%s", post.post_id, post.title)
            try:
                comments = self.get_comments(post)
            except requests.RequestException as exc:
                LOG.warning("댓글 API를 가져오지 못함 (%s): %s", post.post_id, exc)
                comments = []
            if not comments:
                comments = self.extract_comments(soup) if soup is not None else []
            self.state.record_comments(post, comments[:100])
            LOG.info("댓글 수집 완료: post=%s 수집=%d건 저장대상=%d건", post.post_id, len(comments), min(len(comments), 100))
            for comment_id, comment_author, comment in comments[:100]:
                excerpt = comment if len(comment) <= 240 else comment[:237] + "..."
                LOG.info("    댓글 %s [%s]: %s", comment_id, comment_author or "작성자 미상", excerpt.replace("\n", " "))
            for comment_id, comment_author, comment in comments[:100]:
                comment_new_author = False
                if comment_author:
                    if not author_ready:
                        authors_to_seed.add(comment_author)
                    elif self.config.get("newbie_alert_enabled", True):
                        comment_new_author = not self.state.author_known(comment_author)
                        if comment_new_author:
                            newbie_comment_authors.add(normalize_author_name(comment_author))
                            self.state.log_newbie(comment_author, "comment", post.title, comment, post.url)
                        self.state.observe_author(comment_author)
                    else:
                        self.state.observe_author(comment_author)
                display_comment_author = comment_author or "작성자 미상"
                if comment_new_author or (
                    self.config.get("newbie_alert_enabled", True)
                    and self.state.is_newbie_author(comment_author)
                ):
                    display_comment_author = f"👶 {display_comment_author}"
                matched_comment_words = [word for word in comment_keywords if word.casefold() in comment.casefold()]
                embedded_comment_links = self.extract_links(comment)
                if (
                    self.config.get("profanity_alert_enabled", False)
                    and contains_profanity(comment)
                    and self.state.is_new(f"profanity-comment:{post.post_id}:{comment_id}")
                ):
                    display_title = f"{post.author} : {post.title}" if post.author else post.title
                    profanity_matches.append(
                        f"🤬 {display_comment_author} : {comment}"
                        f"\n\n📝 {display_title}\n{post.url}"
                    )
                comment_link_check_new = self.state.is_new(f"link-check:comment:{post.post_id}:{comment_id}") if link_check_enabled else False
                comment_link_trigger = bool(comment_link_check_new and embedded_comment_links)
                comment_is_new = (matched_comment_words and self.state.is_new(f"comment:{post.post_id}:{comment_id}")) or (comment_link_trigger and self.state.is_new(f"comment-link:{post.post_id}:{comment_id}"))
                if comment_is_new:
                    excerpt = comment if len(comment) <= 220 else comment[:217] + "..."
                    display_title = f"{post.author} : {post.title}" if post.author else post.title
                    if matched_comment_words:
                        excerpt = highlight_keywords(excerpt, matched_comment_words)
                        highlighted_title = highlight_keywords(post.title, [])
                        display_title = f"{post.author} : {highlighted_title}" if post.author else highlighted_title
                        # 댓글이 핵심 내용이므로 댓글을 먼저 보여주고,
                        # 아래에 작성자명이 포함된 원문 제목과 링크를 표시한다.
                        message = (
                            f"💬 {display_comment_author} : {excerpt}"
                            f"\n\n📝 {display_title}\n{post.url}"
                        )
                    else:
                        # Link alerts keep the complete comment text.  The
                        # extracted URLs are also listed separately below.
                        message = f"🔗 💬 {display_comment_author} : {comment}\n\n📝 {display_title}\n{post.url}"
                    if embedded_comment_links:
                        message += "\n\n첨부 링크:\n" + "\n".join(embedded_comment_links)
                    keyword_matches.append(message)
            # Do not wait for all 100 posts and their comments before sending.
            # The 10-second request spacing can make a full pass take a while.
            flush_matches()
            flush_profanity_matches()
            flush_comment_batch()
        if not author_ready:
            self.state.initialize_authors(authors_to_seed)
            LOG.info("뉴비 알림 기준점 저장 (%d명)", len(authors_to_seed))
        flush_matches()
        flush_profanity_matches()
        flush_comment_batch()
        if not sent_any:
            LOG.info("새 키워드 일치 항목 없음 (최근 게시글 %d개 확인)", len(posts))

    def close(self) -> None:
        self.state.close()
        self.http.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    monitor = Monitor(config)
    topic_seconds = int(config["topic_interval_minutes"]) * 60
    keyword_seconds = int(config["keyword_interval_minutes"]) * 60
    next_topic = 0.0
    next_keyword = 0.0
    LOG.info("모니터링 시작: 주제 %d분, 키워드 %d분", topic_seconds // 60, keyword_seconds // 60)
    try:
        while True:
            now = time.monotonic()
            if now >= next_topic:
                try:
                    monitor.check_topics()
                except Exception:
                    LOG.exception("주제 확인 중 오류")
                next_topic = time.monotonic() + topic_seconds
            if now >= next_keyword:
                try:
                    monitor.scan_recent()
                except Exception:
                    LOG.exception("키워드 확인 중 오류")
                next_keyword = time.monotonic() + keyword_seconds
            time.sleep(min(5, max(1, min(next_topic, next_keyword) - time.monotonic())))
    except KeyboardInterrupt:
        LOG.info("사용자 요청으로 종료합니다.")
    finally:
        monitor.close()


if __name__ == "__main__":
    main()
