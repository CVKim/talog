"""감시 설정 — 기본값(_DEFAULT_CFG), talog.yaml 읽기(load_config), 오늘 날짜 로그 폴더(_today_dir)."""

from __future__ import annotations

import datetime as dt
import json
import os

import yaml

from ..capacity import DEFAULTS as _CAPACITY_DEFAULTS
from ..tracker import DEFAULT_FILES


# 감시 대상 파일 (소형·핵심만 — 저부하 원칙). 추적 모드 default 의 목록이다.
_WATCH_FILES = DEFAULT_FILES


_DEFAULT_CFG = {
    "watch_root": r"D:\AIV_LOG\Talos",
    "poll_seconds": 20,
    "alert_dir": r"D:\AIV_LOG\TalogWatch",
    "low_priority": True,
    "site": "",
    # 추적 대상: default(핵심 9종) | select(files 지정) | auto(일자 폴더 전체)
    "tracking": {"mode": "default", "files": [],
                 "exclude": ["DebugImageSaveInfo.log", "InspCondRelationGraph*.log"],
                 "include_alg": False, "max_initial_mb": 32},
    "rules": {
        "error_repeat": {"window_min": 10, "count": 5},
        "no_insp_thread": {"enabled": True},
        "insp_stall": {"factor_x_median": 2.0, "min_seconds": 180, "cooldown_min": 10},
        "restart_burst": {"window_min": 60, "count": 3},
        "memory_trend": {"mb_per_hour": 100, "min_rise_mb": 500,
                         "min_hours": 2.0},
        "gpu_temp": {"enabled": True, "celsius": 85, "record_min": 5},
        # 결함명 감시 (comm.log INSPECT_END NG/REWORK 페이로드). 기본은 모두 꺼짐 —
        # 사이트마다 결함명·NG 수준이 달라 설정에서 켠다
        "defect_watch": {"enabled": True, "critical": [], "critical_cooldown_min": 10,
                         "repeat_window_min": 30, "repeat_count": 0,
                         "ng_streak": 0, "ng_rate_window": 0, "ng_rate_percent": 50},
        # 사용자 정의 로그 패턴 (tracker.PatternEngine): name/match/files/level/
        #   severity/count/window_min/cooldown_min — count 1 = 한 번만 나와도 경보
        "patterns": [],
        # 기본 룰 조정: {룰: {enabled, severity, count, window_min}} — count N 이면
        #   window_min 안에 N번 발생해야 경보 (이벤트형 룰 기준)
        "overrides": {},
        # 처리 여유·처리 시간 추세 (그룹별 기준선 대비) — talog/capacity.py
        "capacity": dict(_CAPACITY_DEFAULTS),
    },
    "notify": {"toast": True, "webhook": "", "jsonl": True},
    "llm": {"url": "http://127.0.0.1:11434", "device": "cpu",
            "model": "qwen2.5:7b", "cpu_threads": 0, "gpu_index": -1,
            "gpu_min_free_mb": 6000, "gpu_max_util": 40, "keep_alive": "",
            "num_ctx": 8192, "timeout_s": 600, "think": None},
    # 경보 사건 분석 (룰 진단 + LLM 2차 의견 + 합의 관문) — talog/agent/
    "agent": {"enabled": False, "use_llm": True, "min_severity": "crit",
              "batch_seconds": 60, "reason_first": True, "runbook": "",
              "gpu_log_tail_mb": 16, "casekb": True},
    # 사건 메일 (SMTP) — talog/mailer.py. 비밀번호는 환경변수로만
    "email": {"enabled": False,
              # 발송 방식: smtp (STARTTLS/SSL/사내 릴레이/Direct Send) | graph (Microsoft 365
              #   Graph API, OAuth2 — Exchange Online 은 SMTP 기본 인증을 폐지하는 중)
              "transport": "smtp",
              "graph": {"tenant_id": "", "client_id": "", "sender": "",
                        "secret_env": "TALOG_GRAPH_SECRET", "client_secret_dpapi": ""},
              "smtp_host": "", "smtp_port": 587,
              "security": "starttls", "username": "",
              "password_env": "TALOG_SMTP_PASSWORD", "password_dpapi": "",
              "sender": "", "to": [],
              "roles": {}, "min_severity": "crit", "min_interval_min": 5,
              "max_per_hour": 10, "outbox": True, "dry_run": False,
              "attach_json": True, "timeout_s": 20,
              # 등급 정책: immediate 등급은 묶음을 기다리지 않고 immediate_seconds 뒤
              #   룰 판단으로 바로 보낸다(메일 간격 제한 없음, 시간당 상한만).
              #   LLM 2차 의견은 llm_followup 이면 같은 스레드의 후속 메일로 보낸다
              "immediate": ["crit"], "immediate_seconds": 10, "llm_followup": True},
    "cooldown_min": 30,
}


def _deep_merge(base: dict, over: dict):
    """재귀 병합 — 사용자가 룰의 값 하나만 바꿔도 나머지 기본값이 유지된다."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config(path: str) -> dict:
    """엔진 설정. talog.yaml(사용자 설정)은 settings.compile 로 펼치고, 옛 watch.yaml
    (엔진 형식)은 기본값에 그대로 병합한다. 파일이 없으면 엔진 기본값."""
    cfg = json.loads(json.dumps(_DEFAULT_CFG))  # deep copy
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"! 설정 읽기 실패 — 기본값으로 진행: {e}")
            user = {}
        if not isinstance(user, dict):
            print("! 설정 파일 최상위가 딕셔너리가 아닙니다 — 기본값으로 진행")
            user = {}
        from .. import settings
        if user and not settings.is_legacy(user):
            return settings.compile(user)
        _deep_merge(cfg, user)
    return cfg


def _today_dir(root: str) -> str:
    now = dt.datetime.now()
    return os.path.join(root, f"{now.year:04d}_{now.month:02d}",
                        f"{now.day:02d}")
