"""Expand configurable teaching weeks into classes and reminder messages."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta

WEEK = re.compile(r"(\d+)(?:-(\d+))?(?:\((单|双)\))?")


def teaching_weeks(value):
    weeks = set()
    for part in value.split(","):
        match = WEEK.fullmatch(part.strip())
        if not match:
            raise ValueError(f"无效的课程周次：{part}")
        first = int(match[1])
        last = int(match[2] or first)
        if not 1 <= first <= last <= 54:
            raise ValueError(f"课程周次超出范围：{part}")
        weeks.update(week for week in range(first, last + 1)
                     if match[3] is None or week % 2 == (match[3] == "单"))
    return weeks


def clock_time(value):
    return datetime.strptime(value, "%H:%M").time()


class Timetables:
    def __init__(self, config):
        self.plans = []
        campuses = config.get("campus_periods", {})
        for plan in config.get("timetables", []):
            key = plan["id"]
            first_monday = date.fromisoformat(plan["week1_monday"])
            minutes = plan.get("remind_minutes", 15)
            if (not re.fullmatch(r"[a-zA-Z0-9_-]+", key) or first_monday.weekday() != 0
                    or type(minutes) is not int or not 0 <= minutes <= 1440
                    or any(entry["id"] == key for entry in self.plans)):
                raise ValueError(f"课表 {key} 的 ID、首周周一或提醒提前量无效。")
            classes = []
            for course in plan["courses"]:
                first, last = course["periods"]
                campus = campuses.get(course["campus"], {})
                if (type(first) is not int or type(last) is not int or not 1 <= first <= last
                        or not 1 <= course["weekday"] <= 7
                        or any(str(n) not in campus for n in range(first, last + 1))):
                    raise ValueError(f"课表 {key} 的「{course['title']}」使用了无效星期或节次。")
                start = clock_time(campus[str(first)][0])
                end = clock_time(campus[str(last)][1])
                if start >= end:
                    raise ValueError(f"课程开始时间必须早于结束时间：{course['title']}")
                classes.append({**course, "weeks": teaching_weeks(course["weeks"]), "start": start, "end": end})
            self.plans.append({
                **plan, "first": first_monday, "minutes": minutes, "classes": classes,
                "last_week": max((max(c["weeks"], default=0) for c in classes), default=0),
            })

    def days(self, day):
        """Yield the active timetables, including those with a free day."""
        for plan in self.plans:
            week = (day - plan["first"]).days // 7 + 1
            if 1 <= week <= plan["last_week"]:
                courses = [c for c in plan["classes"] if c["weekday"] == day.isoweekday() and week in c["weeks"]]
                yield plan, week, sorted(courses, key=lambda c: c["start"])

    def agenda(self, day):
        return [
            {"类型": "课程", "课表": plan["daily_title"], "标题": c["title"],
             "时间": f'{c["start"]:%H:%M}', "结束": f'{c["end"]:%H:%M}',
             "节次": periods_text(c["periods"]), "地点": c["room"], "提前分钟": plan["minutes"]}
            for plan, _, courses in self.days(day) for c in courses
        ]

    def daily_messages(self, day):
        for plan, week, courses in self.days(day):
            lines = [f'{plan["daily_title"]} · {day}（第{week}周）']
            lines.extend(course_text(course) for course in courses)
            if not courses:
                lines.append("今天没有课。")
            yield f'classes:{plan["id"]}:{day}', "\n".join(lines)

    def reminders(self, at):
        for day in (at.date(), at.date() + timedelta(days=1)):
            for plan, _, courses in self.days(day):
                minutes = plan["minutes"]
                for course in courses:
                    start = datetime.combine(day, course["start"], at.tzinfo)
                    due = start - timedelta(minutes=minutes)
                    until = min(start if minutes else start + timedelta(minutes=1), due + timedelta(seconds=601))
                    if due <= at < until:
                        identity = json.dumps([course["title"], course["periods"]], ensure_ascii=False)
                        fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                        key = f'class:{plan["id"]}:{day}:{fingerprint}'
                        yield key, f'{plan["reminder_title"]}\n{course_text(course)}', int(until.timestamp())


def periods_text(periods):
    first, last = periods
    return f"第{first}节" if first == last else f"第{first}–{last}节"


def course_text(course):
    room = f' · {course["room"]}' if course["room"] else ""
    return (f'{course["start"]:%H:%M}–{course["end"]:%H:%M} '
            f'{periods_text(course["periods"])}｜{course["title"]}{room}')
