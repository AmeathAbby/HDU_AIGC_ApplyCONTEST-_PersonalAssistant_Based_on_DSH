"""Read the public HDU notice lists and article bodies."""

import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ARTICLE = re.compile(r"/(\d{4})/(\d{2})(\d{2})/c\d+a\d+/page\.htm")
HOSTS = {"jwc.hdu.edu.cn", "comm.hdu.edu.cn"}


def allowed(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc in HOSTS
            and not parsed.query and not parsed.fragment)


class SiteRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl) or urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            raise ValueError("公告网站跳转出了允许的 HTTPS 站点。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    if not allowed(url):
        raise ValueError("仅支持配置的杭电 HTTPS 公告站点。")
    request = Request(url, headers={"User-Agent": "HDU Campus Assistant/1.0"})
    with build_opener(SiteRedirect()).open(request, timeout=12) as response:
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("网页超过大小限制。")
        charset = response.headers.get_content_charset()
    if not charset:
        meta = re.search(rb"""charset\s*=\s*['"]?([\w-]+)""", data[:4096], re.I)
        charset = meta[1].decode("ascii") if meta else "utf-8"
    return data.decode(charset)


class NoticeList(HTMLParser):
    def __init__(self, source, url):
        super().__init__()
        self.source = source
        self.url = url
        self.items = {}
        self.link = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        self.link = None
        url = urljoin(self.url, attrs.get("href") or "")
        match = ARTICLE.fullmatch(urlsplit(url).path)
        if (not match or not allowed(url)
                or urlsplit(url).netloc != urlsplit(self.url).netloc):
            return
        try:
            published = date(*map(int, match.groups())).isoformat()
        except ValueError:
            return
        self.link = (url, published, attrs.get("title"), [])

    def handle_data(self, data):
        if self.link:
            self.link[3].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.link:
            url, published, title, fragments = self.link
            title = " ".join((title or "".join(fragments)).split())
            if title:
                self.items.setdefault(url, {
                    "url": url, "title": title, "source": self.source, "published": published,
                })
            self.link = None


class ArticleText(HTMLParser):
    """Extract the shared WebPlus article container, including paragraph breaks."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts = []
        self.ignored = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "div" and (self.depth or "wp_articlecontent" in classes):
            self.depth += 1
        if self.depth and tag in {"script", "style"}:
            self.ignored = tag
        if self.depth and tag in {"p", "br", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1
        if tag == self.ignored:
            self.ignored = None
        if self.depth and tag in {"p", "li", "tr", "td"}:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_data(self, data):
        if self.depth and not self.ignored:
            self.parts.append(data)


def sync(campus, sources):
    totals, errors = {}, {}
    for source in sources:
        name, url = source["name"], source["url"]
        try:
            parser = NoticeList(name, url)
            parser.feed(fetch(url))
            if not parser.items:
                raise ValueError("未找到公告链接，请核对栏目地址或页面结构。")
            campus.store_notices(parser.items.values())
            totals[name] = len(parser.items)
        except (OSError, ValueError, LookupError) as exc:
            errors[name] = str(exc)
    return {"栏目条数": totals, "失败": errors}


def read_article(url):
    if not ARTICLE.fullmatch(urlsplit(url).path):
        raise ValueError("无可读取的杭电通知正文。")
    parser = ArticleText()
    parser.feed(fetch(url))
    text = "\n".join(line for raw in "".join(parser.parts).splitlines() if (line := " ".join(raw.split())))
    if not text:
        raise ValueError("没有解析到正文，请打开原文核对。")
    return text[:10000], len(text) > 10000
