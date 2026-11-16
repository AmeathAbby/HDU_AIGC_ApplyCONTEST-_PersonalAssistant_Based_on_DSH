"""Read dated daily forecasts from China Weather Network."""

import re
from datetime import datetime, timedelta
from html import unescape
from urllib.request import Request, urlopen

SOURCE = "https://www.weather.com.cn/weather/"


def forecast_rows(html, day):
    published = re.search(r'id="fc_24h_internal_update_time"\s+value="(\d{10})"', html)
    if not published:
        raise ValueError("未找到预报发布日期")
    base = datetime.strptime(published[1], "%Y%m%d%H").date()
    if not day - timedelta(days=1) <= base <= day:
        raise ValueError("预报发布日期已过期或超前")
    rows = re.findall(r'<li\b[^>]*class="sky [^"]*"[^>]*>(.*?)</li>', html, re.S)
    for offset, row in enumerate(rows[:7]):
        if base + timedelta(days=offset) != day:
            continue
        label = re.search(r'<h1[^>]*>\s*(\d+)日', row)
        condition = re.search(r'<p\b[^>]*class="wea"[^>]*>([^<]+)</p>', row)
        temperature = re.search(r'<p\b[^>]*class="tem"[^>]*>(.*?)</p>', row, re.S)
        if label and int(label[1]) == day.day and condition and temperature:
            values = re.findall(r'>(-?\d+)\s*(?:℃)?\s*<', temperature[1])
            if len(values) == 1 and offset == 0:
                hidden = re.search(r'id="hidden_title"\s+value="[^"]*?(-?\d+)/(-?\d+)\s*°C', html)
                if hidden:
                    values = hidden.groups()
            if values:
                readings = sorted(map(int, values))
                degree = f"{readings[0]}–{readings[-1]}℃" if len(readings) > 1 else f"{readings[0]}℃"
                return f"{unescape(condition[1]).strip()}，{degree}"
    raise ValueError("未找到对应日期的预报")


def forecasts(locations, day):
    lines = []
    for place in locations:
        name, code = place["name"], place["city_code"]
        try:
            request = Request(f"{SOURCE}{code}.shtml", headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=8) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("天气页面超过大小限制")
            lines.append(f"{name}：{forecast_rows(data.decode('utf-8'), day)}")
        except (OSError, ValueError):
            lines.append(f"{name}：暂未获取到当天预报")
    if lines:
        lines.append("天气来源：中国天气网")
    return tuple(lines)
