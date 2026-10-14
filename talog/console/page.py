"""콘솔 페이지 조립 — talog/web/ 의 소스를 브라우저가 받을 HTML 파일 하나로 합친다.

브라우저가 받는 페이지는 늘 파일 하나·외부 리소스 없음이다(현장 PC 오프라인). 소스만 나눠 둔다:
  web/console.html        껍데기(head·사이드바·위 막대). 아래 표시 줄이 그 자리에 들어간다
    /* @@css */             → web/css/*.css 를 이름순으로
    /* @@js */              → web/js/*.js 를 이름순으로 (한 <script> 안 — 앞 파일의 const·function 을 뒤 파일이 쓴다)
    <!-- @@include 경로 -->  → web/ 안의 그 파일 (views/*.html 화면, partials/*.html 아이콘·대화상자)
파일 이름 앞 숫자가 순서다(00-tokens.css → 90-responsive.css). 표시 줄은 줄 하나를 통째로 차지해야 한다.
소스가 바뀌면 다음 요청 때 다시 합친다 — 콘솔을 다시 띄우지 않아도 새로고침으로 보인다.
"""

from __future__ import annotations

import glob
import os
import re
import threading

from .util import WEB

_MARK = re.compile(r"^(?:<!-- @@include ([\w./-]+) -->|/\* @@(css|js) \*/)\n", re.M)
_cache: dict = {"key": None, "page": ""}
_lock = threading.Lock()


def _read(path: str) -> str:
    # 보편 줄바꿈으로 읽는다 — Windows 에서 받은 사본(core.autocrlf)은 CRLF 라 표시 줄 뒤가 '\r\n' 이다
    with open(path, encoding="utf-8") as f:
        return f.read()


def sources(web: str = WEB) -> list[str]:
    """조립에 쓰는 파일 전부 (바뀜 감지·테스트용)."""
    out = []
    for root, _dirs, files in os.walk(web):
        out += [os.path.join(root, f) for f in files if f.endswith((".html", ".css", ".js"))]
    return sorted(out)


def assemble(web: str = WEB) -> str:
    """web/ 소스 → 페이지 HTML 한 덩어리. 없는 include·web 밖 경로는 바로 실패한다."""
    def rep(m: re.Match) -> str:
        if m.group(1):
            rel = m.group(1)
            p = os.path.normpath(os.path.join(web, rel))
            if ".." in rel.split("/") or not p.startswith(os.path.normpath(web) + os.sep):
                raise ValueError(f"web 밖 include: {rel}")
            return _read(p)
        kind = m.group(2)
        return "".join(_read(f) for f in sorted(glob.glob(os.path.join(web, kind, f"*.{kind}"))))
    return _MARK.sub(rep, _read(os.path.join(web, "console.html")))


def build_page(web: str = WEB) -> str:
    """조립한 페이지 (소스가 그대로면 지난번 결과)."""
    files = sources(web)
    key = (web, tuple((f, os.path.getmtime(f)) for f in files))
    with _lock:
        if _cache["key"] != key:
            _cache["page"] = assemble(web)
            _cache["key"] = key
        return _cache["page"]
