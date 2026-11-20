"""推特卡片（xcard）的数据整理、模板和本机出图的失败路径：不联网，不起真浏览器。"""

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests._stub import install  # noqa: E402

install()

import jinja2  # noqa: E402

import xcard  # noqa: E402

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=BJ)


def _png(w=40, h=30, color=(200, 30, 30)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, status, ctype, content):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.content = content


class FakeClient:
    """按地址回图：地址里带 "bad" 的回 404，带 "tall" 的回竖图。记下请求过的地址。"""

    def __init__(self):
        self.urls = []

    async def get(self, url, headers=None):
        self.urls.append(url)
        if "bad" in url:
            return _Resp(404, "text/html", b"nope")
        return _Resp(200, "image/png", _png(30, 60) if "tall" in url else _png())


def card(**kw):
    c = {
        "platform": "x", "url": "https://x.com/a/status/1",
        "text": "hello @someone see https://example.com/path/that/is/quite/long/indeed. #话题 #123",
        "author": {"name": "某某", "handle": "@someone", "avatar": "https://pbs.twimg.com/a.jpg",
                   "verification": {"verified": True, "type": "individual"}},
        "images": ["https://pbs.twimg.com/media/PHOTO.jpg?name=orig"], "video": False, "duration": None,
        "stats": [["回复", "1"], ["转帖", "2"], ["喜欢", "3"], ["浏览", "4"]],
        "time": "2026-11-18 16:51", "quote": None,
    }
    c.update(kw)
    return c


def build(c, theme="light", **kw):
    data = asyncio.run(xcard.x_card_data(FakeClient(), c, theme, now=NOW, embed_fonts=False, **kw))
    return data, xcard.build_x_html(data)


class TestThemeNow(unittest.TestCase):
    def at(self, h, m):
        # 北京时间 h:m 换成 UTC 传进去，确认按北京时间判断、不看本机时区
        return datetime(2026, 11, 20, h, m, tzinfo=BJ).astimezone(UTC)

    def test_boundaries(self):
        self.assertEqual(xcard.theme_now(self.at(5, 59)), "dark")
        self.assertEqual(xcard.theme_now(self.at(6, 0)), "light")
        self.assertEqual(xcard.theme_now(self.at(17, 59)), "light")
        self.assertEqual(xcard.theme_now(self.at(18, 0)), "dark")

    def test_auto_theme_in_data(self):
        data, _ = build(card(), theme="auto")
        self.assertIn(data["theme"], ("light", "dark"))
        data, html = build(card(), theme="dark")
        self.assertIn('<body class="dark">', html)


class TestText(unittest.TestCase):
    def test_blank(self):
        for s in ("", "  \n\t", "​​", "ㅤㅤ", "⠀ ⠀", "﻿‎"):
            self.assertTrue(xcard.is_blank(s), repr(s))
            self.assertEqual(xcard.tidy(s), "")
        self.assertFalse(xcard.is_blank("​a"))

    def kinds(self, text):
        return [(s["k"], s["t"]) for s in xcard.segments(text) if s["k"] != "text"]

    def test_links_mentions_tags(self):
        segs = self.kinds(".@NASA and a@b.com, see https://go.nasa.gov/vgr. #ポケモン30周年 #123 x#y")
        self.assertIn(("link", "@NASA"), segs)
        self.assertIn(("link", "go.nasa.gov/vgr"), segs)       # 句末的点不进链接
        self.assertIn(("link", "#ポケモン30周年"), segs)
        self.assertNotIn(("link", "@b"), segs)                   # 邮箱里的 @ 不算
        self.assertFalse(any(t in ("#123", "#y") for _, t in segs))  # 纯数字、紧贴字母的 # 不算

    def test_short_url(self):
        self.assertEqual(xcard.short_url("https://www.ana.co.jp/ja/jp/promotion/pokemonjet/"), "ana.co.jp/ja/jp/promotio…")
        self.assertEqual(xcard.short_url("https://go.nasa.gov/vgr"), "go.nasa.gov/vgr")

    def test_quotes_proportional_only_in_latin(self):
        en = [s for s in xcard.segments("Voyager’s “Light-Day”") if s["k"] == "pw"]
        self.assertEqual("".join(s["t"] for s in en), "’“”")
        zh = [s for s in xcard.segments("第三架“宝可梦喷气机”——赤·绿") if s["k"] == "pw"]
        self.assertEqual(zh, [])
        # 右引号、省略号跟前一个字，左引号跟后一个字
        mixed = [s["t"] for s in xcard.segments("fine… 还有 “quotes”。中文“好”") if s["k"] == "pw"]
        self.assertEqual(mixed, ["…", "“", "”"])

    def test_emoji_wrapped(self):
        segs = self.kinds("好❤️ ok 🚀👍🏽 🇯🇵 ☀ 1️⃣")
        emos = [t for k, t in segs if k == "emo"]
        self.assertEqual(emos, ["❤️", "🚀", "👍🏽", "🇯🇵", "1️⃣"])   # 不带 VS16 的 ☀ 照字画

    def test_paragraphs(self):
        ps = xcard.paragraphs("一\n二\n\n\n三")
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[0][0]["t"], "一\n二")


class TestFitText(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(xcard.fit_text("短的。", 10, 56), ("短的。", False))

    def test_long_cut_at_punctuation(self):
        text = "这是一句话。" * 2000
        shown, cut = xcard.fit_text(text, 20, 56)
        self.assertTrue(cut)
        self.assertTrue(shown.endswith("。……"))
        self.assertLessEqual(xcard.estimate_lines(shown, 56), 20)

    def test_newlines_count(self):
        text = "\n".join(["a"] * 50)
        shown, cut = xcard.fit_text(text, 10, 56)
        self.assertTrue(cut)
        self.assertLessEqual(shown.count("\n"), 9)

    def test_long_card_shows_more_line(self):
        data, html = build(card(text="很长的一段字，" * 3000))
        self.assertTrue(data["truncated"])
        self.assertIn("完整内容见原文", html)
        self.assertLess(data["height"], xcard.MAX_WINDOW_HEIGHT + 1)
        data, html = build(card(text="短"))
        self.assertNotIn("完整内容见原文", html)


class TestNumbersAndTime(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(xcard.compact_count(9999), "9999")
        self.assertEqual(xcard.compact_count(12345), "1.2万")
        self.assertEqual(xcard.compact_count(252000), "25.2万")
        self.assertEqual(xcard.compact_count(100000), "10万")
        self.assertEqual(xcard.compact_count(123456789), "1.2亿")
        self.assertIsNone(xcard.compact_count(None))

    def test_stamp(self):
        ts = int(datetime(2026, 11, 18, 16, 51, tzinfo=BJ).timestamp())
        self.assertEqual(xcard.x_stamp(xcard._to_datetime(ts)), "下午4:51 · 2026年11月18日")
        self.assertEqual(xcard.x_stamp(xcard._to_datetime("2026-11-18 00:05")), "上午12:05 · 2026年11月18日")
        self.assertEqual(xcard.x_stamp(xcard._to_datetime("Fri Oct 28 03:49:11 +0000 2022")),
                         "上午11:49 · 2022年10月28日")
        self.assertEqual(xcard.x_stamp(None), "")

    def test_short_date(self):
        self.assertEqual(xcard.short_date(xcard._to_datetime("2026-11-15 10:00"), NOW), "11月15日")
        self.assertEqual(xcard.short_date(xcard._to_datetime("2024-11-15 10:00"), NOW), "2024年11月15日")

    def test_badge(self):
        self.assertEqual(xcard.badge_kind({"verified": True, "type": "individual"}), "blue")
        self.assertEqual(xcard.badge_kind({"verified": True, "type": "organization"}), "gold")
        self.assertEqual(xcard.badge_kind("government"), "gray")
        self.assertIsNone(xcard.badge_kind({"verified": True}))          # 有认证但不知道哪种：不画
        self.assertIsNone(xcard.badge_kind({"verified": False, "type": "individual"}))
        self.assertIsNone(xcard.badge_kind(None))


class TestCardData(unittest.TestCase):
    def test_main_path(self):
        c = card(x_stats={"replies": 12, "retweets": 345, "likes": 67890, "bookmarks": 1000, "views": 252000})
        data, html = build(c)
        self.assertTrue(data["author"]["avatar"].startswith("data:image/png;base64,"))
        self.assertEqual(data["author"]["badge"], "blue")
        self.assertEqual(data["author"]["handle"], "@someone")
        self.assertEqual(data["time"], "下午4:51 · 2026年11月18日")
        self.assertEqual(data["views"], "25.2万")
        self.assertEqual([(a["icon"], a["t"]) for a in data["actions"]],
                         [("reply", "12"), ("repost", "345"), ("like", "6.8万"), ("bookmark", "1000")])
        self.assertIn('class="badge blue"', html)
        self.assertIn("<b>25.2万</b> 查看", html)
        self.assertIn('<span class="ln">@someone</span>', html)
        self.assertIn('<span class="ln">#话题</span>', html)
        json.dumps(data)  # 文转图要把数据转成 JSON

    def test_photos_not_drawn(self):
        client = FakeClient()
        data = asyncio.run(xcard.x_card_data(client, card(), "light", now=NOW, embed_fonts=False))
        self.assertIsNone(data["media"])
        self.assertFalse(any("PHOTO" in u for u in client.urls))  # 照片另附，连下载都不下

    def test_stats_fallback_and_missing(self):
        data, html = build(card(stats=[["回复", "1,234"], ["喜欢", "5.6万"]], x_stats=None))
        self.assertEqual([(a["icon"], a["t"]) for a in data["actions"]], [("reply", "1234"), ("like", "5.6万")])
        data, html = build(card(stats=[], time=None))
        self.assertEqual(data["actions"], [])
        self.assertNotIn('class="actions', html)
        self.assertNotIn('class="info"', html)

    def test_zero_count_icon_only(self):
        data, html = build(card(stats=[], x_stats={"replies": 0, "retweets": 5, "likes": None, "bookmarks": 0}))
        self.assertEqual([(a["icon"], a["t"]) for a in data["actions"]], [("reply", ""), ("repost", "5"), ("bookmark", "")])
        self.assertEqual(html.count("<span>5</span>"), 1)
        self.assertNotIn("<span>0</span>", html)

    def test_missing_thumbnail_no_play(self):
        data, html = build(card(videos=[{"thumbnail": "https://bad/v.jpg"}]))
        self.assertIsNone(data["media"]["cells"][0]["src"])
        self.assertIn("图片未加载", html)
        self.assertNotIn('class="play"', html)

    def test_no_body(self):
        for text in ("", "​ㅤ", "   "):
            data, html = build(card(text=text))
            self.assertEqual(data["paras"], [])
            self.assertNotIn('class="body"', html)

    def test_translation_replaces_text(self):
        data, html = build(card(text="こんにちは", translation="你好 @someone"))
        self.assertIn("你好", html)
        self.assertNotIn("こんにちは", html)
        self.assertNotIn("翻译自", html)

    def test_no_avatar_no_badge(self):
        c = card(author={"name": "无图", "handle": "x", "avatar": "https://bad/a.jpg"})
        data, html = build(c)
        self.assertIsNone(data["author"]["avatar"])
        self.assertIsNone(data["author"]["badge"])
        self.assertIn('<div class="avatar"></div>', html)
        self.assertNotIn('class="badge', html)

    def test_video_legacy(self):
        c = card(images=["https://pbs.twimg.com/thumb.jpg"], video=True, duration="1:41")
        data, html = build(c)
        m = data["media"]
        self.assertEqual(m["n"], 1)
        self.assertTrue(m["cells"][0]["video"])
        self.assertEqual(m["cells"][0]["duration"], "1:41")
        self.assertIn('class="play"', html)
        self.assertIn('<div class="pill">1:41</div>', html)

    def test_video_new_field_and_more(self):
        vids = [{"thumbnail": f"https://pbs.twimg.com/v{i}.jpg", "width": 1280, "height": 720,
                 "duration": 65.4, "type": "gif" if i == 1 else "video"} for i in range(6)]
        data, html = build(card(videos=vids))
        m = data["media"]
        self.assertEqual((m["n"], m["more"]), (4, 2))
        self.assertEqual(m["cells"][0]["duration"], "1:05")
        self.assertTrue(m["cells"][1]["gif"])
        self.assertIn("+2", html)

    def test_single_media_ratio_clamped(self):
        data, _ = build(card(videos=[{"thumbnail": "https://pbs.twimg.com/tall.jpg", "width": 100, "height": 400}]))
        self.assertEqual(data["media"]["ratio"], 1.0)

    def test_quote_structured(self):
        q = {"name": "被引者", "handle": "quoted", "avatar": "https://pbs.twimg.com/q.jpg",
             "verification": {"verified": True, "type": "government"}, "timestamp": "2026-11-15 09:00",
             "text": "被引正文", "images": [{"url": "https://pbs.twimg.com/media/Q.jpg?name=orig", "width": 100, "height": 300}]}
        client = FakeClient()
        data = asyncio.run(xcard.x_card_data(client, card(quote=q), "light", now=NOW, embed_fonts=False))
        html = xcard.build_x_html(data)
        qd = data["quote"]
        self.assertEqual((qd["name"], qd["handle"], qd["badge"], qd["date"]), ("被引者", "@quoted", "gray", "11月15日"))
        self.assertEqual(qd["media"]["ratio"], 0.5625)            # 引用框里的图一律 16:9
        self.assertIn("https://pbs.twimg.com/media/Q.jpg?name=medium", client.urls)
        self.assertIn('class="quote"', html)
        self.assertIn("被引正文", html)

    def test_quote_legacy(self):
        data, html = build(card(quote={"author": "老 名字 @old_id", "text": "旧字段"}))
        qd = data["quote"]
        self.assertEqual((qd["name"], qd["handle"]), ("老 名字", "@old_id"))
        self.assertIsNone(qd["media"])
        self.assertEqual(qd["date"], "")
        self.assertIn('<div class="q-av"></div>', html)

    def test_poll(self):
        poll = {"choices": [{"label": "甲", "count": 30, "percentage": 75}, {"label": "乙", "count": 10, "percentage": 25}],
                "total_votes": 40, "ends_at": "2026-11-01T00:00:00Z"}
        data, html = build(card(poll=poll))
        p = data["poll"]
        self.assertEqual([(c["label"], c["pct"], c["lead"]) for c in p["choices"]], [("甲", "75%", True), ("乙", "25%", False)])
        self.assertEqual(p["footer"], "共 40 票 · 已结束")
        self.assertIn('class="pc lead"', html)
        # 没给比例按票数算；都没有只画选项名
        p = xcard.poll_data({"choices": [{"label": "a", "count": 1}, {"label": "b", "count": 3}],
                             "ends_at": "2026-12-01T07:00:00Z"}, NOW)
        self.assertEqual([c["pct"] for c in p["choices"]], ["25%", "75%"])
        self.assertEqual(p["footer"], "共 4 票 · 截止 12月1日 下午3:00")
        p = xcard.poll_data({"choices": [{"label": "a"}, {"label": "b"}]}, NOW)
        self.assertEqual([c["pct"] for c in p["choices"]], ["", ""])
        self.assertIsNone(xcard.poll_data({"choices": []}))


class TestEscape(unittest.TestCase):
    EVIL = '<script>alert(1)</script><img src=x onerror="y">'

    def test_text_escaped(self):
        c = card(text=self.EVIL, author={"name": self.EVIL, "handle": "a"}, quote={"author": "q @q", "text": self.EVIL})
        data, html = build(c)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)

    def test_escaped_without_autoescape(self):
        """文转图远端套模板不一定开自动转义：模板里显式 |e 也要挡住。"""
        c = card(text=self.EVIL, author={"name": self.EVIL, "handle": "a"})
        data, _ = build(c)
        html = jinja2.Environment(autoescape=False).from_string(xcard.TEMPLATE.read_text(encoding="utf-8")).render(**data)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


@unittest.skipUnless(xcard.font_path(xcard.ROUNDED_NAME), "fonts/ 里没有文渊圆体")
class TestFonts(unittest.TestCase):
    def test_subset_renamed(self):
        from fontTools.ttLib import TTFont
        import base64

        css = xcard.font_css("推特卡片한")   # 한 文渊圆体和 Maple 都没有，交给系统字体
        self.assertIn('font-family:"LinkCardRounded"', css)
        self.assertIn("font-weight:100 900", css)
        self.assertNotIn("LinkCardMono", css)   # 兜底字体也没有这个字，就不内嵌兜底字体
        self.assertNotIn("WenYuan", css)
        b64 = css.split("base64,", 1)[1].split(")", 1)[0]
        font = TTFont(io.BytesIO(base64.b64decode(b64)))
        # OFL 保留字体名不能用作改过的字体的名字；版权声明、许可证说明（0、7、13、14）照原样保留
        names = " ".join(str(n) for n in font["name"].names if n.nameID not in (0, 7, 13, 14))
        self.assertNotIn("WenYuan", names)
        self.assertNotIn("文渊", names)
        self.assertNotIn("文源", names)
        self.assertIn("WenYuan", str(font["name"].getName(0, 3, 1, 0x409)))
        self.assertIn("chws", {r.FeatureTag for r in font["GPOS"].table.FeatureList.FeatureRecord})

    @unittest.skipUnless(all(xcard.font_path(n) for n, _ in xcard.MONO_NAMES), "fonts/ 里没有 Maple Mono")
    def test_fallback_font_only_for_missing(self):
        from fontTools.ttLib import TTFont
        import base64

        css = xcard.font_css("卡")   # U+E0B0 是 Nerd Font 的符号，文渊圆体没有、Maple NF 有
        self.assertEqual(css.count('font-family:"LinkCardMono"'), 2)  # 常规、粗体
        b64 = css.split('font-family:"LinkCardMono"', 1)[1].split("base64,", 1)[1].split(")", 1)[0]
        font = TTFont(io.BytesIO(base64.b64decode(b64)))
        self.assertIn(0xE0B0, font.getBestCmap())
        self.assertNotIn(ord("卡"), font.getBestCmap())   # 兜底字体只装文渊圆体缺的字
        names = " ".join(str(n) for n in font["name"].names if n.nameID not in (0, 7, 13, 14))
        self.assertNotIn("Maple", names)


class TestLocalRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_no_browser(self):
        orig = xcard.find_browser
        xcard.find_browser = lambda: None
        try:
            with self.assertRaisesRegex(xcard.XCardRenderError, "找不到 Edge 或 Chrome"):
                asyncio.run(xcard.render_local("<p>x</p>", self.dir))
        finally:
            xcard.find_browser = orig

    def test_browser_exits_without_png(self):
        # 拿 Python 当“浏览器”：不认识这些参数，报错退出，不出图
        t = time.monotonic()
        with self.assertRaisesRegex(xcard.XCardRenderError, "没出图"):
            asyncio.run(xcard.render_local("<p>x</p>", self.dir, browser=sys.executable, timeout=20))
        self.assertLess(time.monotonic() - t, 15)
        self.assertEqual(self.leftovers(), [])

    def test_timeout(self):
        orig = xcard._browser_args
        xcard._browser_args = lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(60)"]
        try:
            t = time.monotonic()
            with self.assertRaisesRegex(xcard.XCardRenderError, "秒内没出图"):
                asyncio.run(xcard.render_local("<p>x</p>", self.dir, browser="x", timeout=1))
            self.assertLess(time.monotonic() - t, 25)
        finally:
            xcard._browser_args = orig
        self.assertEqual(self.leftovers(), [])

    def test_crop_to_card(self):
        from PIL import Image, ImageDraw

        png = self.dir / "c.png"
        im = Image.new("RGB", (200, 300), (255, 255, 255))
        ImageDraw.Draw(im).rectangle((16, 16, 183, 120), outline=(207, 217, 222))
        im.save(png)
        box, touches = xcard._content_box(png)
        self.assertEqual(box, (16, 16, 184, 121))
        self.assertFalse(touches)
        xcard.crop_png(png, box)
        with Image.open(png) as out:
            self.assertEqual((out.format, out.size), ("PNG", (168, 105)))

    def test_touching_bottom_detected(self):
        from PIL import Image, ImageDraw

        png = self.dir / "c.png"
        im = Image.new("RGB", (100, 100), (21, 32, 43))
        ImageDraw.Draw(im).rectangle((10, 10, 89, 99), outline=(66, 83, 100))
        im.save(png)
        self.assertTrue(xcard._content_box(png)[1])


if __name__ == "__main__":
    unittest.main()
