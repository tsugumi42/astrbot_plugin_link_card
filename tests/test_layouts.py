"""layouts 的纯函数测试：按平台拼消息，不联网、不需要 AstrBot。"""

import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests._stub import install  # noqa: E402

install()

import fetchers  # noqa: E402
import layouts  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=fetchers.CST)


class TestCleanLink(unittest.TestCase):
    def test_x_drops_share_tokens(self):
        self.assertEqual(layouts.clean_link("https://twitter.com/a/status/1?s=20&t=abc"),
                         "https://x.com/a/status/1")

    def test_youtube_keeps_time(self):
        self.assertEqual(layouts.clean_link("https://www.youtube.com/watch?v=abc&t=90&si=zzz&feature=share"),
                         "https://www.youtube.com/watch?v=abc&t=90")

    def test_youtu_be_keeps_host_and_time(self):
        self.assertEqual(layouts.clean_link("https://youtu.be/abc?si=zzz&t=5"), "https://youtu.be/abc?t=5")

    def test_bilibili_keeps_page_and_time_fragment(self):
        self.assertEqual(
            layouts.clean_link("https://www.bilibili.com/video/BV1xx411c7mD/?p=3&spm_id_from=333&vd_source=x#t=12"),
            "https://www.bilibili.com/video/BV1xx411c7mD?p=3#t=12")

    def test_other_site_drops_utm_only(self):
        self.assertEqual(layouts.clean_link("https://example.com/a?id=7&utm_source=x#top"),
                         "https://example.com/a?id=7")


class TestShortTime(unittest.TestCase):
    def test_this_year_drops_year(self):
        self.assertEqual(layouts.short_time("2026-11-12 17:45", NOW), "11-12 17:45")

    def test_other_year_keeps_year(self):
        self.assertEqual(layouts.short_time("2025-01-02 03:04", NOW), "2025-01-02 03:04")

    def test_missing(self):
        self.assertIsNone(layouts.short_time(None, NOW))


def bili(**kw):
    c = fetchers.card("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=1",
                      title="标题", author={"name": "某UP"}, images=["https://i0.hdslb.com/cover.jpg"],
                      stats=[["播放", "1.2万"], ["弹幕", "30"], ["点赞", "800"]],
                      time="2026-11-12 17:45", duration="3:33", video=True)
    c.update(kw)
    return c


class TestVideo(unittest.TestCase):
    def test_bilibili_cover_then_lines(self):
        msgs = layouts.plan(bili(), now=NOW)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][0], ("image", "https://i0.hdslb.com/cover.jpg"))
        self.assertEqual(msgs[0][1], ("text", "标题\n某UP | 3:33\n播放 1.2万｜点赞 800\n11-12 17:45\n"
                                              "https://www.bilibili.com/video/BV1xx411c7mD"))

    def test_page_line_when_link_names_a_page(self):
        c = bili(pages=[("第一集", "1:00"), ("第二集", "2:00")], page=2)
        text = layouts.video_text(c, "L", NOW)
        self.assertEqual(text.split("\n")[:3], ["标题", "P2/2：第二集", "某UP | 2:00"])

    def test_total_when_no_page_given(self):
        c = bili(pages=[("a", "1:00"), ("b", "2:00")], page=None, total_duration="3:00")
        self.assertIn("某UP | 共 2P｜3:00", layouts.video_text(c, "L", NOW))

    def test_no_cover_no_stats(self):
        msgs = layouts.plan(bili(images=[], stats=[], time=None), now=NOW)
        self.assertEqual(msgs, [[("text", "标题\n某UP | 3:33\nhttps://www.bilibili.com/video/BV1xx411c7mD")]])

    def test_youtube_uses_video_layout(self):
        c = fetchers.card("youtube", "https://youtu.be/abc?si=1", title="T", author={"name": "Ch"},
                          images=["https://i.ytimg.com/x.jpg"], video=True)
        msgs = layouts.plan(c, now=NOW)
        self.assertEqual(msgs[0][1], ("text", "T\nCh\nhttps://youtu.be/abc"))


class TestPixiv(unittest.TestCase):
    def card(self, **kw):
        c = fetchers.card("pixiv", "https://www.pixiv.net/artworks/1", title="作品", author={"name": "画师"},
                          images=["u1"], pages_images=["u1", "u2"])
        c.update(kw)
        return c

    def test_plain_head(self):
        self.assertEqual(layouts.pixiv_head(self.card()), "作品 | 画师")

    def test_marks(self):
        self.assertEqual(layouts.pixiv_head(self.card(ai=True, sensitive=True)), "[AI]🔞 | 作品 | 画师")
        self.assertEqual(layouts.pixiv_head(self.card(sensitive=True)), "🔞 | 作品 | 画师")

    def test_images_in_same_message(self):
        self.assertEqual(layouts.plan(self.card()), [[("text", "作品 | 画师"), ("image", "u1"), ("image", "u2")]])

    def test_many_images_go_to_forward(self):
        msgs = layouts.plan(self.card(pages_images=["a", "b", "c", "d"]), image_limit=3)
        self.assertEqual(msgs, [[("text", "作品 | 画师")], [("forward", ["a", "b", "c", "d"])]])


class TestX(unittest.TestCase):
    def card(self, **kw):
        c = fetchers.card("x", "https://twitter.com/a/status/1?s=20", text="hi", photos=[], videos=[])
        c.update(kw)
        return c

    def test_card_link_and_photos_in_one_message(self):
        msgs = layouts.plan(self.card(photos=["p1", "p2"]))
        self.assertEqual(msgs, [[("card", None), ("text", "https://x.com/a/status/1"),
                                 ("image", "p1"), ("image", "p2")]])

    def test_video_goes_to_second_message(self):
        msgs = layouts.plan(self.card(videos=[{"url": "v.mp4"}]))
        self.assertEqual(msgs, [[("card", None), ("text", "https://x.com/a/status/1")], [("video", "v.mp4")]])

    def test_four_photos_over_limit(self):
        msgs = layouts.plan(self.card(photos=["1", "2", "3", "4"]), image_limit=3)
        self.assertEqual(msgs[1], [("forward", ["1", "2", "3", "4"])])


class TestOtherPlatforms(unittest.TestCase):
    def test_github_not_handled(self):
        self.assertIsNone(layouts.plan(fetchers.card("github", "https://github.com/a/b")))


if __name__ == "__main__":
    unittest.main()
