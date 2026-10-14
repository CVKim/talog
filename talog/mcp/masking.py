"""고객 값 가리기 — 12자리 이상 숫자(검사 inner id 등)는 ID-xxxxxx, 출처의 로그·데이터 경로는 <로그>·<데이터>.

같은 값은 늘 같은 토큰이 되므로(작업 폴더의 소금) Claude 가 사건 사이를 이어 볼 수 있고, 토큰 → 원래 값 대응표를
작업 폴더(.mask_map.json)에 남겨 Claude 가 받은 ID-xxxxxx 로 원본 로그를 다시 찾을 수 있다(이 PC 안에서만).
"""

from __future__ import annotations

import hashlib
import json
import os
import re

LONG_DIGITS = re.compile(r"(?<!\d)\d{12,}(?!\d)")


WIN_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'|<>*?]+")


class Masker:
    """고객 데이터 가리기. 같은 값은 같은 토큰이 되므로 Claude 가 사건 사이를 이어 볼 수 있다."""

    def __init__(self, on: bool, salt: str, roots: dict[str, str] | None = None, map_path: str = ""):
        self.on = on
        self.salt = salt
        # 긴 경로부터 바꾼다 (상위 경로가 하위 경로를 먼저 먹지 않게)
        self.roots = sorted(((k, v) for k, v in (roots or {}).items() if k), key=lambda kv: -len(kv[0]))
        # 토큰 → 원래 값. Claude 가 받은 ID-xxxxxx 로 원본 로그를 찾을 수 있게 작업 폴더에 남긴다 (이 PC 안에서만)
        self.map_path = map_path
        self.rev: dict[str, str] = {}
        self._dirty = False
        if map_path:
            try:
                with open(map_path, encoding="utf-8") as f:
                    self.rev = json.load(f)
            except (OSError, ValueError):
                self.rev = {}

    def add_root(self, root: str, alias: str):
        if root and all(r != root for r, _a in self.roots):
            self.roots = sorted(self.roots + [(root, alias)], key=lambda kv: -len(kv[0]))

    def token(self, digits: str) -> str:
        tok = "ID-" + hashlib.sha1((self.salt + digits).encode("utf-8")).hexdigest()[:6]
        if self.rev.get(tok) != digits and len(self.rev) < 200000:
            self.rev[tok] = digits
            self._dirty = True
        return tok

    def unmask(self, s: str) -> str:
        """ID-xxxxxx 토큰을 원래 값으로 (모르는 토큰은 그대로)."""
        return re.sub(r"ID-[0-9a-f]{6}", lambda m: self.rev.get(m.group(0), m.group(0)), s or "")

    def save(self):
        if self._dirty and self.map_path:
            try:
                tmp = self.map_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.rev, f)
                os.replace(tmp, self.map_path)
                self._dirty = False
            except OSError:
                pass

    def text(self, s) -> str:
        if not isinstance(s, str):
            return s
        if not self.on:
            return s
        for root, alias in self.roots:
            for variant in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
                if variant and variant in s:
                    s = s.replace(variant, alias)
        s = WIN_PATH.sub(lambda m: "<경로>\\" + re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], s)
        return LONG_DIGITS.sub(lambda m: self.token(m.group(0)), s)

    def obj(self, o):
        if not self.on:
            return o
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        return o
