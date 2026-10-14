"""MCP 도구 등록소 — 도구 하나 = 설명(spec) 하나 + 구현 함수 하나. tools/ 의 영역 파일에서 @tool 로 등록한다.

    @tool({"name": "overview", "title": "데이터 개요",
           "description": "처음 부르는 도구. …",
           "inputSchema": schema({"source": SOURCE}), "annotations": READ_ONLY})
    def overview(srv: TalogMCP, a: dict) -> str:
        return srv.mask.text("# …")

지킬 것
- 반환은 Claude 에게 줄 마크다운 글이다. 고객 값(검사 id·경로)이 들어갈 수 있으면 srv.mask.text(...) 로 가리고,
  길어질 수 있으면 cut(...) 으로 자른다(한 번 22,000자).
- 잘못된 인자·없는 대상은 ValueError(한국어 문장)로 알린다 — 서버가 isError 결과로 바꿔 Claude 에게 보인다.
- 파일을 쓰는 도구는 annotations=WRITES_LOCAL 로 표시한다. 콘솔 '바로 묻기'는 읽기 도구만 허용하고,
  스킬의 allowed-tools 에도 읽기 도구만 둔다(tests/test_mcp.py 가 확인).
- 도구 순서는 tools/__init__.py 의 import 순서다 — 클라이언트 캐시를 위해 바꾸지 않는다(새 도구는 끝에).
"""

from __future__ import annotations

from typing import Callable

STR = {"type": "string"}
SOURCE = {"type": "string", "description": "데이터 출처 이름 (overview 의 '출처' 열, 일부만 써도 됨). 비우면 전체"}
DAY = {"type": "string", "description": "날짜 YYYYMMDD (예: 20261012). 비우면 가장 최근"}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITES_LOCAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

TOOLS: list[dict] = []                     # tools/list 로 내보내는 설명 (등록 순서)
_IMPL: dict[str, Callable] = {}


def schema(props: dict, required: list[str] | None = None) -> dict:
    """도구 입력 JSON Schema — 모르는 인자는 받지 않는다."""
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def tool(spec: dict):
    """도구 등록 데코레이터. 이름이 겹치거나 설명이 모자라면 import 할 때 바로 실패한다."""
    def deco(fn: Callable) -> Callable:
        name = spec.get("name") or ""
        if not name or name in _IMPL:
            raise ValueError(f"MCP 도구 이름이 비었거나 겹칩니다: {name!r}")
        if (spec.get("inputSchema") or {}).get("type") != "object" or "annotations" not in spec:
            raise ValueError(f"MCP 도구 {name}: inputSchema(object)·annotations 가 필요합니다")
        if not spec.get("title") or not spec.get("description"):
            raise ValueError(f"MCP 도구 {name}: title·description 이 필요합니다")
        TOOLS.append(spec)
        _IMPL[name] = fn
        return fn
    return deco


def names() -> set[str]:
    return set(_IMPL)


def call(srv, name: str, args: dict) -> str:
    fn = _IMPL.get(name)
    if fn is None:
        raise KeyError(name)
    return fn(srv, args or {})
