"""推特卡片出样张：给推文 ID，取 fxtwitter 数据，画浅色、深色各一张 PNG。

用法（在插件目录下）：
    .venv/Scripts/python.exe tools/sample_xcard.py --out <目录> [推文ID ...]
不给 ID 就画内置的几条公开推文。--proxy 默认 http://127.0.0.1:7897，--no-proxy 直连。

卡片字典用 fetchers._x_card 拼，和插件线上一样。
翻译用 --translation ID=译文 传假的译文（样张只看版式）。
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._stub import install  # noqa: E402

install()

import httpx  # noqa: E402

import fetchers  # noqa: E402
import xcard  # noqa: E402

SAMPLES = [
    ("1585841080431321088", "蓝标大号、往年的推文"),
    ("2123665147105276072", "日文、金标、需翻译（假译文）"),
    ("2123502343752986746", "带视频、灰标"),
    ("2122776652807905338", "被引推文（引用框里带视频）"),
    ("2123372494992142582", "纯文字长文 4500 字 + 被引推文"),
    ("2122996946017169459", "带三张图（卡片里不画照片）"),
    ("2123424019022299469", "中文、表情"),
]

FAKE_TRANSLATIONS = {
    "2123665147105276072": (
        "为纪念宝可梦 30 周年，ANA 的第三架“宝可梦喷气机”——“宝可梦喷气机 蓝”预计于 12 月 12 日（周六）首航！\n"
        "机身上画着水属性冒险的第一批伙伴们。\n"
        "想了解“宝可梦喷气机 赤·绿·蓝”，请看这里！\n"
        "https://www.ana.co.jp/ja/jp/promotion/pokemonjet/ #ポケモンジェット #AllNipponAirways #ANA #ポケモン30周年"
    ),
}


async def build_card(client, tid, translation=None):
    url = f"https://x.com/i/status/{tid}"
    card = await fetchers._x_card(client, url, tid)
    if translation:
        card["translation"] = translation
    return card


async def main():
    ap = argparse.ArgumentParser(description="推特卡片出样张")
    ap.add_argument("ids", nargs="*", help="推文 ID；不给就画内置样本")
    ap.add_argument("--out", required=True, help="PNG 输出目录")
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--theme", choices=("both", "light", "dark"), default="both")
    ap.add_argument("--translation", action="append", default=[], metavar="ID=译文")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "_cache"
    fakes = dict(FAKE_TRANSLATIONS)
    for item in args.translation:
        k, _, v = item.partition("=")
        fakes[k.strip()] = v.replace("\\n", "\n")
    ids = [(i, "") for i in args.ids] or SAMPLES
    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    proxy = None if args.no_proxy else args.proxy

    async with httpx.AsyncClient(proxy=proxy, timeout=30, follow_redirects=True,
                                 headers={"User-Agent": fetchers.UA}) as client:
        for tid, note in ids:
            try:
                card = await build_card(client, tid, fakes.get(tid))
            except Exception as e:  # noqa: BLE001
                print(f"{tid} 取数失败：{e}")
                continue
            for theme in themes:
                t0 = time.perf_counter()
                data = await xcard.x_card_data(client, card, theme)
                t1 = time.perf_counter()
                html = xcard.build_x_html(data)
                png = await xcard.render_local(html, cache, height=data["height"])
                t2 = time.perf_counter()
                dst = out / f"x-{tid}-{theme}.png"
                os.replace(png, dst)
                from PIL import Image
                with Image.open(dst) as im:
                    size = im.size
                print(f"{dst}  {size[0]}x{size[1]}  数据 {t1 - t0:.1f}s 出图 {t2 - t1:.1f}s  HTML {len(html) // 1024}KB  {note}")


if __name__ == "__main__":
    asyncio.run(main())
