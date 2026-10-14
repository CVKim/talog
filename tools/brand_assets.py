"""브랜드 자산 생성 — docs/brand/aivex_logo.ai(AIVEX 로고, Illustrator PDF 호환) → talog/assets/.

  python tools/brand_assets.py            (개발 PC 전용: pymupdf·Pillow 필요, 실행 파일에는 결과물만 들어간다)

.ai 의 대지 0(주황, 투명 배경)에서 로고 경로 7개를 읽어 여백 없이 잘라 낸 깔끔한 SVG 로 옮긴다.
로고는 손대지 않는다 — 글자를 떼어 쓰지 않고 워드마크 전체를 쓴다.

아이콘은 원본 대지 1(진회색 #1D1D1D 바탕 + 주황 로고)의 배색을 정사각 타일로 옮긴 것이다. 정사각 칸에
가로로 긴 워드마크를 넣으므로 16~32px 에서는 워드마크를 타일 폭 가득(여백 최소) 키운다.

출력 (talog/assets/)
  aivex_wordmark.svg / _dark.svg / _white.svg   로고 (주황 #FF5900 / 진회색 #1D1D1D / 흰색, 여백 없음)
  talog_icon.svg                                 앱 아이콘 (진회색 타일 + 주황 로고)
  talog.ico                                      16~256px 다중 크기
  talog_256.png, talog_32.png                    문서·파비콘용
"""

from __future__ import annotations

import os
import sys

import pymupdf
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "brand", "aivex_logo.ai")
OUT = os.path.join(ROOT, "talog", "assets")

ORANGE, DARK, WHITE = "#FF5900", "#1D1D1D", "#FFFFFF"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _path_d(item, dx: float = 0.0, dy: float = 0.0, k: float = 1.0) -> str:
    """get_drawings() 요소 하나 → SVG path d (M/L/C/H/V/Z, 절대 좌표). (x−dx)·k 로 옮기고 키운다."""
    def P(x, y):
        return f"{_f((x - dx) * k)} {_f((y - dy) * k)}"

    out, cur = [], None
    for seg in item["items"]:
        op = seg[0]
        if op == "re":
            r = seg[1]
            out.append(f"M{P(r.x0, r.y0)}H{_f((r.x1 - dx) * k)}V{_f((r.y1 - dy) * k)}H{_f((r.x0 - dx) * k)}Z")
            cur = None
            continue
        pts = seg[1:]
        if cur is None or abs(cur.x - pts[0].x) > 1e-3 or abs(cur.y - pts[0].y) > 1e-3:
            out.append("M" + P(pts[0].x, pts[0].y))
        if op == "l":
            out.append("L" + P(pts[1].x, pts[1].y))
            cur = pts[1]
        elif op == "c":
            out.append("C" + " ".join(P(p.x, p.y) for p in pts[1:4]))
            cur = pts[3]
        else:
            raise ValueError(f"지원하지 않는 경로 요소: {op}")
    if out and not out[-1].endswith("Z"):
        out.append("Z")
    return "".join(out)


def _bbox(items):
    rs = [it["rect"] for it in items]
    return (min(r.x0 for r in rs), min(r.y0 for r in rs), max(r.x1 for r in rs), max(r.y1 for r in rs))


def wordmark_svg(items, color: str) -> str:
    """로고 그대로 — 여백 없이 경로 경계에 맞춘 viewBox."""
    x0, y0, x1, y1 = _bbox(items)
    paths = "".join(f'<path d="{_path_d(it, x0, y0)}"/>' for it in items)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(x1 - x0)} {_f(y1 - y0)}" '
            f'role="img" aria-label="AIVEX"><title>AIVEX</title><g fill="{color}">{paths}</g></svg>')


def icon_svg(items, fill: float = 0.80, radius: float = 0.2, side: float = 100.0) -> str:
    """진회색 둥근 정사각 타일 + 가운데 주황 로고. fill = 타일 폭 대비 로고 폭."""
    x0, y0, x1, y1 = _bbox(items)
    w, h = x1 - x0, y1 - y0
    k = side * fill / w
    ox = (side - w * k) / 2
    oy = (side - h * k) / 2
    paths = "".join(f'<path d="{_path_d(it, x0 - ox / k, y0 - oy / k, k)}"/>' for it in items)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(side)} {_f(side)}" role="img" '
            f'aria-label="talog"><title>talog · AIVEX</title>'
            f'<rect width="{_f(side)}" height="{_f(side)}" rx="{_f(side * radius)}" fill="{DARK}"/>'
            f'<g fill="{ORANGE}">{paths}</g></svg>')


def render(svg: str, px: int) -> Image.Image:
    """SVG → px×px RGBA. 4배로 그린 뒤 줄여 가장자리를 매끈하게 한다."""
    doc = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")
    pg = doc[0]
    z = px * 4 / pg.rect.width
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=True)
    im = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    return im.resize((px, px), Image.LANCZOS)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not os.path.exists(SRC):
        print(f"원본이 없습니다: {SRC}")
        return 1
    os.makedirs(OUT, exist_ok=True)
    items = pymupdf.open(SRC)[0].get_drawings()
    if len(items) != 7:
        print(f"! 경로가 {len(items)}개 — AIVEX 로고(7개)와 다릅니다. 원본을 확인하십시오.")
        return 1
    for name, col in (("aivex_wordmark", ORANGE), ("aivex_wordmark_dark", DARK),
                      ("aivex_wordmark_white", WHITE)):
        with open(os.path.join(OUT, f"{name}.svg"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(wordmark_svg(items, col) + "\n")
    big = icon_svg(items)
    with open(os.path.join(OUT, "talog_icon.svg"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(big + "\n")
    small = icon_svg(items, fill=0.92, radius=0.16)          # 작은 크기: 로고를 타일 폭 가득
    frames = [render(small if px <= 32 else big, px) for px in ICO_SIZES]
    frames[-1].save(os.path.join(OUT, "talog.ico"), format="ICO",
                    sizes=[(px, px) for px in ICO_SIZES], append_images=frames[:-1])
    render(big, 256).save(os.path.join(OUT, "talog_256.png"))
    render(small, 32).save(os.path.join(OUT, "talog_32.png"))
    with Image.open(os.path.join(OUT, "talog.ico")) as ico:
        got = sorted(ico.info.get("sizes", set()))
    print(f"[brand] {OUT} — 로고 3색, talog_icon.svg, talog.ico {got}, PNG 2개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
