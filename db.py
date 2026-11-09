"""
MySQL persistence via aiomysql.

Every function is a no-op returning a safe default when the pool is down, so a
database outage never stops a report run.
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import aiomysql

import config

log = logging.getLogger("db")

_pool = None
_lock = asyncio.Lock()
_ready = False

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username VARCHAR(64) NULL,
        first_name VARCHAR(128) NULL,
        created_at DATETIME NOT NULL,
        last_seen DATETIME NOT NULL,
        total_reports INT NOT NULL DEFAULT 0,
        is_banned TINYINT(1) NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        number VARCHAR(20) NOT NULL,
        total INT NOT NULL,
        done INT NOT NULL DEFAULT 0,
        success INT NOT NULL DEFAULT 0,
        failed INT NOT NULL DEFAULT 0,
        status ENUM('queued','running','done','cancelled','failed') NOT NULL DEFAULT 'queued',
        reason VARCHAR(191) NULL,
        created_at DATETIME NOT NULL,
        started_at DATETIME NULL,
        finished_at DATETIME NULL,
        INDEX idx_user (user_id, created_at),
        INDEX idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reports (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        job_id BIGINT NOT NULL,
        number VARCHAR(20) NOT NULL,
        success TINYINT(1) NOT NULL,
        reason VARCHAR(191) NULL,
        created_at DATETIME NOT NULL,
        INDEX idx_job (job_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS login_sessions (
        id TINYINT PRIMARY KEY,
        linked TINYINT(1) NOT NULL DEFAULT 0,
        number VARCHAR(20) NULL,
        updated_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def enabled() -> bool:
    return _ready and _pool is not None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _params(url: str) -> dict:
    url = re.sub(r"^(mysql|mariadb)(\+\w+)?://", "mysql://", url.strip())
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    params = {
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or 3306,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "db": (parts.path or "/").lstrip("/") or None,
        "charset": "utf8mb4",
        "autocommit": True,
        "maxsize": max(1, config.DB_POOL_SIZE),
        "connect_timeout": 15,
    }
    mode = (query.get("ssl-mode") or query.get("sslmode") or [""])[0].upper()
    plain_ssl = (query.get("ssl") or [""])[0].lower() in {"true", "1"}
    if mode in {"REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"} or plain_ssl:
        ctx = ssl.create_default_context()
        if mode == "REQUIRED":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        params["ssl"] = ctx
    return params


async def _run(sql: str, args=None) -> int:
    if not enabled():
        return 0
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args or ())
                return cur.lastrowid or cur.rowcount or 0
    except Exception as exc:
        log.warning("db write failed: %s", exc)
        return 0


async def _fetch(sql: str, args=None) -> list[dict]:
    if not enabled():
        return []
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args or ())
                return list(await cur.fetchall())
    except Exception as exc:
        log.warning("db read failed: %s", exc)
        return []


async def init() -> None:
    global _pool, _ready
    if not config.MYSQL_URL:
        log.warning("MYSQL_URL not set — running without a database")
        return
    async with _lock:
        if _pool is not None:
            return
        params = _params(config.MYSQL_URL)
        for attempt in (1, 2, 3):
            try:
                _pool = await aiomysql.create_pool(**params)
                break
            except Exception as exc:
                log.warning("db connect attempt %s failed: %s", attempt, exc)
                await asyncio.sleep(3 * attempt)
        if _pool is None:
            log.error("database unreachable — continuing without persistence")
            return
        for statement in SCHEMA:
            try:
                async with _pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(statement)
            except Exception as exc:
                log.error("schema statement failed: %s", exc)
        _ready = True
        log.info("database ready on %s", params["host"])


async def close() -> None:
    global _pool, _ready
    _ready = False
    if _pool is not None:
        try:
            _pool.close()
            await _pool.wait_closed()
        except Exception:
            pass
        _pool = None


# ------------------------------------------------------------------ users
async def upsert_user(user_id: int, username, first_name) -> None:
    now = _now()
    await _run(
        "INSERT INTO users (user_id, username, first_name, created_at, last_seen) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "username=COALESCE(VALUES(username), username), "
        "first_name=COALESCE(VALUES(first_name), first_name), "
        "last_seen=VALUES(last_seen)",
        (user_id, username, first_name, now, now),
    )


async def add_user_reports(user_id: int, count: int) -> None:
    await _run("UPDATE users SET total_reports = total_reports + %s WHERE user_id=%s", (count, user_id))


async def is_banned(user_id: int) -> bool:
    rows = await _fetch("SELECT is_banned FROM users WHERE user_id=%s", (user_id,))
    return bool(rows and rows[0]["is_banned"])


async def set_banned(user_id: int, flag: bool) -> None:
    await _run("UPDATE users SET is_banned=%s WHERE user_id=%s", (1 if flag else 0, user_id))


# ------------------------------------------------------------------- jobs
async def create_job(chat_id: int, user_id: int, number: str, total: int) -> int:
    return await _run(
        "INSERT INTO jobs (chat_id, user_id, number, total, status, created_at) "
        "VALUES (%s,%s,%s,%s,'queued',%s)",
        (chat_id, user_id, number, total, _now()),
    )


async def mark_running(job_id: int) -> None:
    await _run("UPDATE jobs SET status='running', started_at=%s WHERE id=%s", (_now(), job_id))


async def update_job(job_id: int, done: int, success: int, failed: int) -> None:
    await _run("UPDATE jobs SET done=%s, success=%s, failed=%s WHERE id=%s", (done, success, failed, job_id))


async def finish_job(job_id: int, status: str, reason=None) -> None:
    await _run(
        "UPDATE jobs SET status=%s, reason=%s, finished_at=%s WHERE id=%s",
        (status, (reason or None) and reason[:191], _now(), job_id),
    )


async def log_report(job_id: int, number: str, success: bool, reason=None) -> None:
    await _run(
        "INSERT INTO reports (job_id, number, success, reason, created_at) VALUES (%s,%s,%s,%s,%s)",
        (job_id, number, 1 if success else 0, (reason or None) and reason[:191], _now()),
    )


async def daily_count(user_id: int) -> int:
    rows = await _fetch(
        "SELECT COALESCE(SUM(success),0) AS n FROM jobs "
        "WHERE user_id=%s AND created_at >= CURDATE()",
        (user_id,),
    )
    return int(rows[0]["n"]) if rows else 0


async def recent_jobs(user_id: int, limit: int = 10) -> list[dict]:
    return await _fetch(
        "SELECT number, total, success, failed, status, created_at FROM jobs "
        "WHERE user_id=%s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )


async def abandon_stale_jobs() -> None:
    """Free instances get killed without warning — close out anything left open."""
    await _run(
        "UPDATE jobs SET status='failed', reason='server restarted', finished_at=%s "
        "WHERE status IN ('queued','running')",
        (_now(),),
    )


# ------------------------------------------------------------------ stats
async def totals() -> dict:
    rows = await _fetch(
        "SELECT (SELECT COUNT(*) FROM users) AS users, "
        "(SELECT COUNT(*) FROM jobs) AS jobs, "
        "(SELECT COALESCE(SUM(success),0) FROM jobs) AS success, "
        "(SELECT COUNT(*) FROM jobs WHERE status='running') AS running"
    )
    return rows[0] if rows else {"users": 0, "jobs": 0, "success": 0, "running": 0}


async def top_users(limit: int = 5) -> list[dict]:
    return await _fetch(
        "SELECT user_id, username, total_reports FROM users "
        "ORDER BY total_reports DESC LIMIT %s",
        (limit,),
    )


# ------------------------------------------------------------------ login
async def set_linked(linked: bool, number=None) -> None:
    await _run(
        "INSERT INTO login_sessions (id, linked, number, updated_at) VALUES (1,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE linked=VALUES(linked), number=VALUES(number), "
        "updated_at=VALUES(updated_at)",
        (1 if linked else 0, number, _now()),
    )


async def get_linked():
    rows = await _fetch("SELECT linked, number FROM login_sessions WHERE id=1")
    if not rows:
        return None, None
    return bool(rows[0]["linked"]), rows[0]["number"]