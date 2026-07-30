"""사례 지식베이스(Case KB) — 과거 RCA 사례를 축적하고 유사 사례를 검색한다.

저장: <kb_dir>/cases.jsonl (1줄 = 1사례)
    {"id","date","site","title","symptom","cause","action","tags":[...]}
검색: Ollama 임베딩(nomic-embed-text, 로컬)이 있으면 코사인 유사도,
      없으면 키워드 중첩 점수 폴백 — 어느 환경에서도 동작한다.
임베딩 캐시: <kb_dir>/emb_cache.json (텍스트 해시로 무효화)

주의: 사례에는 현장 정보가 포함되므로 kb/ 는 git 에 커밋하지 않는다
(.gitignore 처리, 템플릿은 kb_template.jsonl 참조).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from dataclasses import dataclass, field

_OLLAMA = "http://localhost:11434"
_EMB_MODEL = "nomic-embed-text"


@dataclass(slots=True)
class Case:
    id: str
    title: str
    symptom: str
    cause: str = ""
    action: str = ""
    site: str = ""
    date: str = ""
    tags: list = field(default_factory=list)

    def text(self) -> str:
        return f"{self.title}\n증상: {self.symptom}\n원인: {self.cause}\n" \
               f"조치: {self.action}\n태그: {', '.join(self.tags)}"


def default_kb_dir() -> str:
    return os.environ.get("TALOG_KB_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kb")


class CaseKB:
    def __init__(self, kb_dir: str = ""):
        self.dir = kb_dir or default_kb_dir()
        self.path = os.path.join(self.dir, "cases.jsonl")
        self.cache_path = os.path.join(self.dir, "emb_cache.json")
        self.cases: list[Case] = []
        self._cache: dict = {}
        self._load()

    # ── 저장/로드 ─────────────────────────────────────────────
    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            j = json.loads(line)
                            if not isinstance(j, dict):
                                continue
                            self.cases.append(Case(
                                id=str(j.get("id", "")),
                                title=str(j.get("title", "")),
                                symptom=str(j.get("symptom", "")),
                                cause=str(j.get("cause", "")),
                                action=str(j.get("action", "")),
                                site=str(j.get("site", "")),
                                date=str(j.get("date", "")),
                                tags=list(j.get("tags") or [])))
                        except (json.JSONDecodeError, TypeError):
                            continue
        except OSError:
            pass
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    c = json.load(f)
                self._cache = c if isinstance(c, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._cache = {}

    def add(self, case: Case):
        os.makedirs(self.dir, exist_ok=True)
        if not case.id:
            case.id = f"case-{len(self.cases) + 1:04d}"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": case.id, "date": case.date, "site": case.site,
                "title": case.title, "symptom": case.symptom,
                "cause": case.cause, "action": case.action, "tags": case.tags,
            }, ensure_ascii=False) + "\n")
        self.cases.append(case)      # 파일 기록 성공 후에만 메모리 반영

    # ── 임베딩 ────────────────────────────────────────────────
    @staticmethod
    def _embed_available() -> bool:
        try:
            with urllib.request.urlopen(f"{_OLLAMA}/api/tags", timeout=2) as r:
                return _EMB_MODEL in r.read().decode("utf-8", "replace")
        except OSError:
            return False

    @staticmethod
    def _embed(text: str) -> list[float]:
        req = urllib.request.Request(
            f"{_OLLAMA}/api/embeddings",
            data=json.dumps({"model": _EMB_MODEL, "prompt": text[:4000]})
            .encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8")).get("embedding", [])

    def _vec(self, case: Case) -> list[float]:
        h = hashlib.md5(case.text().encode("utf-8")).hexdigest()
        ent = self._cache.get(case.id)
        if isinstance(ent, dict) and ent.get("hash") == h \
                and isinstance(ent.get("vec"), list):
            return ent["vec"]
        vec = self._embed(case.text())
        self._cache[case.id] = {"hash": h, "vec": vec}
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
        except OSError:
            pass
        return vec

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    # ── 검색 ─────────────────────────────────────────────────
    def search(self, query: str, k: int = 3) -> list[tuple[float, Case]]:
        if not self.cases or not query.strip():
            return []
        if self._embed_available():
            try:
                qv = self._embed(query)
                scored = [(self._cos(qv, self._vec(c)), c) for c in self.cases]
                scored.sort(key=lambda x: -x[0])
                return [(s, c) for s, c in scored[:k] if s > 0.4]
            except Exception:
                pass     # 임베딩 실패 시 키워드 폴백으로 강등
        # 폴백: 키워드 중첩 점수 (2글자 이상 토큰)
        toks = {t for t in re.split(r"[^0-9A-Za-z가-힣_]+", query.lower())
                if len(t) >= 2}
        scored = []
        for c in self.cases:
            ct = c.text().lower()
            hit = sum(1 for t in toks if t in ct)
            if hit:
                scored.append((hit / max(len(toks), 1), c))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


# ── CLI (talog kb ...) ───────────────────────────────────────
def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="talog kb", description="RCA 사례 지식베이스")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="사례 추가")
    for f_ in ("title", "symptom", "cause", "action", "site", "date"):
        a.add_argument(f"--{f_}", default="")
    a.add_argument("--tags", default="", help="쉼표 구분")
    sub.add_parser("list", help="사례 목록")
    s = sub.add_parser("search", help="유사 사례 검색")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=3)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    kb = CaseKB()
    if args.cmd == "add":
        if not args.title or not args.symptom:
            print("--title 과 --symptom 은 필수입니다.")
            return 2
        try:
            kb.add(Case(id="", title=args.title, symptom=args.symptom,
                        cause=args.cause, action=args.action, site=args.site,
                        date=args.date,
                        tags=[t.strip() for t in args.tags.split(",")
                              if t.strip()]))
        except OSError as e:
            print(f"저장 실패: {e}")
            return 1
        print(f"저장: {kb.path} (총 {len(kb.cases)}건)")
    elif args.cmd == "list":
        for c in kb.cases:
            print(f"[{c.id}] ({c.site} {c.date}) {c.title}")
        print(f"총 {len(kb.cases)}건 — {kb.path}")
    elif args.cmd == "search":
        hits = kb.search(args.query, args.k)
        if not hits:
            print("유사 사례 없음")
        for score, c in hits:
            print(f"── 유사도 {score:.2f} [{c.id}] {c.title} ({c.site} {c.date})")
            print(f"   증상: {c.symptom}")
            print(f"   원인: {c.cause}")
            print(f"   조치: {c.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
