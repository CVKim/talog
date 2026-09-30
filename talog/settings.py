"""talog 설정 (talog.yaml) — 사람이 정하는 값만 담은 사용자 설정과 엔진 설정 변환.

사용자 설정은 에이전트 흐름(수집 → 탐지 → 판단 → 알림) 순서의 약 20개 키다:

    site / log_root / data_dir / tracking      수집
    rules.builtin / rules.custom               탐지 (경보 규칙 한 표)
    ai.llm / ai.url / ai.model / ai.device     판단 (룰 진단 + LLM 교차 확인)
    mail.* / notify.*                          알림
    advanced                                   엔진 세부값 직접 조정 (보통 비움)

엔진(watch.RuleEngine·agent·mailer)은 기존의 세부 설정(dict)을 그대로 쓴다.
compile() 이 사용자 설정을 엔진 설정으로 펼치고, 옛 watch.yaml(엔진 형식)은
from_engine() 이 사용자 설정으로 옮긴다. 옮길 자리가 없는 값은 advanced 에
그대로 남겨 compile(from_engine(x)) == x 가 되게 한다 (손실 없는 변환).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re

import yaml

from .tracker import DEFAULT_FILES

SEVERITIES = ("crit", "warn", "info")

# ── 사용자 설정 기본값 ─────────────────────────────────────────────
DEFAULTS: dict = {
    "site": "",
    "log_root": r"D:\AIV_LOG\Talos",
    "data_dir": r"D:\AIV_LOG\TalogWatch",
    "tracking": "core",
    "rules": {"builtin": {}, "custom": []},
    "ai": {"llm": False, "url": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
           "device": "cpu"},
    "mail": {"provider": "off", "to": [], "roles": {}, "digest": False,
             "account": "", "sender": "", "host": "", "port": 587,
             "security": "starttls", "tenant_id": "", "client_id": "", "secret": ""},
    "notify": {"toast": True, "webhook": ""},
    "advanced": {},
}

# ── 기본 경보 규칙 ─────────────────────────────────────────────────
# (이름, 표시 이름, 기본 등급, 조건 종류, 기본 조건)
#   event  : 한 번 나오면 경보 — count/window_min 으로 'N분 안에 N번' 문턱을 둘 수 있다
#   repeat : count 번 / window_min 분 (엔진 룰 값)
BUILTIN = [
    ("no_insp_thread", "검사 시작 거부 (NoInspThread)", "crit", "event", {}),
    ("crash", "프로세스 크래시", "crit", "event", {}),
    ("img_timeout", "검사 타임아웃 — 판정 미송신", "crit", "event", {}),
    ("grab_fail", "그랩 실패 (카메라·트리거)", "crit", "event", {}),
    ("storage_low", "이미지 저장 공간 부족", "crit", "event", {}),
    ("light_unstable", "조명 컨트롤러 불안정", "crit", "event", {}),
    ("reject_busycam", "시작 거부 — 카메라 점유", "crit", "event", {}),
    ("reject_notready", "시작 거부 — 모델 미로드", "crit", "event", {}),
    ("reject_sim", "시작 거부 — 시뮬레이션 모드", "crit", "event", {}),
    ("alg_timeout", "알고리즘 타임아웃 (TIME_OUT NG)", "warn", "event", {}),
    ("restart_burst", "재시작 빈발", "crit", "repeat", {"count": 3, "window_min": 60}),
    ("error_repeat", "동일 에러 반복", "warn", "repeat", {"count": 5, "window_min": 10}),
    ("insp_stall", "검사 정체 (평소 소요의 N배)", "warn", "stall", {"factor": 2.0}),
    ("memory_trend", "메모리 증가 추세 (릭 의심)", "crit", "memory", {"mb_per_hour": 100}),
    ("gpu_temp", "GPU 과열", "crit", "gpu", {"celsius": 85}),
    ("ng_streak", "연속 NG", "crit", "streak", {"enabled": False, "count": 10}),
    ("ng_rate", "NG 비율 급증 (최근 N검사)", "warn", "rate",
     {"enabled": False, "count": 50, "percent": 50}),
]
_BUILTIN = {b[0]: b for b in BUILTIN}

# 메일 발송 방식
PROVIDERS = {
    "off": {"label": "보내지 않음"},
    "gmail": {"label": "Gmail (앱 비밀번호)",
              "help": "Google 계정 → 보안 → 2단계 인증을 켠 뒤 '앱 비밀번호' 16자리를 "
                      "만들어 넣습니다. 계정 비밀번호는 넣지 마십시오. 고객사 현장 "
                      "데이터가 회사 밖 계정으로 나가므로 시험·개발 PC 에만 권합니다."},
    "m365": {"label": "Microsoft 365 (회사 메일)",
             "help": "IT 가 Entra 앱 등록 + 보낼 사서함 1개에 Mail.Send 권한을 1회 "
                     "설정해 주면 HTTPS 로 보냅니다 (SMTP 포트 불필요, 정크함 문제 없음). "
                     "테넌트 ID·앱 ID·보낼 사서함·클라이언트 암호를 받아 넣습니다."},
    "smtp": {"label": "SMTP 서버 (사내 릴레이 등)",
             "help": "사내 메일 릴레이나 다른 SMTP 서버. 계정을 비우면 인증 없이 보냅니다."},
}

# 엔진 설정에서 사용자 설정·diff 비교에서 빼는 값 (없어진 기능)
_DROPPED = (("llm", "enabled"), ("llm", "script"), ("llm", "interval_min"))


# ---------------------------------------------------------------------------
def _deep_merge(base: dict, over: dict):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


def _engine_defaults() -> dict:
    from .watch import _DEFAULT_CFG
    return json.loads(json.dumps(_DEFAULT_CFG))


def normalize(user: dict | None) -> dict:
    """기본값 위에 사용자 값을 얹는다 (목록은 통째 교체)."""
    s = json.loads(json.dumps(DEFAULTS))
    _deep_merge(s, user or {})
    if not isinstance(s["rules"].get("custom"), list):
        s["rules"]["custom"] = []
    if not isinstance(s["rules"].get("builtin"), dict):
        s["rules"]["builtin"] = {}
    return s


def is_legacy(doc: dict) -> bool:
    """옛 watch.yaml(엔진 형식)인가 — 사용자 설정에 없는 최상위 키로 가린다."""
    return any(k in doc for k in ("watch_root", "alert_dir", "agent", "email", "llm",
                                  "poll_seconds", "cooldown_min", "low_priority"))


def builtin_value(s: dict, name: str) -> dict:
    """기본 규칙의 현재 조건 (기본 조건 + 사용자 조정)."""
    b = _BUILTIN[name]
    v = {"enabled": True, "severity": b[2], "count": 1, "window_min": 10}
    v.update(b[4])
    v.update((s.get("rules") or {}).get("builtin", {}).get(name) or {})
    return v


def _as_list(x) -> list:
    if x is None or x == "":
        return []
    if isinstance(x, (list, tuple)):
        return [str(i).strip() for i in x if str(i).strip()]
    return [t.strip() for t in str(x).split(",") if t.strip()]


# ---------------------------------------------------------------------------
def compile(user: dict | None) -> dict:
    """사용자 설정 → 엔진 설정."""
    s = normalize(user)
    cfg = _engine_defaults()
    cfg["site"] = str(s["site"] or "")
    cfg["watch_root"] = str(s["log_root"] or DEFAULTS["log_root"])
    cfg["alert_dir"] = str(s["data_dir"] or DEFAULTS["data_dir"])

    r = cfg["rules"]
    ov = r["overrides"]
    for name, _label, sev, kind, _d in BUILTIN:
        v = builtin_value(s, name)
        o = {}
        if v.get("enabled") is False and kind not in ("streak", "rate"):
            o["enabled"] = False
        if str(v.get("severity", sev)).lower() != sev and v.get("severity") in SEVERITIES:
            o["severity"] = v["severity"]
        if kind == "event":
            n = int(v.get("count", 1) or 1)
            if n > 1:
                o["count"] = n
                o["window_min"] = float(v.get("window_min", 10) or 10)
        elif kind == "repeat":
            r[name]["count"] = int(v["count"])
            r[name]["window_min"] = v["window_min"]
        elif kind == "stall":
            r[name]["factor_x_median"] = float(v["factor"])
        elif kind == "memory":
            r[name]["mb_per_hour"] = v["mb_per_hour"]
        elif kind == "gpu":
            r[name]["celsius"] = v["celsius"]
        elif kind == "streak":
            r["defect_watch"]["ng_streak"] = int(v["count"]) if v.get("enabled") else 0
        elif kind == "rate":
            r["defect_watch"]["ng_rate_window"] = int(v["count"]) if v.get("enabled") else 0
            r["defect_watch"]["ng_rate_percent"] = v.get("percent", 50)
        if o:
            ov[name] = o

    pats, defs, extra_files = [], [], []
    for it in s["rules"]["custom"]:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        typ = str(it.pop("type", "log") or "log")
        if typ == "defect":
            it["match"] = _as_list(it.get("match"))
            defs.append(it)
        else:
            pats.append(it)
            for f in it.get("files") or []:
                if str(f).lower() not in DEFAULT_FILES:
                    extra_files.append(str(f))
    r["patterns"] = pats
    r["defect_watch"]["rules"] = defs

    tr = s["tracking"]
    if isinstance(tr, list):
        cfg["tracking"]["mode"] = "select"
        cfg["tracking"]["files"] = [str(x) for x in tr]
    elif str(tr).lower() == "all":
        cfg["tracking"]["mode"] = "auto"
    elif extra_files:
        # 사용자 규칙이 핵심 로그 밖의 파일을 보면 그 파일도 같이 읽는다
        names = {"inspstarter.log": "InspStarter.log", "comm.log": "Comm.log",
                 "workerthreadpoolmng.log": "WorkerThreadPoolMng.log",
                 "exception.log": "exception.log", "processusage.log": "ProcessUsage.log",
                 "batchrunlog.txt": "BatchRunLog.txt"}
        base = [names.get(f, f) for f in DEFAULT_FILES]
        cfg["tracking"]["mode"] = "select"
        cfg["tracking"]["files"] = base + [f for f in dict.fromkeys(extra_files)
                                           if f.lower() not in DEFAULT_FILES]

    a = s["ai"]
    cfg["agent"]["enabled"] = True
    cfg["agent"]["use_llm"] = bool(a.get("llm"))
    cfg["llm"]["url"] = str(a.get("url") or DEFAULTS["ai"]["url"])
    cfg["llm"]["model"] = str(a.get("model") or DEFAULTS["ai"]["model"])
    cfg["llm"]["device"] = str(a.get("device") or "cpu").lower()
    if cfg["llm"]["model"].lower().startswith("qwen3"):
        cfg["llm"]["think"] = False               # qwen3 계열 생각 모드는 느리기만 하다 (벤치)

    m, e = s["mail"], cfg["email"]
    prov = str(m.get("provider") or "off").lower()
    e["enabled"] = prov != "off"
    e["to"] = _as_list(m.get("to"))
    e["roles"] = {k: _as_list(v) for k, v in (m.get("roles") or {}).items() if _as_list(v)}
    e["min_severity"] = "warn" if m.get("digest") else "crit"
    acct = str(m.get("account") or "")
    sender = str(m.get("sender") or "")
    secret = str(m.get("secret") or "")
    if prov == "m365":
        e["transport"] = "graph"
        e["graph"]["tenant_id"] = str(m.get("tenant_id") or "")
        e["graph"]["client_id"] = str(m.get("client_id") or "")
        e["graph"]["sender"] = sender or acct
        e["graph"]["client_secret_dpapi"] = secret
    elif prov in ("gmail", "smtp", "off"):
        e["transport"] = "smtp"
        if prov == "gmail":
            e["smtp_host"], e["smtp_port"], e["security"] = "smtp.gmail.com", 587, "starttls"
            e["attach_json"] = False              # 회사 밖 계정으로는 근거 JSON 을 붙이지 않음
            sender = sender or (f"talog <{acct}>" if acct else "")
        else:
            e["smtp_host"] = str(m.get("host") or "")
            e["smtp_port"] = int(m.get("port") or 587)
            e["security"] = str(m.get("security") or "starttls")
        e["username"] = acct
        e["sender"] = sender
        e["password_dpapi"] = secret

    n = s["notify"]
    cfg["notify"]["toast"] = bool(n.get("toast", True))
    cfg["notify"]["webhook"] = str(n.get("webhook") or "")

    _deep_merge(cfg, s.get("advanced") or {})
    return cfg


# ---------------------------------------------------------------------------
def _diff(cur, base):
    """cur 에서 base 와 다른 부분만 (dict 는 재귀, 그 밖은 통째)."""
    if isinstance(cur, dict) and isinstance(base, dict):
        out = {}
        for k, v in cur.items():
            if k not in base:
                out[k] = copy.deepcopy(v)
                continue
            d = _diff(v, base[k])
            if d is not _SAME:
                out[k] = d
        return out if out else _SAME
    return _SAME if cur == base else copy.deepcopy(cur)


_SAME = object()


def _norm_engine(cfg: dict) -> dict:
    """옛 결함 감시 키(critical/repeat_count)를 규칙 목록으로 옮긴 엔진 설정."""
    cfg = copy.deepcopy(cfg)
    for sec, key in _DROPPED:
        (cfg.get(sec) or {}).pop(key, None)
    dw = cfg.setdefault("rules", {}).setdefault("defect_watch", {})
    rules = list(dw.get("rules") or [])
    crit = _as_list(dw.get("critical"))
    if crit:
        it = {"name": "치명 결함", "match": crit, "severity": "crit", "count": 1}
        cd = dw.get("critical_cooldown_min", 10)
        if cd != 10:
            it["cooldown_min"] = cd
        rules.insert(0, it)
    need = int(dw.get("repeat_count", 0) or 0)
    if need:
        rules.append({"name": "동일 결함 빈발", "match": ["*"], "severity": "warn",
                      "count": need, "window_min": dw.get("repeat_window_min", 30)})
    em = cfg.setdefault("email", {})
    em["roles"] = {k: v for k, v in (em.get("roles") or {}).items() if _as_list(v)}
    dw["critical"] = []
    dw["repeat_count"] = 0
    dw["rules"] = rules
    # 플랫폼에서는 사건 분석이 늘 켜져 있다 — 옛 설정에서 분석을 껐으면 LLM 도 안 쓴 것
    ag = cfg.setdefault("agent", {})
    ag["use_llm"] = bool(ag.get("use_llm", True)) and bool(ag.get("enabled", True))
    ag["enabled"] = True
    return cfg


def from_engine(engine_cfg: dict) -> dict:
    """엔진 설정(옛 watch.yaml 을 기본값에 병합한 것) → 사용자 설정."""
    base = _engine_defaults()
    _deep_merge(base, engine_cfg)
    c = _norm_engine(base)
    s = json.loads(json.dumps(DEFAULTS))
    s["site"] = c.get("site", "")
    s["log_root"] = c.get("watch_root", DEFAULTS["log_root"])
    s["data_dir"] = c.get("alert_dir", DEFAULTS["data_dir"])
    t = c.get("tracking") or {}
    mode = str(t.get("mode", "default"))
    s["tracking"] = ("all" if mode == "auto" else
                     list(t.get("files") or []) if mode == "select" else "core")

    r = c["rules"]
    ov = r.get("overrides") or {}
    bi = {}
    for name, _label, sev, kind, d in BUILTIN:
        o = ov.get(name) or {}
        v = {}
        if o.get("enabled") is False:
            v["enabled"] = False
        if o.get("severity") in SEVERITIES and o["severity"] != sev:
            v["severity"] = o["severity"]
        if kind == "event" and int(o.get("count", 1) or 1) > 1:
            v["count"] = int(o["count"])
            v["window_min"] = o.get("window_min", 10)
        elif kind == "repeat":
            for k in ("count", "window_min"):
                if r[name][k] != d[k]:
                    v[k] = r[name][k]
        elif kind == "stall" and r[name]["factor_x_median"] != d["factor"]:
            v["factor"] = r[name]["factor_x_median"]
        elif kind == "memory" and r[name]["mb_per_hour"] != d["mb_per_hour"]:
            v["mb_per_hour"] = r[name]["mb_per_hour"]
        elif kind == "gpu" and r[name]["celsius"] != d["celsius"]:
            v["celsius"] = r[name]["celsius"]
        elif kind == "streak" and int(r["defect_watch"].get("ng_streak", 0) or 0):
            v.update(enabled=True, count=int(r["defect_watch"]["ng_streak"]))
        elif kind == "rate" and int(r["defect_watch"].get("ng_rate_window", 0) or 0):
            v.update(enabled=True, count=int(r["defect_watch"]["ng_rate_window"]))
        if kind == "rate" and r["defect_watch"].get("ng_rate_percent", 50) != d["percent"]:
            v["percent"] = r["defect_watch"]["ng_rate_percent"]
        if v:
            bi[name] = v
    s["rules"]["builtin"] = bi
    custom = [dict(type="log", **{k: v for k, v in p.items() if k != "type"})
              for p in r.get("patterns") or []]
    custom += [dict(type="defect", **{k: v for k, v in d.items() if k != "type"})
               for d in r["defect_watch"].get("rules") or []]
    s["rules"]["custom"] = custom

    lc, ag = c.get("llm") or {}, c.get("agent") or {}
    s["ai"] = {"llm": bool(ag.get("use_llm", True)),
               "url": lc.get("url", DEFAULTS["ai"]["url"]),
               "model": lc.get("model", DEFAULTS["ai"]["model"]),
               "device": lc.get("device", "cpu")}

    e = c.get("email") or {}
    m = s["mail"]
    m["to"] = list(e.get("to") or [])
    m["roles"] = {k: _as_list(v) for k, v in (e.get("roles") or {}).items() if _as_list(v)}
    m["digest"] = str(e.get("min_severity", "crit")) in ("warn", "info")
    if not e.get("enabled"):
        # 꺼진 메일 설정도 접속 값은 남긴다 (나중에 켤 때 다시 쓰도록)
        m.update(host=e.get("smtp_host", ""), port=e.get("smtp_port", 587),
                 security=e.get("security", "starttls"), account=e.get("username", ""),
                 sender=e.get("sender", ""), secret=e.get("password_dpapi", ""))
    else:
        if str(e.get("transport", "smtp")) == "graph":
            g = e.get("graph") or {}
            m.update(provider="m365", tenant_id=g.get("tenant_id", ""),
                     client_id=g.get("client_id", ""), sender=g.get("sender", ""),
                     secret=g.get("client_secret_dpapi", ""))
        elif str(e.get("smtp_host", "")).lower() == "smtp.gmail.com":
            acct = e.get("username", "")
            snd = e.get("sender", "")
            m.update(provider="gmail", account=acct,
                     sender="" if snd in ("", f"talog <{acct}>") else snd,
                     secret=e.get("password_dpapi", ""))
        else:
            m.update(provider="smtp", host=e.get("smtp_host", ""),
                     port=e.get("smtp_port", 587), security=e.get("security", "starttls"),
                     account=e.get("username", ""), sender=e.get("sender", ""),
                     secret=e.get("password_dpapi", ""))
    n = c.get("notify") or {}
    s["notify"] = {"toast": bool(n.get("toast", True)), "webhook": n.get("webhook", "")}

    # 옮길 자리가 없는 값은 advanced 로 (compile 결과와의 차이)
    s["advanced"] = {}
    got = _norm_engine(compile(s))
    adv = _diff(c, got)
    s["advanced"] = {} if adv is _SAME else adv
    return s


# ---------------------------------------------------------------------------
def load(path: str) -> tuple[dict, bool]:
    """(사용자 설정, 옛 형식이었는가). 파일이 없으면 기본값."""
    doc = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"! 설정 읽기 실패 — 기본값으로 진행: {e}")
            doc = {}
        if not isinstance(doc, dict):
            print("! 설정 파일 최상위가 딕셔너리가 아닙니다 — 기본값으로 진행")
            doc = {}
    if is_legacy(doc):
        return from_engine(doc), True
    return normalize(doc), False


def validate(s: dict) -> list[str]:
    from .tracker import compile_patterns
    errs = []
    s = normalize(s)
    logs = [dict((k, v) for k, v in it.items() if k != "type")
            for it in s["rules"]["custom"]
            if isinstance(it, dict) and str(it.get("type", "log")) != "defect"]
    errs += compile_patterns(logs)[1]
    for it in s["rules"]["custom"]:
        if isinstance(it, dict) and it.get("type") == "defect" and not _as_list(it.get("match")):
            errs.append(f"{it.get('name') or '결함 규칙'}: 결함명이 비어 있음")
    tr = s["tracking"]
    if not isinstance(tr, list) and str(tr) not in ("core", "all"):
        errs.append(f"tracking 값 오류: {tr} (core | all | 파일 목록)")
    if isinstance(tr, list) and not tr:
        errs.append("추적할 파일 목록이 비어 있음")
    if str(s["ai"].get("device")) not in ("cpu", "gpu", "auto"):
        errs.append(f"ai.device 값 오류: {s['ai'].get('device')}")
    m = s["mail"]
    prov = str(m.get("provider"))
    if prov not in PROVIDERS:
        errs.append(f"mail.provider 값 오류: {prov}")
    elif prov != "off":
        if not _as_list(m.get("to")):
            errs.append("받는 사람을 한 명 이상 넣으십시오")
        if prov == "gmail" and not m.get("account"):
            errs.append("Gmail 계정(보내는 주소)을 넣으십시오")
        if prov == "m365" and not (m.get("tenant_id") and m.get("client_id")
                                   and (m.get("sender") or m.get("account"))):
            errs.append("Microsoft 365 는 테넌트 ID·앱 ID·보낼 사서함이 필요합니다")
        if prov == "smtp" and not m.get("host"):
            errs.append("SMTP 서버 주소를 넣으십시오")
    return errs


# ── 저장 (설명 주석 포함) ──────────────────────────────────────────
_DESC = {
    "site": "설비 이름 (메일 제목·기록에 표시)",
    "log_root": "talos 로그 루트 — 오늘 YYYY_MM\\DD 폴더를 자동으로 따라감",
    "data_dir": "경보·사건·리포트 저장 폴더",
    "tracking": "core = 핵심 로그(저부하) | all = 일자 폴더 전체 | [파일 목록]",
    "rules": "경보 규칙",
    "rules.builtin": "기본 규칙 조정 — {규칙: {enabled, severity, count, window_min, ...}}",
    "rules.custom": "사용자 규칙 — type defect(판정 결함명) | log(로그 문구, re: 정규식)",
    "ai": "판단 — 룰 진단 + 로컬 LLM 교차 확인",
    "ai.llm": "true = Qwen 교차 확인 / false = 룰 진단만",
    "ai.url": "Ollama 주소",
    "ai.device": "cpu(검사 GPU 미사용) | gpu | auto(여유 VRAM 보고 선택)",
    "mail": "알림 메일 — 심각 즉시, 주의는 digest 일 때 묶음",
    "mail.provider": "off | gmail | m365 | smtp",
    "mail.roles": "분석이 지목한 담당 역할별 추가 수신자",
    "mail.digest": "true = 주의 등급도 묶어서 메일",
    "mail.secret": "콘솔에서 입력한 비밀번호·암호의 암호문 (이 PC·이 사용자만 복호화)",
    "notify": "화면 알림",
    "advanced": "엔진 세부값 직접 조정 (poll_seconds, agent.batch_seconds 등) — 보통 비움",
}


def dump(s: dict) -> str:
    from . import __version__
    body = yaml.safe_dump(normalize(s), allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)
    out, stack = [], []
    key_re = re.compile(r"^(\s*)([A-Za-z_][\w]*):(.*)$")
    for line in body.splitlines():
        m = key_re.match(line)
        if m and not line.lstrip().startswith("-"):
            ind = len(m.group(1))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            stack.append((ind, m.group(2)))
            path = ".".join(k for _i, k in stack)
            if path in _DESC and "#" not in m.group(3):
                line = f"{line}  # {_DESC[path]}"
        out.append(line)
    head = (f"# talog 설정 — AI 비전 로그 운영 플랫폼 {__version__} "
            f"(콘솔 저장 {dt.datetime.now():%Y-%m-%d %H:%M})\n"
            "# 직접 고쳐도 됩니다. 콘솔로 다시 저장하면 이전 파일은 .bak 으로 남습니다.\n")
    return head + "\n".join(out) + "\n"


def default_path() -> str:
    """설정 파일 기본 위치: 실행 파일(또는 저장소) 옆 talog.yaml, 없고 watch.yaml 이 있으면 그것."""
    import sys
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    new = os.path.join(base, "talog.yaml")
    old = os.path.join(base, "watch.yaml")
    if not os.path.exists(new) and os.path.exists(old):
        return old
    return new
