"""리포트 입력(ReportContext)과 검사 상태 어휘(한글 이름·색·이상 상태 묶음)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..assemble import ChannelRun, Inspection, ModelLoad, ProcessGen
from ..diagnose import Finding
from ..events import Event
from ..recipe import Recipe


_STATUS_KO = {
    "complete": "완료",
    "rejected": "시작 거부",
    "incomplete_lost": "실행 중 소실",
    "incomplete": "미완료",
    "in_progress_eof": "로그 절단(판정 불가)",
    "sim_complete": "시뮬레이션 완료",
    "sim_partial": "시뮬레이션 부분 실행",
    "unknown": "불명",
}


# 디자인 토큰 — dataviz 검증 팔레트(레퍼런스 인스턴스, 라이트 서피스 기준)
# 상태색은 status 팔레트, 크기(막대)는 시퀀셜 blue, 보조 시퀀셜은 orange.
_STATUS_COLOR = {
    "complete": "#0ca30c",            # status: good
    "rejected": "#d03b3b",            # status: critical
    "incomplete": "#d03b3b",
    "unknown": "#d03b3b",
    "incomplete_lost": "#ec835a",     # status: serious
    "in_progress_eof": "#898781",     # muted (판정 불가)
    "sim_complete": "#1baf7a",        # categorical slot3 (중립 계열)
    "sim_partial": "#898781",
}


_OK_STATUSES = ("complete", "in_progress_eof", "sim_complete")


_BAD_STATUSES = ("rejected", "incomplete_lost", "incomplete", "unknown")


@dataclass
class ReportContext:
    title: str
    day_dir: str
    recipe: Recipe | None
    inspections: list[Inspection]
    runs: list[ChannelRun]
    gens: list[ProcessGen]
    events: list[Event]
    dl_channels: dict[int, str]
    file_rows: list[tuple] = field(default_factory=list)
    log_start: float = 0.0
    log_end: float = 0.0
    stitched: bool = False       # 익일 첫 구간 스티칭 적용 여부
    detail_cap: int = 60         # 상세(간트) 내장 검사 수 상한
    model_loads: list[ModelLoad] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    llm_summary: str = ""        # 로컬 LLM 종합 소견 (--llm 옵션)
    similar_cases: list = field(default_factory=list)   # (유사도,제목,원인,조치,site,date)
    gpu_watch: list = field(default_factory=list)  # TalogWatch gpu_*.jsonl 샘플
