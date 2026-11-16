"""Background notice sync and delivery for the campus assistant."""

import json
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from campus import now, stamp
from notices import sync
from weather import forecasts

ROUTE = "/api/dsh-im/delivery/messages"


class DirectLoopback(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(base, payload):
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Harness 地址应是本机 HTTP 地址，例如 http://127.0.0.1:3080。")
    request = Request(base.rstrip("/") + ROUTE, method="POST",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), DirectLoopback())
    try:
        response = opener.open(request, timeout=10)
    except HTTPError as error:
        response = error
    except URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            raise error.reason from error
        raise
    with response:
        status = response.code
        try:
            body = json.loads(response.read(65536))
        except (ValueError, UnicodeError):
            body = {}
    return status, body if isinstance(body, dict) else {}


def discover():
    """Locate the running Desktop Host without using any account credentials."""
    if sys.platform != "win32":
        return None
    command = (
        "$pids = (Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue).Id; "
        "Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.OwningProcess -in $pids } | Select-Object -ExpandProperty LocalPort -Unique"
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=8, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        port = line.strip()
        if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            continue
        base = f"http://127.0.0.1:{port}"
        try:
            status, body = post(base, {})
        except OSError:
            continue
        if status == 400 and body.get("error", {}).get("code") == "bad-request":
            return base
    return None


class Sender:
    def __init__(self, config):
        self.config = config
        self.base = config.get("harness_url")
        self.explicit = bool(self.base)
        self.mode = config.get("mode", "console")
        if self.mode not in {"console", "wechat"}:
            raise ValueError("mode 必须是 console 或 wechat。")
        if self.mode == "wechat" and not all(config.get(key) for key in ("bot_id", "target_id")):
            raise ValueError("启用微信前，请填写 DSH-IM 的 bot_id 和 target_id。")

    def send(self, message):
        if self.mode == "console":
            print(f"\n[本地投递]\n{message}", flush=True)
            return "sent", ""
        if not self.base:
            self.base = discover()
            if not self.base:
                return "queued", "找不到正在运行的 Harness 桌面投递接口"
        try:
            status, body = post(self.base, {
                "botId": self.config["bot_id"], "targetId": self.config["target_id"], "text": message,
            })
        except ConnectionRefusedError:
            if not self.explicit:
                self.base = None
            return "queued", "Harness 拒绝连接"
        except (OSError, ValueError) as error:
            if not self.explicit:
                self.base = None
            return "uncertain", type(error).__name__
        if status == 200 and body.get("sent") is True:
            return "sent", ""
        error = body.get("error", {})
        code = error.get("code", f"http-{status}") if isinstance(error, dict) else f"http-{status}"
        if code == "bot-not-connected":
            return "queued", code
        if not self.explicit and status == 404 and code == "http-404":
            self.base = None
            return "queued", "Harness 桌面端口已变化"
        return ("uncertain" if status >= 500 or 200 <= status < 300 else "failed"), code


def drain(campus, sender, clock=now):
    for _ in range(20):
        at = stamp(clock())
        item = campus.claim(at)
        if not item:
            break
        state, error = sender.send(item["text"])
        campus.result(item["key"], state, error, at + 60 if state == "queued" else 0)
        print(f"{item['key']} {state}" + (f" ({error})" if error else ""), flush=True)
        if state == "queued":
            break


def run(campus, config):
    sender = Sender(config)
    if config.get("mode") == "console":
        print("当前模式在控制台显示测试投递。请用独立的演示数据库。", flush=True)
    stop = threading.Event()
    ready = threading.Event()
    issues = {}
    locations = config.get("weather_locations", [])
    weather = {}

    def refresh():
        while not stop.is_set():
            result = sync(campus, config.get("sources", []))
            issues["failures"] = tuple(result["失败"])
            ready.set()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            stop.wait(config["sync_minutes"] * 60)

    thread = threading.Thread(target=refresh, daemon=True, name="notice-sync")
    thread.start()

    def refresh_weather():
        while not stop.is_set():
            day = now().date()
            weather["daily"] = (day, forecasts(locations, day))
            stop.wait(300)

    if locations:
        threading.Thread(target=refresh_weather, daemon=True, name="weather-sync").start()
    try:
        while True:
            at = now()
            forecast_day, forecast = weather.get("daily", (None, ()))
            campus.enqueue(at, config["digest_at"], issues.get("failures", ()),
                           include_digest=ready.is_set() and (not locations or forecast_day == at.date()),
                           weather=forecast)
            drain(campus, sender)
            time.sleep(config["poll_seconds"])
    except KeyboardInterrupt:
        stop.set()
        print("校园助手已停止。", flush=True)
