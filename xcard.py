"""推特卡片：照 X 网页的单条推文样式画一张图。

数据从 fetchers 的卡片字典来（x_card_data 整理成模板变量），模板是 templates/x_card.html。
版式照 Sumika 的推特卡片（规格-链接发送改版 C1、附录 C；代码 linkcard/tweet.go）移植，
再按用户 2026-11-20 给的 X 网页截图去掉 Grok 图标、翻译提示、分享按钮这些网页上的东西。

出图两条路：
- render_local：本机 Edge（找不到再找 Chrome）无界面截图，设备像素比 2，出 PNG 并裁边；
  找不到浏览器或超时抛 XCardRenderError，调用方退回 AstrBot 的文转图。
- 文转图：模板原文 + x_card_data 的结果交给 html_render。所以模板变量只放能转成 JSON 的
  字符串、数字、列表，图标和认证章的矢量直接写在模板里；用户文字在模板里一律显式 |e，
  远端渲染时没开自动转义也不会把正文当 HTML。

卡片里不放主推文的照片（照片另附）；视频、动图画缩略图加播放钮；被引推文的图画在引用框里。
"""

import asyncio
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jinja2

from astrbot.api import logger

try:                                    # 作为包的一部分加载（AstrBot 正常路径）
    from . import render
except ImportError:                     # 测试里直接 import xcard
    import render

BEIJING = timezone(timedelta(hours=8))  # 中国没有夏令时，固定东八区，不看本机时区
TEMPLATE = Path(__file__).parent / "templates" / "x_card.html"

CARD_WIDTH = 600   # 卡片宽（CSS 像素，照 X 网页的推文栏宽）
PAGE_PAD = 8       # 卡片四周留的底色，裁边时按它和卡片边框的色差找边
CONTENT_WIDTH = CARD_WIDTH - 2 - 32
MAX_WINDOW_HEIGHT = 4300  # 本机出图窗口最高（CSS 像素）；DPR 2 时约 8600 像素，和 Sumika 的硬上限 8000 同一量级
BODY_MAX_LINES = 100      # 正文估算超过这么多行就截在标点后，补“……”和“完整内容见原文”
QUOTE_MAX_LINES = 20      # 被引推文正文最多估这么多行
MEDIA_MAX = 4

# ---------------------------------------------------------------------------
# 主题

def theme_now(now=None):
    """北京时间 06:00 ≤ t < 18:00 浅色，其余深色。

    now 带时区就换算到北京时间；不带时区按本机时间理解（datetime.astimezone 的约定）。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    h = now.astimezone(BEIJING).hour
    return "light" if 6 <= h < 18 else "dark"


def _theme(theme):
    if theme in ("light", "dark"):
        return theme
    return theme_now()

# ---------------------------------------------------------------------------
# 文字

# 发帖人拿来占位的“看不见的字”：韩文填充符、盲文空白（照 Sumika linkexpand.blankText）
_FILLERS = {0x3164, 0x115F, 0x1160, 0x2800}


def is_blank(text):
    """整段看不见任何字：只有空白、格式字符（零宽空格、方向标记、BOM）或占位填充符。"""
    for ch in str(text or ""):
        if ch.isspace() or unicodedata.category(ch) == "Cf" or ord(ch) in _FILLERS:
            continue
        return False
    return True


def tidy(text):
    """换行统一成 \\n，去掉行尾空白和首尾空行，三个以上的换行压成一个空行。整段看不见就是空串。"""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if is_blank(s):
        return ""
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip("\n").strip()


# 链接、@账号、#话题按 X 的蓝色画。用正则认，不用接口给的 facets：facets 的下标对的是带 t.co 的原文，
# 而卡片上画的是展开过的正文或译文。规则照 twitter-text 的大意：@ 前面不能是字母数字和 !#$%&*@，
# 话题前面不能是字母数字和 &，纯数字的 # 不算话题。
_TOKEN_RE = re.compile(
    r"(?P<url>https?://[^\s<>\"'「」『』（）【】《》]+)"
    r"|(?P<mention>(?<![A-Za-z0-9_!#$%&*@＠])[@＠][A-Za-z0-9_]{1,15}(?![A-Za-z0-9_@＠]))"
    r"|(?P<tag>(?<![\w&])[#＃](?=\w*[^\W\d])\w+)"
)
_URL_TAIL = ".,!?:;'\"。，！？、；：…)）]】"

# 表情序列（照 Sumika emoji.go 的切法）：旗帜、键帽、默认就是表情样式的字、后面跟 VS16 的符号，
# 再吃掉肤色、VS16、零宽连接的后续字。表情单独包一层，字体先用系统的彩色表情字体，
# 免得文渊圆体把 ❤️ ☀️ 这类画成单色字形。
_DEFAULT_EMOJI = ("⌚⌛⏩-⏬⏰⏳◽◾☔☕♈-♓♿"
                  "⚓⚡⚪⚫⚽⚾⛄⛅⛎⛔⛪⛲⛳⛵"
                  "⛺⛽✅✊✋✨❌❎❓-❕❗➕-➗"
                  "➰➿⬛⬜⭐⭕")
_TEXT_SYMBOL = ("©®‼⁉™ℹ↔-↙↩↪⌀-⏿Ⓜ"
                "▪-◾☀-➿⤀-⯿〰〽㊗㊙")
_EMOJI_BASE = (r"(?:[\U0001F000-\U0001FAFF]|[" + _DEFAULT_EMOJI + r"]|[" + _TEXT_SYMBOL + r"](?=️))")
_EMOJI_TAIL = r"[\U0001F3FB-\U0001F3FF]?️?⃣?[\U000E0020-\U000E007F]*"
_EMOJI_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{2}"
    r"|[0-9#*]️?⃣"
    r"|" + _EMOJI_BASE + _EMOJI_TAIL
    + r"(?:‍(?:[\U0001F000-\U0001FAFF]|[" + _TEXT_SYMBOL + _DEFAULT_EMOJI + r"])" + _EMOJI_TAIL + r")*"
)


def short_url(url):
    """照 X 显示链接：去掉协议和 www.，太长截断补“…”。"""
    s = re.sub(r"^https?://", "", url)
    s = s.removeprefix("www.")
    return s if len(s) <= 26 else s[:24] + "…"


# 文渊圆体（简体字形）把这些标点画成全角宽，夹在英文里会空出一大块（“Voyager’ s”）。英文语境下改用字体自带的
# 比例宽字形（OpenType pwid）；前后挨着全角字（中文、日文）时照旧全角。
_AMBIG = set("‘’“”·…—–")
_CLOSING = set("’”…")   # 跟着前一个字走：fine… 还有 → 比例宽；喷气机” → 全角
_OPENING = set("‘“")    # 跟着后一个字走


def _wide(ch):
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _proportional_marks(text):
    """返回该用比例宽字形的那些标点的位置。

    右引号、省略号看前面最近的字，左引号看后面最近的字（跳过空白和同类标点）；间隔点、破折号两边都不是全角字
    才用比例宽。一边没有字就看另一边。
    """
    marks = set()
    for i, ch in enumerate(text):
        if ch not in _AMBIG:
            continue
        j = i - 1
        while j >= 0 and (text[j].isspace() or text[j] in _AMBIG):
            j -= 1
        k = i + 1
        while k < len(text) and (text[k].isspace() or text[k] in _AMBIG):
            k += 1
        prev = text[j] if j >= 0 else None
        nxt = text[k] if k < len(text) else None
        if ch in _CLOSING:
            side = prev if prev is not None else nxt
            wide = side is not None and _wide(side)
        elif ch in _OPENING:
            side = nxt if nxt is not None else prev
            wide = side is not None and _wide(side)
        else:
            wide = (prev is not None and _wide(prev)) or (nxt is not None and _wide(nxt))
        if not wide:
            marks.add(i)
    return marks


def _text_runs(text, base, marks):
    out = []
    start = 0
    for i, ch in enumerate(text):
        if base + i in marks:
            if i > start:
                out.append({"k": "text", "t": text[start:i]})
            out.append({"k": "pw", "t": ch})
            start = i + 1
    if start < len(text):
        out.append({"k": "text", "t": text[start:]})
    return out


def _plain_segments(text, base=0, marks=frozenset()):
    out = []
    pos = 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.extend(_text_runs(text[pos:m.start()], base + pos, marks))
        out.append({"k": "emo", "t": m.group(0)})
        pos = m.end()
    if pos < len(text):
        out.extend(_text_runs(text[pos:], base + pos, marks))
    return out


def segments(text):
    """一段正文切成 [{"k": text|link|emo|pw, "t": 字}]：链接显示成短链（"pw": 真，省略号用比例宽），
    pw 是英文语境里要用比例宽字形的标点。"""
    marks = _proportional_marks(text)
    out = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        start, end = m.span()
        word = m.group(0)
        if m.lastgroup == "url":
            stripped = word.rstrip(_URL_TAIL)
            # 括号成对出现在链接里（维基百科那种）就留着
            if word.endswith(")") and stripped.count("(") > stripped.count(")"):
                stripped += ")"
            end = start + len(stripped)
            if not stripped or "://" not in stripped or stripped.endswith("://"):
                continue
            shown = short_url(stripped)
        else:
            shown = word
        if start > pos:
            out.extend(_plain_segments(text[pos:start], pos, marks))
        out.append({"k": "link", "t": shown, "pw": m.lastgroup == "url"})
        pos = end
    if pos < len(text):
        out.extend(_plain_segments(text[pos:], pos, marks))
    return out


def paragraphs(text):
    """按空行分段（段落之间留半行，照 Sumika 不封顶时的排法）；段内的单个换行原样保留。"""
    return [segments(p) for p in re.split(r"\n[ \t　]*\n+", text) if p.strip()]


def _units(ch):
    """估算一个字占多宽：全角 2，半角约 1.1（按 20 像素字、文渊圆体量的大概值）。"""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2.0
    if ord(ch) >= 0x1F000:
        return 2.4
    return 1.1


def _line_cost(line, per_line):
    return max(1.0, math.ceil(sum(_units(c) for c in line) / per_line))


def estimate_lines(text, per_line):
    """估正文排几行：每个换行另起一行，空行（分段）算半行。"""
    total = 0.0
    for line in text.split("\n"):
        total += 0.5 if not line.strip() else _line_cost(line, per_line)
    return total


_CUT_AT = "。！？!?；;…\n，,、 "


def fit_text(text, max_lines, per_line):
    """估算超过 max_lines 行就截短：找排得下的最长前缀，退到最近的标点后补“……”。

    返回 (显示的字, 是否截过)。和 Sumika linktrunc.FitBodyLines 同一个意思，这里没有排版引擎，按字宽估。
    """
    if not text or estimate_lines(text, per_line) <= max_lines:
        return text, False
    budget = max_lines - 1  # 留一行给“……”
    used = 0.0
    width = 0.0
    cut = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            if width == 0 and i > 0 and text[i - 1] == "\n":
                used += 0.5
            elif width > 0:
                used += math.ceil(width / per_line)
            width = 0.0
        else:
            width += _units(ch)
        if used + (math.ceil(width / per_line) if width else 0) > budget:
            break
        cut = i + 1
    head = text[:cut]
    # 往回最多找 80 个字的标点，截在标点后；找不到就硬截
    window = head[-80:]
    best = max(window.rfind(p) for p in _CUT_AT)
    if best > 0:
        head = head[:len(head) - len(window) + best + 1]
    head = head.rstrip(" \n，,、")
    return head + "……", True

# ---------------------------------------------------------------------------
# 数字、时间、认证

def compact_count(n):
    """照 X 中文界面写互动数（Sumika CompactCount）：一万以下原样，万、亿各留一位小数。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if n >= 100_000_000:
        return f"{n / 1e8:.1f}".removesuffix(".0") + "亿"
    if n >= 10_000:
        return f"{n / 1e4:.1f}".removesuffix(".0") + "万"
    return str(n)


def _to_datetime(value):
    """时间戳、ISO 字符串、fetchers 的 'YYYY-MM-DD HH:MM'（北京时间）都认；认不出返回 None。"""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        s = str(value).strip()
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?: (\d{1,2}):(\d{2}))?", s)
        if m:
            y, mo, d, hh, mm = m.groups()
            return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=BEIJING)
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass
    try:  # Twitter 原生格式：Fri Oct 28 03:49:11 +0000 2022
        return datetime.strptime(str(value).strip(), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def x_stamp(dt):
    """照 X 中文界面写发布时间（北京时间）：下午4:51 · 2026年11月18日。"""
    if dt is None:
        return ""
    b = dt.astimezone(BEIJING)
    half = "下午" if b.hour >= 12 else "上午"
    h = b.hour % 12 or 12
    return f"{half}{h}:{b.minute:02d} · {b.year}年{b.month}月{b.day}日"


def short_date(dt, now=None):
    """被引推文头一行的日期：今年写“11月15日”，往年带年份“2024年11月15日”（北京时间）。"""
    if dt is None:
        return ""
    b = dt.astimezone(BEIJING)
    year = (now or datetime.now(timezone.utc)).astimezone(BEIJING).year
    if b.year == year:
        return f"{b.month}月{b.day}日"
    return f"{b.year}年{b.month}月{b.day}日"


def badge_kind(verification):
    """认证标的颜色：蓝（个人 / 蓝标）、金（机构）、灰（政府）。取不到或认不出就不画（规格 C 禁止的偷懒写法）。

    fxtwitter 给的是 {"verified": true, "type": "individual|organization|government"}；也认 Sumika 的
    blue / business / government 写法和直接给的字符串。
    """
    v = verification
    if isinstance(v, dict):
        v = v.get("type") if v.get("verified", True) else None
    v = str(v or "").strip().lower()
    if v in ("individual", "blue"):
        return "blue"
    if v in ("organization", "business", "gold"):
        return "gold"
    if v in ("government", "gray", "grey"):
        return "gray"
    return None


def _handle(h):
    h = str(h or "").strip().lstrip("@＠").strip()
    return "@" + h if h else ""

# ---------------------------------------------------------------------------
# 互动数

# 照用户 2026-11-20 的截图：回复、转帖、喜欢、书签四项（浏览数在时间那一行，分享按钮不画）。
# 取 fetchers 新字段 x_stats 的原始数；没有就从 stats 里按中文名取已经缩写好的字。
ACTIONS = (("reply", "replies", "回复"), ("repost", "retweets", "转帖"),
           ("like", "likes", "喜欢"), ("bookmark", "bookmarks", "书签"))


def _stat_text(card, key, label):
    raw = (card.get("x_stats") or {}).get(key)
    if raw is not None and raw != "":
        return compact_count(raw)
    for s in card.get("stats") or []:
        if s and len(s) >= 2 and s[0] == label and s[1] not in (None, ""):
            v = str(s[1]).replace(",", "")
            return compact_count(v) if v.isdigit() else v
    return None


def actions(card):
    """取不到的项不画；是 0 的照 X 网页只画图标、不写数字。"""
    out = []
    for icon, key, label in ACTIONS:
        t = _stat_text(card, key, label)
        if t is not None:
            out.append({"icon": icon, "t": "" if t == "0" else t})
    return out

# ---------------------------------------------------------------------------
# 投票

def poll_data(poll, now=None):
    """fxtwitter 的 poll（choices[label,count,percentage]、total_votes、ends_at）整理成卡片用的样子。

    接口没给比例就用票数除以总票数算；都没有只画选项名（规格 C1“卡片上的投票”）。
    """
    if not isinstance(poll, dict):
        return None
    choices = [c for c in poll.get("choices") or [] if isinstance(c, dict) and str(c.get("label") or "").strip()]
    if not choices:
        return None
    total = poll.get("total_votes")
    try:
        total = int(total) if total is not None else sum(int(c.get("count") or 0) for c in choices)
    except (TypeError, ValueError):
        total = None
    rows = []
    for c in choices:
        pct = c.get("percentage")
        if pct is None and total and c.get("count") is not None:
            try:
                pct = int(c["count"]) * 100 / total
            except (TypeError, ValueError):
                pct = None
        try:
            pct = None if pct is None else max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pct = None
        rows.append({"label": str(c["label"]).strip(), "pct": pct})
    top = max((r["pct"] for r in rows if r["pct"] is not None), default=None)
    for r in rows:
        p = r.pop("pct")
        r["fill"] = round(p, 2) if p is not None else 0
        r["pct"] = "" if p is None else (f"{p:.0f}%" if p == int(p) else f"{p:.1f}%")
        r["lead"] = bool(p is not None and top and p == top)
    foot = []
    if total:
        foot.append(f"共 {compact_count(total)} 票")
    ends = _to_datetime(poll.get("ends_at"))
    if ends:
        now = now or datetime.now(timezone.utc)
        if ends <= now:
            foot.append("已结束")
        else:
            b = ends.astimezone(BEIJING)
            half = "下午" if b.hour >= 12 else "上午"
            foot.append(f"截止 {b.month}月{b.day}日 {half}{b.hour % 12 or 12}:{b.minute:02d}")
    return {"choices": rows, "footer": " · ".join(foot), "footer_parts": foot}

# ---------------------------------------------------------------------------
# 媒体

def _pbs_medium(url):
    """X 图床的原图换成 name=medium（最长边 1200），免得原图超过下载上限画成占位（Sumika 同样做法）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.hostname != "pbs.twimg.com" or not parts.path.startswith("/media/"):
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "name"] + [("name", "medium")]
    return urlunsplit(parts._replace(query=urlencode(q)))


def _fmt_duration(v):
    if v in (None, ""):
        return None
    if isinstance(v, str) and ":" in v:
        return v
    try:
        s = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def video_items(src, legacy=True):
    """视频、动图的缩略图。

    新字段 videos：[{"thumbnail", "width", "height", "duration", "type": "video|gif"}]（fetchers 待补）。
    没有新字段时（legacy）：card["video"] 为真说明 images 里全是视频缩略图，第一个配 card["duration"]。
    """
    out = []
    for v in src.get("videos") or []:
        if not isinstance(v, dict):
            continue
        thumb = v.get("thumbnail") or v.get("thumbnail_url")
        if not thumb:
            continue
        out.append({"url": thumb, "w": v.get("width"), "h": v.get("height"), "video": True,
                    "gif": (v.get("type") == "gif") or bool(v.get("gif")),
                    "duration": _fmt_duration(v.get("duration"))})
    if not out and legacy and src.get("video"):
        for i, u in enumerate(src.get("images") or []):
            if u:
                out.append({"url": u, "w": None, "h": None, "video": True, "gif": False,
                            "duration": _fmt_duration(src.get("duration")) if i == 0 else None})
    return out


def photo_items(src):
    out = []
    for p in src.get("images") or []:
        if isinstance(p, dict):
            url, w, h = p.get("url"), p.get("width"), p.get("height")
        else:
            url, w, h = p, None, None
        if url:
            out.append({"url": _pbs_medium(url), "w": w, "h": h, "video": False, "gif": False, "duration": None})
    return out


async def _download(client, items, referer):
    async def one(it):
        uri, w, h = await render._image_info(client, it["url"], referer)
        return {"src": uri, "video": it["video"], "gif": it["gif"], "duration": it["duration"],
                "w": it.get("w") or w, "h": it.get("h") or h}
    return list(await asyncio.gather(*[one(it) for it in items]))


def media_grid(cells, total, single_ratio=True):
    """照 X 的 1～4 张网格：一张按原比例（高/宽限 0.5～1），两张以上整块 16:9；超过四张第四格写 +N。

    single_ratio=False（被引推文）一律 16:9（规格附录 C“引用媒体 高/宽 0.5625”）。
    """
    if not cells:
        return None
    ratio = 0.5625
    if len(cells) == 1 and single_ratio:
        c = cells[0]
        try:
            r = float(c["h"]) / float(c["w"]) if c.get("w") and c.get("h") else 0.5625
        except (TypeError, ValueError, ZeroDivisionError):
            r = 0.5625
        ratio = min(max(r, 0.5), 1.0)
    for c in cells:
        c.pop("w", None)
        c.pop("h", None)
    return {"n": len(cells), "cells": cells, "more": max(0, total - len(cells)),
            "ratio": round(ratio, 4), "aspect": f"1000 / {round(ratio * 1000)}"}

# ---------------------------------------------------------------------------
# 被引推文

def _quote_author(q):
    name = str(q.get("name") or "").strip()
    handle = q.get("handle") or q.get("screen_name")
    if not name and not handle and q.get("author"):
        # 旧字段：“名字 @账号”
        a = str(q["author"]).strip()
        if " @" in a:
            name, _, handle = a.rpartition(" @")
        elif a.startswith("@"):
            handle = a
        else:
            name = a
    return name.strip(), _handle(handle)

# ---------------------------------------------------------------------------
# 字体：直接用系统里装的字体。卡片在本机 Edge 里截图，系统和当前用户装的字体它都能用，
# 按配置的顺序找，前面的缺字再往后找，最后交给浏览器自己的兜底字体。

DEFAULT_FONTS = ("Resource Han Rounded CN", "Maple Mono NF CN")
FALLBACK_FONTS = ('"Segoe UI"', '"Microsoft YaHei UI"', '"Microsoft YaHei"', '"PingFang SC"',
                  '"Noto Sans CJK SC"', '"Malgun Gothic"', '"Apple SD Gothic Neo"', "sans-serif")


def font_stack(names=None):
    """配置的字体名（逗号分隔的字符串或列表）拼成 CSS font-family。

    名字里的引号、分号、花括号、尖括号去掉：这串字直接写进样式表，不能让它改坏样式或跳出 <style>。
    """
    if names is None or (isinstance(names, str) and not names.strip()):
        names = DEFAULT_FONTS
    if isinstance(names, str):
        names = names.replace("，", ",").split(",")
    out = []
    for n in names:
        n = " ".join(re.sub(r"[\"'\\;{}<>]", "", str(n)).split())
        if n and f'"{n}"' not in out:
            out.append(f'"{n}"')
    return ", ".join(out + [f for f in FALLBACK_FONTS if f not in out])


# ---------------------------------------------------------------------------
# 模板数据

def _estimate_height(d):
    """估卡片有多高（CSS 像素），本机出图按它开窗口；估少了 render_local 会自己再开高一次。"""
    per_line = CONTENT_WIDTH / 10
    h = 2 + 24 + 48 + 12 + 40 + 4 + 28 + 2 * PAGE_PAD
    if d["body_text"]:
        h += estimate_lines(d["body_text"], per_line) * 29 + 12
    if d["truncated"]:
        h += 26
    if d["media"]:
        h += 12 + CONTENT_WIDTH * d["media"]["ratio"]
    if d["poll"]:
        h += 12 + 44 * len(d["poll"]["choices"]) + 28
    q = d["quote"]
    if q:
        h += 12 + 24 + 22 + 12
        if q["text"]:
            h += 4 + estimate_lines(q["text"], CONTENT_WIDTH / 7.5) * 22
        if q["media"]:
            h += CONTENT_WIDTH * 0.5625
    return int(h * 1.15) + 120


async def x_card_data(client, card, theme, *, now=None, fonts=None):
    """把 fetchers 的推特卡片字典整理成 x_card.html 的模板变量。

    client 是 httpx.AsyncClient（下载头像和缩略图）；theme 是 light / dark，别的值（auto、None）按北京时间。
    拿不到的字段模板里整块不画。fonts 是配置里填的系统字体名（见 font_stack）。
    """
    now = now or datetime.now(timezone.utc)
    c = card or {}
    author = c.get("author") or {}
    name = str(author.get("name") or "").strip()
    handle = _handle(author.get("handle") or author.get("screen_name"))

    # 正文：有译文就只画译文（用户 2026-11-20 截图：译文直接替代原文，不写“翻译自”）
    raw = tidy(c.get("translation")) or tidy(c.get("text"))
    body, truncated = fit_text(raw, BODY_MAX_LINES, CONTENT_WIDTH / 10)

    referer = "https://x.com/"
    videos = video_items(c)  # 主推文的照片不画（另附），只画视频、动图
    q = c.get("quote") if isinstance(c.get("quote"), dict) else None
    q_items = (photo_items(q) + video_items(q, legacy=False)) if q else []

    jobs = [render._image_info(client, author.get("avatar"), referer),
            _download(client, videos[:MEDIA_MAX], referer)]
    if q:
        jobs.append(render._image_info(client, q.get("avatar"), referer))
        jobs.append(_download(client, q_items[:MEDIA_MAX], referer))
    res = await asyncio.gather(*jobs)

    quote = None
    if q:
        qname, qhandle = _quote_author(q)
        qtext = tidy(q.get("translation")) or tidy(q.get("text"))
        qtext, _ = fit_text(qtext, QUOTE_MAX_LINES, CONTENT_WIDTH / 7.5)
        quote = {
            "name": qname, "handle": qhandle, "avatar": res[2][0], "badge": badge_kind(q.get("verification")),
            "date": short_date(_to_datetime(q.get("timestamp") or q.get("time")), now),
            "text": qtext, "paras": paragraphs(qtext) if qtext else [],
            "media": media_grid(res[3], len(q_items), single_ratio=False),
        }

    published = _to_datetime(c.get("timestamp") or c.get("time"))
    views = (_stat_text(c, "views", "浏览"))
    d = {
        "theme": _theme(theme),
        "width": CARD_WIDTH,
        "author": {"name": name, "handle": handle, "avatar": res[0][0],
                   "badge": badge_kind(author.get("verification"))},
        "body_text": body,
        "paras": paragraphs(body) if body else [],
        "truncated": truncated,
        "media": media_grid(res[1], len(videos)),
        "poll": poll_data(c.get("poll"), now),
        "quote": quote,
        "time": x_stamp(published),
        "time_parts": x_stamp(published).split(" · ") if published else [],
        "views": views or "",
        "actions": actions(c),
    }
    d["height"] = min(MAX_WINDOW_HEIGHT, _estimate_height(d))
    d["font_family"] = font_stack(fonts)
    return d


_env = jinja2.Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)


def build_x_html(data):
    """本机渲染模板（文转图那条路由 AstrBot 自己套模板）。"""
    return _env.from_string(TEMPLATE.read_text(encoding="utf-8")).render(**data)

# ---------------------------------------------------------------------------
# 本机出图


class XCardRenderError(RuntimeError):
    """本机出图失败（找不到浏览器、超时、没出图）。调用方接住后退回文转图。"""


def _version_key(name):
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return ()


def find_browser():
    """找本机的 Edge，找不到再找 Chrome；都没有返回 None。

    Windows 上 Edge 新的安装方式会放在 EdgeCore\\<版本号>\\ 下（这台机器就是），按版本号从新到旧找。
    """
    cands = []
    if os.name == "nt":
        bases = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        bases = [Path(b) for b in bases if b]
        for b in bases:
            cands.append(b / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        for b in bases:
            core = b / "Microsoft" / "EdgeCore"
            if core.is_dir():
                vers = sorted((p for p in core.iterdir() if p.is_dir() and _version_key(p.name)),
                              key=lambda p: _version_key(p.name), reverse=True)
                cands += [v / "msedge.exe" for v in vers]
        for b in bases:
            cands.append(b / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for p in cands:
        if p.is_file():
            return str(p)
    for exe in ("msedge", "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                "chromium", "chromium-browser", "chrome"):
        found = shutil.which(exe)
        if found:
            return found
    return None


_PNG_END = b"IEND\xaeB`\x82"


def _png_done(path):
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            f.seek(-8, os.SEEK_END)
            return f.read(8) == _PNG_END
    except OSError:
        return False


def _browser_args(exe, html_path, png, profile, width, height, scale):
    return [exe, "--headless", "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update",
            f"--user-data-dir={profile}", f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}", f"--screenshot={png}", Path(html_path).resolve().as_uri()]


async def _kill_leftovers(profile):
    """超时时结束还在跑的浏览器进程：只认命令行里带这次独有的用户目录的进程（都是这次启动的），按 PID 结束。

    Edge 的 msedge.exe 在有的安装方式下只是启动器，真正截图的进程是它另起的，PID 不是我们拿到的那个。
    """
    marker = str(profile)
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                  "$_.CommandLine.Contains($env:XCARD_MARKER) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", ps,
                env=dict(os.environ, XCARD_MARKER=marker),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 15)
        else:
            proc = await asyncio.create_subprocess_exec("ps", "-eo", "pid=,args=", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            for line in out.decode("utf-8", "replace").splitlines():
                pid, _, args = line.strip().partition(" ")
                if marker in args and pid.isdigit() and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), 9)
                    except OSError:
                        pass
    except Exception as e:  # noqa: BLE001 - 收尾失败只记日志
        logger.warning(f"[LinkPreview] 结束超时的浏览器进程失败：{e}")


async def _shoot(args, png, log_path, deadline):
    """起浏览器截图，等到 PNG 写完。启动器可能先退出（Edge 转交给子进程），所以以文件写完为准。"""
    try:
        Path(png).unlink()
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    with open(log_path, "wb") as log:
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=log)
        except OSError as e:
            raise XCardRenderError(f"启动浏览器失败：{e}") from e
    exited_at = None
    try:
        while True:
            if _png_done(png):
                break
            if proc.returncode is not None and exited_at is None:
                exited_at = loop.time()
            if exited_at is not None and proc.returncode != 0 and loop.time() - exited_at > 2:
                tail = ""
                try:
                    tail = " ".join(Path(log_path).read_bytes()[-300:].decode("utf-8", "replace").split())
                except OSError:
                    pass
                raise XCardRenderError(f"浏览器退出了但没出图（退出码 {proc.returncode}）{('：' + tail) if tail else ''}")
            if loop.time() > deadline:
                raise XCardRenderError("浏览器超时没出图")
            try:
                await asyncio.wait_for(proc.wait(), 0.1)
            except asyncio.TimeoutError:
                pass
        # 出图了：浏览器一般随后自己退出，等一会儿让它把文件句柄放掉
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _content_box(png, threshold=10):
    """和左上角底色不同的区域；返回 (包围盒, 是否碰到图的底边)。"""
    from PIL import Image, ImageChops

    with Image.open(png) as im:
        im = im.convert("RGB")
        bg = im.getpixel((2, 2))
        diff = ImageChops.difference(im, Image.new("RGB", im.size, bg)).convert("L")
        box = diff.point(lambda v: 255 if v > threshold else 0).getbbox()
        return box, bool(box and box[3] >= im.size[1] - 2)


def crop_png(png, box):
    """裁到卡片边框（不留底色），仍存 PNG。"""
    from PIL import Image

    if not box:
        return
    with Image.open(png) as im:
        out = im.convert("RGB").crop(box)
    out.save(png, "PNG", optimize=False)


async def _remove_dir(path, wait=5.0):
    """删浏览器用户目录。Edge 转交出去的截图进程在图写完后还要零点几秒才退出、松开目录，所以隔一会儿重试；
    到点还删不掉就留给下次的 _sweep。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + wait
    while True:
        await asyncio.to_thread(shutil.rmtree, path, True)
        if not Path(path).exists() or loop.time() > end:
            return
        await asyncio.sleep(0.2)


def _sweep(cache_dir, max_age=600):
    """清掉之前没收拾干净的临时用户目录、HTML 和日志（超过十分钟的）。PNG 归调用方管。"""
    now = time.time()
    try:
        entries = list(Path(cache_dir).glob("xcard-*"))
    except OSError:
        return
    for p in entries:
        if p.suffix == ".png":
            continue
        try:
            if now - p.stat().st_mtime < max_age:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            pass


async def render_local(html, cache_dir, *, height=1600, scale=2, timeout=30.0, browser=None):
    """用本机 Edge / Chrome 无界面截图出 PNG，裁到卡片边框，返回 PNG 路径。

    窗口宽 = 卡片宽 + 两边底色，设备像素比 scale（默认 2，出图约 1200 像素宽）。height 是估的窗口高
    （x_card_data 的 height），内容碰到窗口底边就按最大高度再截一次。临时 HTML、浏览器用户目录、日志
    都写在 cache_dir，用完删掉。找不到浏览器、超时、浏览器没出图都抛 XCardRenderError。
    """
    exe = browser or find_browser()
    if not exe:
        raise XCardRenderError("本机找不到 Edge 或 Chrome，没法本地出图")
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_sweep, cache)
    tag = uuid.uuid4().hex[:12]
    html_path = cache / f"xcard-{tag}.html"
    png = cache / f"xcard-{tag}.png"
    profile = cache / f"xcard-prof-{tag}"
    log_path = cache / f"xcard-{tag}.log"
    html_path.write_text(html, encoding="utf-8")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    width = CARD_WIDTH + 2 * PAGE_PAD
    h = max(400, min(int(height), MAX_WINDOW_HEIGHT))
    ok = False
    try:
        while True:
            try:
                await _shoot(_browser_args(exe, html_path, png, profile, width, h, scale), png, log_path, deadline)
            except XCardRenderError as e:
                if "超时" in str(e):
                    await _kill_leftovers(profile)
                    raise XCardRenderError(f"浏览器 {timeout:.0f} 秒内没出图") from None
                raise
            box, touches = await asyncio.to_thread(_content_box, png)
            if touches and h < MAX_WINDOW_HEIGHT:
                h = MAX_WINDOW_HEIGHT
                continue
            if not box:
                raise XCardRenderError("浏览器出的图是空白的")
            if touches:
                logger.warning("[LinkPreview] 推特卡片超过本机出图的最大高度，底部被截掉")
            await asyncio.to_thread(crop_png, png, box)
            ok = True
            return png
    finally:
        await _remove_dir(profile)
        # 日志是浏览器继承的 stderr，要等它退出（上面删目录时已经等过）才删得掉；删不掉留给 _sweep
        for p in (html_path, log_path):
            try:
                p.unlink()
            except OSError:
                pass
        if not ok:
            try:
                png.unlink()
            except OSError:
                pass


def _bottom_has_content(png, rows=4, threshold=10):
    """截图最下面几行和左上角底色不一样，说明内容可能被窗口截断了。"""
    from PIL import Image, ImageChops

    with Image.open(png) as im:
        im = im.convert("RGB")
        w, h = im.size
        bg = im.getpixel((2, 2))
        strip = im.crop((0, max(0, h - rows), w, h))
    diff = ImageChops.difference(strip, Image.new("RGB", strip.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > threshold else 0).getbbox() is not None


async def shoot_page(html, cache_dir, *, width, height=1600, scale=2, timeout=30.0, browser=None):
    """通用卡片、长文页面的本机截图：窗口宽 width，先按 height 截，内容碰到底边就按最大高度再截一次。

    不裁边（模板底色是纯色，调用方用 render.crop_image 按底色裁）。失败抛 XCardRenderError。
    """
    exe = browser or find_browser()
    if not exe:
        raise XCardRenderError("本机找不到 Edge 或 Chrome，没法本地出图")
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_sweep, cache)
    tag = uuid.uuid4().hex[:12]
    html_path = cache / f"page-{tag}.html"
    png = cache / f"page-{tag}.png"
    profile = cache / f"page-prof-{tag}"
    log_path = cache / f"page-{tag}.log"
    html_path.write_text(html, encoding="utf-8")
    deadline = asyncio.get_running_loop().time() + timeout
    h = max(400, min(int(height), MAX_WINDOW_HEIGHT))
    ok = False
    try:
        while True:
            try:
                await _shoot(_browser_args(exe, html_path, png, profile, width, h, scale), png, log_path, deadline)
            except XCardRenderError as e:
                if "超时" in str(e):
                    await _kill_leftovers(profile)
                    raise XCardRenderError(f"浏览器 {timeout:.0f} 秒内没出图") from None
                raise
            if h < MAX_WINDOW_HEIGHT and await asyncio.to_thread(_bottom_has_content, png):
                h = MAX_WINDOW_HEIGHT
                continue
            ok = True
            return png
    finally:
        await _remove_dir(profile)
        for f in (html_path, log_path):
            try:
                f.unlink()
            except OSError:
                pass
        if not ok:
            try:
                png.unlink()
            except OSError:
                pass


async def render_card(client, card, cache_dir, theme=None, fonts=None):
    """插件用的入口：整理数据、套模板、本机出图，返回 PNG 路径。失败抛异常，调用方退回通用卡片。"""
    data = await x_card_data(client, card, theme or theme_now(), fonts=fonts)
    return await render_local(build_x_html(data), Path(cache_dir) / "xcard", height=data.get("height", 1600))
