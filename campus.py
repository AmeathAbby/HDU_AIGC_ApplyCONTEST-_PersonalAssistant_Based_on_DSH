"""Persistent campus schedule, notices, and delivery queue."""

import csv
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from timetable import Timetables

SHANGHAI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts INTEGER NOT NULL,
  minutes INTEGER NOT NULL, location TEXT NOT NULL, source TEXT NOT NULL,
  kind TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
  UNIQUE(kind, title, starts, location)
);
CREATE TABLE IF NOT EXISTS drafts (
  code TEXT PRIMARY KEY, payload TEXT NOT NULL, expires INTEGER NOT NULL,
  event_id INTEGER REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  source TEXT NOT NULL, published TEXT NOT NULL, discovered INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery (
  key TEXT PRIMARY KEY, text TEXT NOT NULL, expires INTEGER NOT NULL,
  created INTEGER NOT NULL, attempt_after INTEGER NOT NULL, state TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT ''
);
"""


def now():
    return datetime.now(SHANGHAI)


def stamp(value):
    return int(value.timestamp())


def local(value):
    return datetime.fromtimestamp(value, SHANGHAI)


def parse_start(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
        raise ValueError("时间格式应为 YYYY-MM-DD HH:MM（北京时间）。")
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    if not path.exists():
        template = (ROOT / "config.example.json").read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as file:
                file.write(template)
        except FileExistsError:
            pass
    options = json.loads(path.read_text(encoding="utf-8-sig"))
    options["database"] = (path.parent / options.get("database", "data/campus.sqlite3")).resolve()
    options["digest_at"] = datetime.strptime(options.get("digest_at", "07:30"), "%H:%M").time()
    for key, default in {"poll_seconds": 5, "sync_minutes": 60}.items():
        value = options.get(key, default)
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} 必须是正整数。")
        options[key] = value
    for place in options.get("weather_locations", []):
        if not place.get("name") or not re.fullmatch(r"[0-9]{9}", str(place.get("city_code", ""))):
            raise ValueError("天气地点需要名称和中国天气网的九位 city_code。")
    return options


class Campus:
    def __init__(self, path, config=None):
        self.path = Path(path)
        self.timetables = Timetables(config or {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def draft(self, title, starts_at, minutes, location="", notice_id=None, at=None):
        at = at or now()
        title, location = title.strip(), location.strip()
        starts = parse_start(starts_at)
        if not title or len(title) > 100 or len(location) > 100:
            raise ValueError("请输入 1 至 100 字的标题，地点上限为 100 字。")
        if type(minutes) is not int or not 0 <= minutes <= 10080:
            raise ValueError("提前量应为 0 至 10080 分钟，0 表示开始时提醒。")
        if starts - timedelta(minutes=minutes) <= at:
            raise ValueError("该设置的提醒时间已到或已过，请调整开始时间或提前量。")
        source = self.notice(notice_id)["url"] if notice_id is not None else ""
        code = secrets.token_hex(5).upper()
        payload = {
            "title": title, "starts": stamp(starts), "minutes": minutes,
            "location": location, "source": source,
        }
        with self.db() as db:
            db.execute("DELETE FROM drafts WHERE expires <= ? AND event_id IS NULL", (stamp(at),))
            db.execute("INSERT INTO drafts VALUES (?,?,?,NULL)",
                       (code, json.dumps(payload, ensure_ascii=False), stamp(at + timedelta(minutes=15))))
        return {
            "标题": title, "开始": starts_at, "提醒": (starts - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"),
            "地点": location, "原文": source, "确认口令": f"确认 {code}", "有效分钟": 15,
        }

    def confirm(self, message, at=None):
        at = at or now()
        match = re.fullmatch(r"确认\s+([0-9a-fA-F]{10})", message.strip())
        if not match:
            raise ValueError("请使用草稿返回的完整确认口令。")
        code = match[1].upper()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            draft = db.execute("SELECT * FROM drafts WHERE code=?", (code,)).fetchone()
            if draft and draft["event_id"] is not None:
                return {"状态": "该口令已处理", "日程编号": draft["event_id"]}
            if not draft or draft["expires"] <= stamp(at):
                raise ValueError("草稿不存在或已过期，请重新创建。")
            event = json.loads(draft["payload"])
            if event["starts"] - 60 * event["minutes"] <= stamp(at):
                raise ValueError("提醒时间已过，请重新创建。")
            existing = db.execute(
                "SELECT id, cancelled FROM events WHERE kind='event' AND title=? AND starts=? AND location=?",
                (event["title"], event["starts"], event["location"]),
            ).fetchone()
            if existing and not existing["cancelled"]:
                event_id, status = existing["id"], "已存在"
            elif existing:
                event_id, status = existing["id"], "已恢复"
                db.execute("UPDATE events SET cancelled=0, minutes=?, source=? WHERE id=?",
                           (event["minutes"], event["source"], event_id))
                db.execute("DELETE FROM delivery WHERE key=?", (f"event:{event_id}",))
            else:
                event_id = db.execute(
                    "INSERT INTO events(title,starts,minutes,location,source,kind) "
                    "VALUES (:title,:starts,:minutes,:location,:source,'event')", event,
                ).lastrowid
                status = "已保存"
            db.execute("UPDATE drafts SET event_id=? WHERE code=?", (event_id, code))
        return {"状态": status, "日程编号": event_id, "标题": event["title"],
                "开始": local(event["starts"]).strftime("%Y-%m-%d %H:%M")}

    def cancel(self, message):
        match = re.fullmatch(r"确认取消\s+([0-9]{1,10})", message.strip())
        if not match:
            raise ValueError("请使用「确认取消 日程编号」。")
        event_id = int(match[1])
        with self.db() as db:
            changed = db.execute("UPDATE events SET cancelled=1 WHERE kind='event' AND id=? AND cancelled=0",
                                 (event_id,)).rowcount
            db.execute("UPDATE delivery SET state='cancelled' WHERE key=? AND state='queued'",
                       (f"event:{event_id}",))
        return {"日程编号": event_id, "状态": "已取消" if changed else "日程不存在或已取消"}

    def import_classes(self, path):
        rows = []
        with Path(path).open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if not {"title", "date", "time", "location"}.issubset(reader.fieldnames or []):
                raise ValueError("课表需要 title,date,time,location 四列。")
            for i, row in enumerate(reader, 2):
                try:
                    title = row["title"].strip()
                    starts = parse_start(f"{row['date'].strip()} {row['time'].strip()}")
                    minutes = int(row.get("minutes") or 15)
                    if not title or not 0 <= minutes <= 1440:
                        raise ValueError("课程名称不能为空，提前量须为 0 至 1440 分钟。")
                    rows.append((title, stamp(starts), minutes, row["location"].strip()))
                except (ValueError, AttributeError) as exc:
                    raise ValueError(f"课表第 {i} 行：{exc}") from exc
        with self.db() as db:
            previous = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO events(title,starts,minutes,location,source,kind) VALUES (?,?,?,?,'','class')", rows,
            )
            return db.total_changes - previous

    def agenda(self, day, *, include_timetables=True):
        start = parse_start(f"{day} 00:00")
        with self.db() as db:
            rows = db.execute("SELECT * FROM events WHERE cancelled=0 AND starts>=? AND starts<? ORDER BY starts,id",
                              (stamp(start), stamp(start + timedelta(days=1)))).fetchall()
        agenda = [{"id": row["id"], "类型": "课程" if row["kind"] == "class" else "日程",
                 "标题": row["title"], "时间": local(row["starts"]).strftime("%H:%M"),
                 "地点": row["location"], "提前分钟": row["minutes"], "原文": row["source"]} for row in rows]
        if include_timetables:
            agenda.extend(self.timetables.agenda(date.fromisoformat(day)))
        return sorted(agenda, key=lambda entry: entry["时间"])

    def store_notices(self, items, at=None):
        at = at or now()
        with self.db() as db:
            db.executemany("INSERT INTO notices(url,title,source,published,discovered) VALUES (?,?,?,?,?) "
                           "ON CONFLICT(url) DO UPDATE SET title=excluded.title,source=excluded.source",
                           [(item["url"], item["title"], item["source"], item["published"], stamp(at)) for item in items])

    def notices(self, limit=10):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,url,title,source,published FROM notices ORDER BY published DESC,id DESC LIMIT ?",
                (max(1, min(30, limit)),),
            )]

    def notice(self, notice_id):
        with self.db() as db:
            row = db.execute("SELECT id,url,title,source,published FROM notices WHERE id=?", (notice_id,)).fetchone()
        if row is None:
            raise ValueError("找不到该通知编号，请先查询最近通知。")
        return dict(row)

    def digest(self, at, problems=(), weather=()):
        day = at.date().isoformat()
        previous_day = (at - timedelta(days=1)).date().isoformat()
        with self.db() as db:
            last = db.execute("SELECT max(created) FROM delivery WHERE key LIKE 'digest:%' AND state='sent'").fetchone()[0]
            notices = db.execute(
                "SELECT * FROM notices WHERE published>=? AND published<=? AND discovered>? "
                "ORDER BY published DESC,id DESC LIMIT 5",
                (previous_day if last is None else "0000-01-01", day, last if last is not None else -1),
            ).fetchall()
        lines = [f"今日早报 · {day}"]
        if weather:
            lines += ["", "今日天气", *weather]
        lines += ["", "今日课程与日程"]
        agenda = self.agenda(day, include_timetables=False)
        lines.extend(f"{entry['时间']} {entry['标题']} {entry['地点']}".strip() for entry in agenda)
        if not agenda:
            lines.append("今天暂无额外日程。" if self.timetables.plans else "今天暂无课程或日程。")
        lines += ["", "近期校园通知"]
        for row in notices:
            lines += [f"[N{row['id']}] {row['source']}｜{row['title']}", row["url"]]
        if not notices:
            lines.append("暂未收录新通知。")
        if problems:
            lines.append("部分栏目同步失败，已保留此前的通知。")
        if notices:
            lines.append("回复「把 N编号加到日程」可查看原文并生成草稿。")
        return "\n".join(lines)

    def enqueue(self, at, digest_at, problems=(), *, include_digest=True, weather=()):
        timestamp = stamp(at)
        day_start = at.replace(hour=digest_at.hour, minute=digest_at.minute, second=0, microsecond=0)
        if include_digest and day_start <= at < day_start + timedelta(hours=3):
            key = f"digest:{at.date()}"
            with self.db() as db:
                exists = db.execute("SELECT 1 FROM delivery WHERE key=?", (key,)).fetchone()
            if not exists:
                self.queue(key, self.digest(at, problems, weather), timestamp, stamp(day_start + timedelta(hours=3)))
        if day_start <= at < day_start + timedelta(hours=3):
            for key, message in self.timetables.daily_messages(at.date()):
                self.queue(key, message, timestamp, stamp(day_start + timedelta(hours=3)))
        for key, message, expires in self.timetables.reminders(at):
            self.queue(key, message, timestamp, expires)
        with self.db() as db:
            due = db.execute(
                "SELECT * FROM events WHERE cancelled=0 AND starts-60*minutes<=? "
                "AND starts-60*minutes>=? AND (starts>? OR (minutes=0 AND starts>=?))",
                (timestamp, timestamp - 600, timestamp, timestamp - 60),
            ).fetchall()
        for row in due:
            label = "课前提醒" if row["kind"] == "class" else "日程提醒"
            text = f"{label}｜{row['title']}\n{local(row['starts']):%m-%d %H:%M}"
            if row["location"]:
                text += f" · {row['location']}"
            if row["source"]:
                text += f"\n{row['source']}"
            expires = min(row["starts"] if row["minutes"] else row["starts"] + 60,
                          row["starts"] - 60 * row["minutes"] + 601)
            self.queue(f"event:{row['id']}", text, timestamp, expires)

    def queue(self, key, text, timestamp, expires):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO delivery VALUES (?,?,?,?,?,'queued','')",
                       (key, text, expires, timestamp, timestamp))

    def claim(self, timestamp):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE delivery SET state='expired' WHERE state='queued' AND expires<=?", (timestamp,))
            db.execute(
                "UPDATE delivery SET state='cancelled' WHERE state='queued' AND key IN "
                "(SELECT 'event:' || id FROM events WHERE cancelled=1)"
            )
            row = db.execute(
                "SELECT key,text FROM delivery WHERE state='queued' AND attempt_after<=? "
                "ORDER BY CASE WHEN key LIKE 'event:%' OR key LIKE 'class:%' THEN 0 ELSE 1 END, "
                "created,key LIMIT 1", (timestamp,),
            ).fetchone()
            if row:
                db.execute("UPDATE delivery SET state='sending' WHERE key=?", (row["key"],))
            return row

    def result(self, key, state, error="", retry_after=0):
        with self.db() as db:
            db.execute("UPDATE delivery SET state=?,error=?,attempt_after=? WHERE key=? AND state='sending'",
                       (state, error, retry_after, key))

    def delivery_status(self, limit=15):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT key,state,error FROM delivery ORDER BY created DESC,key LIMIT ?", (limit,),
            )]
