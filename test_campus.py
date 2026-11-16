"""Regression checks for persistence, MCP, and the DSH-IM request contract."""

import json
import sys
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from campus import Campus, ROOT, now, parse_start, stamp
from notices import ArticleText, NoticeList
from worker import ROUTE, Sender


class CampusTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.campus = Campus(self.path / "campus.sqlite3")
        self.at = parse_start("2026-11-16 07:30")

    def test_confirmation_and_durable_reminder(self):
        draft = self.campus.draft("交实验报告", "2026-11-16 09:00", 15, at=self.at)
        self.assertEqual(self.campus.agenda("2026-11-16"), [])
        with self.assertRaises(ValueError):
            self.campus.confirm("确认", at=self.at)
        saved = self.campus.confirm(draft["确认口令"], at=self.at)
        repeat = self.campus.confirm(draft["确认口令"], at=self.at)
        self.assertEqual(saved["日程编号"], repeat["日程编号"])
        due = self.at + timedelta(minutes=75)
        self.campus.enqueue(due, self.at.time())
        reopened = Campus(self.path / "campus.sqlite3")
        item = reopened.claim(stamp(due))
        self.assertEqual(item["key"], f"event:{saved['日程编号']}")
        reopened.result(item["key"], "sent")
        reopened.enqueue(due, self.at.time())
        self.assertEqual([row["state"] for row in reopened.delivery_status() if row["key"] == item["key"]], ["sent"])

    def test_expiry_and_cancellation(self):
        draft = self.campus.draft("交实验报告", "2026-11-16 09:00", 15, at=self.at)
        with self.assertRaises(ValueError):
            self.campus.confirm(draft["确认口令"], at=self.at + timedelta(minutes=15))
        saved = self.campus.confirm(draft["确认口令"], at=self.at)
        due = self.at + timedelta(minutes=75)
        self.campus.enqueue(due, self.at.time(), include_digest=False)
        self.campus.cancel(f"确认取消 {saved['日程编号']}")
        self.assertIsNone(self.campus.claim(stamp(due)))
        self.assertEqual(self.campus.agenda("2026-11-16"), [])

    def test_notice_parsing_and_course_import(self):
        list_parser = NoticeList("通信工程学院", "https://comm.hdu.edu.cn/jxdt/list.htm")
        list_parser.feed('''
          <a href="/2026/1116/c2849a302892/page.htm" title="完整 标题"><span>标...</span></a>
          <a href="/2026/1116/c2849a302892/page.htm">重复链接</a>
          <a href="https://example.com/2026/1116/c2849a302892/page.htm">外部链接</a>
        ''')
        self.assertEqual(len(list_parser.items), 1)
        self.campus.store_notices(list_parser.items.values(), self.at)
        self.campus.store_notices(list_parser.items.values(), self.at)
        self.assertEqual(len(self.campus.notices()), 1)
        article = ArticleText()
        article.feed("<div>导航</div><div class='wp_articlecontent'><p>开始<span>时间</span></p>"
                     "<script>隐藏</script></div><div>页脚</div>")
        self.assertEqual("".join(article.parts).strip(), "开始时间")
        csv = self.path / "classes.csv"
        csv.write_text("title,date,time,location,minutes\n通信原理,2026-11-16,08:00,A101,15\n", encoding="utf-8")
        self.assertEqual(self.campus.import_classes(csv), 1)
        self.assertEqual(self.campus.import_classes(csv), 0)
        digest = self.campus.digest(self.at)
        self.assertIn("08:00 通信原理 A101", digest)
        self.assertIn("[N1] 通信工程学院｜完整 标题", digest)

    def test_wechat_request(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"sent":true}')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                sender = Sender({"mode": "wechat", "harness_url": f"http://127.0.0.1:{server.server_port}",
                                 "bot_id": "demo-bot", "target_id": "demo-target"})
                self.assertEqual(sender.send("课前提醒"), ("sent", ""))
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(received, [(ROUTE, {"botId": "demo-bot", "targetId": "demo-target", "text": "课前提醒"})])
        self.assertEqual(sender.send("课前提醒")[0], "queued")


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_draft_and_confirm(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text("{}", encoding="utf-8")
            server = StdioServerParameters(command=sys.executable,
                args=[str(ROOT / "campus_mcp.py"), "--config", str(config)])
            async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                tools = {tool.name for tool in (await client.list_tools()).tools}
                self.assertIn("notice_details", tools)
                when = (now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
                draft = await client.call_tool("draft_event", {
                    "title": "MCP 测试", "starts_at": when, "minutes": 10,
                })
                self.assertFalse(draft.isError, draft.content)
                campus = Campus(config.parent / "data/campus.sqlite3")
                self.assertEqual(campus.agenda(when[:10]), [])
                result = await client.call_tool("confirm_event", {"message": draft.structuredContent["确认口令"]})
                self.assertFalse(result.isError, result.content)
                self.assertEqual(campus.agenda(when[:10])[0]["标题"], "MCP 测试")


if __name__ == "__main__":
    unittest.main()
