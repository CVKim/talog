"""콘솔 색 대비 계산 (WCAG 2.x 상대 휘도) — 글자 4.5:1, 비텍스트(초점 링·상태 점·입력 테두리·아이콘) 3:1.

토큰 값은 talog/web/css/00-tokens.css 에서 직접 읽는다 — 밝게(:root)와 어둡게(:root[data-theme="dark"])
두 벌을 같은 쌍으로 계산한다. 색을 새로 쓰거나 바꾸면 쓰는 (글자, 바탕) 쌍을 아래 표에 더하고 돌린다.

    python tools/contrast_check.py          # 미달이 있으면 종료 코드 1
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS = os.path.join(ROOT, "talog", "web", "css", "00-tokens.css")

# 토큰이 아닌 고정색 (화면 코드에 그대로 쓰인 것) — '#fff' 는 진한 바탕 위 흰 글자, tagai 는 사이드바 AI 표시
FIXED = {"#fff": "#ffffff", "tagai": "#ff9259"}

TEXT = [  # (글자, 바탕) — 4.5:1
    ("ink", "bg"), ("ink", "surface"), ("ink-2", "surface"), ("sub", "surface"), ("sub", "bg"), ("sub", "sunken"),
    ("mute", "surface"), ("mute", "bg"), ("mute", "surface-2"), ("mute", "sunken"),
    ("brand-ink", "surface"), ("brand-ink", "brand-soft"), ("on-primary", "primary"),
    ("crit-ink", "surface"), ("crit-ink", "crit-soft"), ("warn-ink", "surface"), ("warn-ink", "warn-soft"),
    ("ok-ink", "surface"), ("ok-ink", "ok-soft"), ("info-ink", "surface"), ("info-ink", "info-soft"),
    ("side-ink", "side-bg"), ("side-mute", "side-bg"), ("#fff", "side-active"), ("tagai", "side-bg"),
    ("#fff", "crit-solid"), ("ink", "brand-soft"),
    # v2.2 스킬 카드·대화상자: 재현 확인 칸(꺼짐 surface-2 / 켜짐 warn-soft)의 글자
    ("ink-2", "surface-2"), ("sub", "surface-2"), ("ink-2", "warn-soft"), ("sub", "warn-soft"),
    # v2.2 캡션·도움말: 요약 줄(위험·주의·기본 바탕), 풀이 예시 칸, 용어 사전 강조 줄
    ("ink-2", "crit-soft"), ("ink-2", "sunken"), ("sub", "brand-soft"), ("ink", "surface-2"),
    # v2.2 모드: 왼쪽 위 전환(꺼짐 side-mute · 마우스 올림 side-ink/side-hover · 지금 #fff/side-active), 모드 카드
    # (고른 칸 ink·sub/brand-soft, 마우스 올림 surface-2), PIN 오류 글
    ("side-ink", "side-hover"), ("sub", "surface-2"), ("crit-ink", "surface"),
]
NONTEXT = [  # (요소, 바탕) — 3:1
    ("focus", "surface"), ("focus", "bg"), ("brand", "surface"), ("crit", "surface"), ("warn", "surface"),
    ("ok", "surface"), ("info", "surface"), ("line-strong", "surface"), ("line-strong", "bg"), ("primary", "surface"),
    ("brand", "side-bg"), ("crit", "sunken"), ("warn", "sunken"),
    ("brand-ink", "warn-soft"),  # v2.2 재현 확인 스위치(켜짐) 바탕
    # v2.2 모드 카드의 아이콘 칸 (현장 sub/sunken · 운영자 ok-ink/ok-soft · 개발자 info-ink/info-soft), 고른 칸 테두리
    ("sub", "sunken"), ("ok-ink", "ok-soft"), ("info-ink", "info-soft"), ("brand-ink", "brand-soft"),
    ("brand-ink", "surface"),
]


def _block(css: str, head: str) -> dict:
    i = css.index(head)
    j = css.index("{", i) + 1
    return dict(re.findall(r"--([\w-]+):\s*([^;}]+)", css[j:css.index("}", j)]))


def themes(path: str = TOKENS) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        css = f.read()
    light = _block(css, ":root{")
    return {"light": light, "dark": {**light, **_block(css, ':root[data-theme="dark"]{')}}


def _rgb(v: str) -> tuple[float, float, float, float]:
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.match(r"rgba?\(([^)]+)\)", v)
    if not m:
        raise ValueError(f"색이 아님: {v}")
    p = [float(x) for x in m.group(1).split(",")]
    return p[0], p[1], p[2], (p[3] if len(p) > 3 else 1.0)


def color(t: dict, name: str, under: str = "") -> tuple[float, float, float]:
    """토큰 → 불투명 RGB. 반투명(rgba)은 under 위에 섞는다 (사이드바 현재 메뉴 = side-active 를 side-bg 위에)."""
    r, g, b, a = _rgb(FIXED.get(name) or t[name])
    if a < 1:
        br, bg_, bb = color(t, under or "side-bg")
        r, g, b = (r * a + br * (1 - a), g * a + bg_ * (1 - a), b * a + bb * (1 - a))
    return r, g, b


def lum(rgb) -> float:
    out = []
    for c in rgb:
        c /= 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def ratio(a, b) -> float:
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    bad = 0
    for mode, t in themes().items():
        print(f"\n[{mode}]")
        for kind, pairs, need in (("글자", TEXT, 4.5), ("비텍스트", NONTEXT, 3.0)):
            for fg, bg in pairs:
                r = ratio(color(t, fg, bg), color(t, bg))
                ok = r >= need
                bad += not ok
                print(f"  {'OK ' if ok else 'BAD'} {kind:4} {fg:12} on {bg:12} {r:5.2f}:1 (기준 {need})")
    print("\n결과:", "모두 통과" if not bad else f"{bad}건 미달")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
