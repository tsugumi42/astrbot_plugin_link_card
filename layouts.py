"""按平台把抓到的卡片字典拼成要发出去的消息。

这里只决定「发几条、每条里放什么、文字怎么排」，不下载、不出图、不发送。
返回值是消息列表，每条消息是若干段：
    ("card", None)        卡片图（由调用方出图后填进来）
    ("text", "文字")
    ("image", 图片地址)
    ("video", 视频地址)
    ("forward", [图片地址...])   图片太多时改成合并转发
排版照 Sumika 链接展开在测试群里定下的格式（2026-11-09、11-12）：
- 视频（B 站、油管、国内视频站）：封面，下面几行文字：标题 / 作者 | 时长 / 播放｜点赞 / 投稿时间 / 链接
- pixiv：首行 `[AI]🔞 | 标题 | 作者`，下面是原图
- X：卡片图 + 干净链接 + 原图同一条；有视频时卡片和链接一条，视频单独一条
"""

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:                                    # 作为包的一部分加载（AstrBot 正常路径）
    from .fetchers import CN_VIDEO, CST, HOST_ALIASES, TRACKING_PARAMS
except ImportError:                     # 测试里直接 import layouts
    from fetchers import CN_VIDEO, CST, HOST_ALIASES, TRACKING_PARAMS

VIDEO_PLATFORMS = frozenset({"youtube"}) | CN_VIDEO
LAYOUT_PLATFORMS = frozenset({"x", "pixiv"}) | VIDEO_PLATFORMS

# 每个平台只保留这些查询参数；不在表里的平台按通用追踪参数表去掉
KEEP_PARAMS = {
    "x.com": set(),                       # X 的 ?t= 是分享追踪码，不是时间点
    "youtube.com": {"v", "t", "start", "list", "index"},
    "bilibili.com": {"p", "t", "start_progress"},
    "pixiv.net": set(),
}
# 锚点里的时间点（#t=90）要留着，其余锚点去掉
TIME_FRAGMENT = re.compile(r"^t=\d")


def clean_link(url):
    """去掉追踪参数，保留时间点。给群友看的链接，不改路径和域名写法。"""
    try:
        u = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "")
    host = (u.hostname or "").lower()
    base = HOST_ALIASES.get(host, host).removeprefix("www.").removeprefix("m.")
    keep = KEEP_PARAMS.get(base)
    pairs = parse_qsl(u.query, keep_blank_values=True)
    if keep is None:
        pairs = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
    else:
        pairs = [(k, v) for k, v in pairs if k in keep]
    frag = u.fragment if TIME_FRAGMENT.match(u.fragment or "") else ""
    netloc = "x.com" if base == "x.com" else u.netloc
    return urlunsplit(("https", netloc, u.path.rstrip("/") or "/", urlencode(pairs), frag))


def short_time(value, now=None):
    """'2026-11-12 17:45' -> 今年写 '11-12 17:45'，往年写全。拿不到返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2}-\d{2})(?: (\d{2}:\d{2}))?$", s)
    if not m:
        return s
    year = (now or datetime.now(CST)).year
    tail = f"{m.group(2)} {m.group(3)}" if m.group(3) else m.group(2)
    return tail if int(m.group(1)) == year else f"{m.group(1)}-{tail}"


def _stat(card, name):
    for label, v in card.get("stats") or []:
        if label == name:
            return v
    return None


def video_text(card, link, now=None):
    """视频的文字部分：标题 / 作者 | 时长 / （分 P）/ 播放｜点赞 / 投稿时间 / 链接，行间不空行。"""
    lines = []
    if card.get("title"):
        lines.append(card["title"].strip())
    pages, page = card.get("pages") or [], card.get("page")
    if len(pages) > 1 and page and 1 <= page <= len(pages):
        part, dur = pages[page - 1]
        lines.append(f"P{page}/{len(pages)}：{part}")
    author = (card.get("author") or {}).get("name")
    dur = card.get("duration")
    if len(pages) > 1 and page and 1 <= page <= len(pages):
        dur = pages[page - 1][1] or dur
    elif len(pages) > 1:
        dur = f"共 {len(pages)}P" + (f"｜{card.get('total_duration')}" if card.get("total_duration") else "")
    head = " | ".join(x for x in (author, dur) if x)
    if head:
        lines.append(head)
    counts = "｜".join(f"{k} {v}" for k in ("播放", "点赞") if (v := _stat(card, k)))
    if counts:
        lines.append(counts)
    when = short_time(card.get("time"), now)
    if when:
        lines.append(when)
    lines.append(link)
    return "\n".join(lines)


def pixiv_head(card):
    """`[AI]🔞 | 标题 | 作者`：[AI] 只给 AI 作品，🔞 只给 R-18。"""
    mark = ("[AI]" if card.get("ai") else "") + ("🔞" if card.get("sensitive") else "")
    parts = [p for p in (card.get("title"), (card.get("author") or {}).get("name")) if p]
    head = " | ".join(parts)
    return f"{mark} | {head}" if mark and head else (mark or head)


def _with_images(parts, images, limit):
    """图片不超过上限就挤进同一条；超过就整组改成合并转发，单独一条。"""
    if not images:
        return [parts]
    if len(images) <= limit:
        return [parts + [("image", u) for u in images]]
    return [parts, [("forward", list(images))]]


def plan(card, image_limit=3, now=None):
    """按平台拼消息；不归这里管的平台返回 None，调用方照原样出卡片。"""
    platform = card.get("platform")
    link = clean_link(card.get("url"))
    if platform == "x":
        photos = card.get("photos") or []
        videos = card.get("videos") or []
        msgs = _with_images([("card", None), ("text", link)], photos, image_limit)
        msgs += [[("video", v["url"])] for v in videos if v.get("url")]
        return msgs
    if platform == "pixiv":
        return _with_images([("text", pixiv_head(card))], card.get("pages_images") or card.get("images") or [],
                            image_limit)
    if platform in VIDEO_PLATFORMS:
        cover = (card.get("images") or [None])[0]
        parts = [("image", cover)] if cover else []
        return [parts + [("text", video_text(card, link, now))]]
    return None
