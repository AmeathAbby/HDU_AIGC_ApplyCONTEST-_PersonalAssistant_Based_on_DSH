"""Windows-friendly command line entry point."""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from campus import Campus, DEFAULT_CONFIG, ROOT, load_config, now
from notices import sync
from weather import forecasts
from worker import Sender, discover, drain, run


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def demo():
    """Use an isolated database and a simulated clock; never contact a platform."""
    at = now().replace(hour=7, minute=30, second=0, microsecond=0)
    day = at.date().isoformat()
    with TemporaryDirectory() as temp:
        path = Path(temp)
        campus = Campus(path / "demo.sqlite3")
        with (path / "class.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerows([["title", "date", "time", "location", "minutes"],
                              ["通信原理（模拟课程）", day, "08:00", "教学楼 A101", 15]])
        campus.import_classes(path / "class.csv")
        campus.store_notices([{
            "title": "模拟通知｜提交通信实验报告", "source": "演示数据",
            "url": "https://example.invalid/demo-notice/1", "published": day,
        }], at)
        config = {"mode": "console", "digest_at": at.time()}
        sender = Sender(config)
        campus.enqueue(at, config["digest_at"])
        drain(campus, sender, lambda: at)

        print("\n模拟用户：把 N1 加到日程，今天 09:00 截止，提前 10 分钟提醒。")
        draft = campus.draft("提交通信实验报告", f"{day} 09:00", 10, notice_id=1, at=at)
        show(draft)
        print(f"模拟用户：{draft['确认口令']}")
        show(campus.confirm(draft["确认口令"], at=at))

        for delay in (15, 80, 81):
            later = at + timedelta(minutes=delay)
            print(f"\n模拟时钟：{later:%H:%M}")
            campus.enqueue(later, config["digest_at"])
            drain(campus, sender, lambda: later)
        show(campus.delivery_status())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "run", "sync", "status", "discover", "mcp-config", "test-send"):
        subcommands.add_parser(name)
    subcommands.add_parser("import-classes").add_argument("csv_file")
    subcommands.add_parser("agenda").add_argument("day", nargs="?", default=now().date().isoformat())
    subcommands.add_parser("preview").add_argument("day", nargs="?", default=now().date().isoformat())
    args = parser.parse_args()
    if args.command == "demo":
        demo()
        return 0
    if args.command == "discover":
        address = discover()
        print(address or "未找到投递接口。请打开 Harness 并确认 DSH-IM 已加载。")
        return 0 if address else 1
    if args.command == "mcp-config":
        show({"mcpServers": {"hdu_campus": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(ROOT / "campus_mcp.py"), "--config", str(Path(args.config).resolve())],
        }}})
        return 0
    config = load_config(args.config)
    campus = Campus(config["database"], config)
    if args.command == "run":
        run(campus, config)
    elif args.command == "sync":
        result = sync(campus, config.get("sources", []))
        show(result)
        return 1 if result["失败"] else 0
    elif args.command == "status":
        show(campus.delivery_status())
    elif args.command == "import-classes":
        show({"新增课程": campus.import_classes(args.csv_file)})
    elif args.command == "agenda":
        show(campus.agenda(args.day))
    elif args.command == "preview":
        day = date.fromisoformat(args.day)
        at = datetime.combine(day, config["digest_at"], now().tzinfo)
        print(campus.digest(at, weather=forecasts(config.get("weather_locations", []), day)))
        for _, body in campus.timetables.daily_messages(day):
            print(f"\n{body}")
    elif args.command == "test-send":
        sender = Sender(config)
        state, error = sender.send("杭电校园助手｜主动投递测试")
        show({"模式": sender.mode, "状态": state, "原因": error})
        return 0 if state == "sent" else 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
