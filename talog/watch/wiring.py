"""감시 부품 잇기 — 룰 엔진에 처리 여유 상태 파일을 붙이고 사건 에이전트·메일을 연결한다 (실시간·리플레이 공용)."""

from __future__ import annotations

from ..capacity import StateFile
from .alerts import Notifier
from .config import _today_dir
from .rules import RuleEngine


def _make_agent(cfg: dict, engine: RuleEngine, notifier: Notifier, gpu=None,
                replay: bool = False, day_dir: str = ""):
    """agent.enabled 또는 email.enabled 이면 사건 분석/메일 에이전트를 붙인다."""
    if not (cfg.get("agent", {}).get("enabled") or cfg.get("email", {}).get("enabled")):
        return None
    from ..agent import IncidentAgent
    from ..mailer import Mailer
    mailer = Mailer(cfg, notifier.alert_dir, replay=replay)
    if replay:
        def day_fn():
            return day_dir
    else:
        def day_fn():
            return _today_dir(cfg["watch_root"])
    agent = IncidentAgent(cfg, engine, notifier.alert_dir, day_fn, gpu=gpu,
                          mailer=mailer, replay=replay)
    notifier.listeners.append(agent.submit)
    a, llm, e = cfg["agent"], cfg["llm"], cfg["email"]
    parts = []
    if a.get("enabled"):
        parts.append("사건 분석 " + (f"룰+LLM({llm.get('model')}, {llm.get('device')}, "
                                     f"{llm.get('url')})" if a.get("use_llm", True)
                                     else "룰만"))
    if e.get("enabled"):
        parts.append("메일 " + ("outbox 만(리플레이/dry_run)" if mailer.dry_run else
                               f"{e.get('smtp_host')}:{e.get('smtp_port')} → "
                               f"{len(mailer.recipients(list((e.get('roles') or {}).keys())))}명"))
    print(f"[talog watch] 에이전트: {' / '.join(parts)} — 기준 심각도 "
          f"{a.get('min_severity', 'crit')}, 묶음 {a.get('batch_seconds', 60)}초")
    return agent


def _attach_capacity_state(engine: "RuleEngine", alert_dir: str, replay: bool = False):
    """처리 여유 기준선·시간별 KPI 를 data_dir 에 잇는다 (talog 재시작·로그 삭제에도 유지)."""
    sf = StateFile(alert_dir, replay=replay)
    sf.load(engine.cap)
    engine.kpi_sink = sf.write_kpi
    engine.state_sink = lambda: sf.save(engine.cap)
    return sf


# select/auto 모드에서 룰 엔진으로 넘길 이벤트 종류 (대용량 계측 이벤트 제외)
_ENGINE_KINDS = frozenset((
    "ERROR", "EXC_REDIRECT", "MODEL_FAIL", "CRASH", "COMM_FAIL", "RECIPE_FAIL",
    "INSP_START", "INSP_REJECT", "REJECT_BUSYCAM", "REJECT_NOTREADY", "REJECT_SIM",
    "GRAB_FAIL", "IMG_TIMEOUT", "ALG_TIMEOUT", "STORAGE_LOW", "LIGHT_UNSTABLE",
    "COMM_MSG", "POOL_CREATE", "POOL_DESTROY", "USAGE", "BATCH", "EXC_SAFE",
    "INSP_RECV", "SEQ_START", "PROC_TIME", "SEND_RESULT", "SEQ_FINISH"))
