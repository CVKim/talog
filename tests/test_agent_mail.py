# -*- coding: utf-8 -*-
"""사건 분석 에이전트·메일·LLM 클라이언트·콘솔 API 테스트.

외부 서비스 없이 검증한다: 메일은 테스트 안의 로컬 SMTP 수신기(인증 포함)로,
LLM 은 가짜 Ollama HTTP 서버로, 콘솔은 임의 포트의 실제 HTTP 서버로 확인한다.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import json
import os
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from talog import watch
from talog.agent import IncidentAgent, LLMAnalyst, RuleDiagnoser, Runbook, fact_check, gate
from talog.events import Event
from talog.llm import OllamaClient, _parse_gpu_free, resolve_device
from talog.mailer import Mailer
from talog.watch import Alert, Notifier, RuleEngine

_BASE = dt.datetime(2026, 7, 27, 9, 28, 3).timestamp()


# ── 공용 도구 ────────────────────────────────────────────────
def _ev(**over):
    """룰 진단용 최소 근거 dict (필요한 칸만 덮어쓴다)."""
    ev = {"site": "PC3", "trigger": {"time": "09:28:03", "rule": "no_insp_thread",
                                     "title": "검사 시작 거부", "severity": "crit",
                                     "evidence": ""},
          "n_alerts": 1, "alerts": [],
          "inspections": {"baseline_duration_s": 106.0, "recent_durations_s": [180.0, 200.0],
                          "slowdown_ratio": 1.8, "inflight": 2, "oldest_inflight_s": 199.0,
                          "recent_interval_s": 100.0, "wait_threads_recent": [1, 0]},
          "rejects": {"count_30min": 1},
          "timeouts": {"img_timeout_30min": 0, "alg_timeout_30min": 0, "last": None},
          "restarts": {"count_60min": 0, "count_30min": 0, "last_restart": None,
                       "since_restart_s": None, "kill_60min": 0, "pool_log_lines_60min": 0},
          "model_load": {"last_cmd": None, "name": None, "prev_name": None, "changed": None,
                         "minutes_since": None, "pending": False},
          "errors": {"count_30min": 0, "top": [], "seh_near": 0, "seh_codes": [],
                     "crash_near": False, "grab_fail_30min": 0, "light_unstable_30min": 0},
          "gpu_log": {"fatal_near": 0, "infer_errors_near": 0, "exec_slowdown_ratio": None},
          "defects": {"inspections_recent": 50, "ng_recent": 2, "ng_rate_recent": 4.0,
                      "ng_rate_baseline": 3.0, "ng_rate_before_change": None, "streak": 0,
                      "top": [], "critical_hits": []}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(ev.get(k), dict):
            ev[k] = {**ev[k], **v}
        else:
            ev[k] = v
    return ev


def _a(rule, sev="crit", ts=_BASE, title="t", evidence="e", key=""):
    return Alert(ts, rule, sev, title, evidence, key=key)


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server

        def w(s):
            self.wfile.write((s + "\r\n").encode())
            self.wfile.flush()
        w("220 sink ESMTP")
        data, buf, mail = False, [], {"rcpt": []}
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            if data:
                if s == ".":
                    data = False
                    mail["data"] = "\r\n".join(buf)
                    srv.messages.append(mail)
                    buf, mail = [], {"rcpt": []}
                    w("250 OK")
                else:
                    buf.append(s[1:] if s.startswith("..") else s)
                continue
            up = s.upper()
            if up.startswith("EHLO"):
                w("250-sink")
                w("250-AUTH PLAIN LOGIN")
                w("250 8BITMIME")
            elif up.startswith("AUTH PLAIN"):
                srv.auth.append(base64.b64decode(s.split(" ", 2)[2]).split(b"\0"))
                w("235 2.7.0 ok")
            elif up.startswith("MAIL"):
                mail["from"] = s
                w("250 OK")
            elif up.startswith("RCPT"):
                mail["rcpt"].append(s)
                w("250 OK")
            elif up.startswith("DATA"):
                data = True
                w("354 go")
            elif up.startswith("QUIT"):
                w("221 bye")
                break
            else:
                w("250 OK")


@pytest.fixture()
def smtp_sink():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    srv.daemon_threads = True
    srv.messages, srv.auth = [], []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"models": [{"name": "qwen2.5:7b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if not self.server.replies:
            return self._json({"error": "no reply"}, 500)
        self._json({"message": {"content": self.server.replies.pop(0)},
                    "prompt_eval_count": 1000, "eval_count": 100,
                    "load_duration": 2_000_000_000})


@pytest.fixture()
def fake_ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    srv.requests, srv.replies = [], []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


# ── 숫자 대조·합의 관문 ───────────────────────────────────────
def test_fact_check_times_ids_numbers():
    ev = _ev(alerts=[{"time": "09:28:03", "evidence": "inner id 2000026072709262821"}])
    ok = fact_check("09:28 에 거부. 진행 중 2건, 평소 106초의 1.8배. inner 2000026072709262821", ev)
    assert ok["ok"], ok
    bad = fact_check("9시 31분 거부, 진행 중 5건, 평소 250초, inner 2000026072709262822", ev)
    assert set(bad["unsupported"]) >= {"9시 31분", "5", "250", "2000026072709262822"}
    pct = fact_check("NG 비율 4.0% (기준 3.0%)", ev)
    assert pct["ok"] and fact_check("한자 섞임 測試", ev)["hanzi"] == 2


def test_gate_modes():
    rb = Runbook()
    rule = {"cause": "throughput_saturation", "name": "처리량 포화", "facts": ["진행 중 2건"],
            "confirmed": True, "actions": ["avoid_restart_now"], "notify": ["vision_engineer"]}
    al = [_a("no_insp_thread")]
    llm_ok = {"ok": True, "root_cause": "throughput_saturation", "notify": "operator",
              "message_ko": "진행 중 2건 — 재시작 보류", "fact": {"ok": True, "hanzi": 0}}
    v = gate("reject", rule, llm_ok, al, rb)
    assert v["mode"] == "agree" and v["message_source"] == "llm"
    assert v["notify"] == ["vision_engineer", "operator"]
    v = gate("reject", rule, {**llm_ok, "fact": {"ok": False, "hanzi": 0}}, al, rb)
    assert v["mode"] == "agree" and v["message_source"] == "rule"      # 근거 없는 숫자
    v = gate("reject", rule, {**llm_ok, "root_cause": "gpu_context_fault"}, al, rb)
    assert v["mode"] == "disagree" and "escalate_to_human" in v["actions"]
    assert "LLM 의견" in v["message"]
    v = gate("reject", {**rule, "cause": "unknown", "name": "원인 미상"}, llm_ok, al, rb)
    assert v["mode"] == "llm_only"
    v = gate("reject", rule, {"ok": False, "error": "timeout"}, al, rb)
    assert v["mode"] == "rule_only" and "timeout" in v["label"]
    v = gate("system", {**rule, "confirmed": False}, None, al, rb)
    assert v["severity"] == "warn" and v["label"].startswith("오경보 의심")


def test_interpret_key_facts_without_cause_names():
    from talog.agent import interpret
    rb = Runbook()
    ev = _ev(gpu_log={"source": "DLInfer.log (꼬리 조사)", "fatal_near": 38,
                      "first_fatal": "11:21:08", "exec_slowdown_ratio": 1.01},
             errors={"seh_near": 24, "seh_codes": ["0xc0000005"]},
             inspections={"inflight": 10, "oldest_inflight_s": 2.3, "recent_interval_s": 0.9,
                          "duration_over_interval": 0.78, "slowdown_ratio": 0.7,
                          "baseline_duration_s": 0.9})
    facts = interpret(ev, rb.params)
    text = "\n".join(facts)
    assert "GPU 치명 오류(enqueueV3 cudaGetLastError) 38건이 경보 ±300초 안에 있다" in text
    assert "SEH 예외 24건(0xc0000005)" in text and "투입이 처리보다 느리다" in text
    for cid in list(rb.causes("reject")) + ["처리량 포화", "미초기화"]:
        assert cid not in text                          # 원인 이름은 말하지 않는다
    none = interpret(_ev(gpu_log={"source": "DLInfer.log 없음"}), rb.params)
    assert any("GPU 오류 여부는 알 수 없다" in f for f in none)


# ── 룰 진단 ──────────────────────────────────────────────────
def test_rule_diagnoser_reject_causes():
    d = RuleDiagnoser(Runbook(), watch.load_config(""))
    assert d.diagnose("reject", _ev(), [])["cause"] == "throughput_saturation"
    assert d.diagnose("reject", _ev(gpu_log={"fatal_near": 38, "first_fatal": "11:21:08"}),
                      [])["cause"] == "gpu_context_fault"
    assert d.diagnose("reject", _ev(errors={"seh_near": 3}), [])["cause"] == "gpu_context_fault"
    assert d.diagnose("reject", _ev(restarts={"since_restart_s": 40, "last_restart": "09:27"}),
                      [])["cause"] == "not_initialized"
    assert d.diagnose("reject", _ev(model_load={"pending": True, "last_cmd": "09:27:50"}),
                      [])["cause"] == "not_initialized"
    assert d.diagnose("reject", _ev(timeouts={"img_timeout_30min": 2, "last": "09:10"},
                                    inspections={"inflight": 0, "slowdown_ratio": 1.0}),
                      [])["cause"] == "slot_leak_timeout"
    assert d.diagnose("reject", _ev(inspections={"inflight": 1, "slowdown_ratio": 1.0}),
                      [])["cause"] == "unknown"


def test_rule_diagnoser_defect_and_system():
    cfg = watch.load_config("")
    d = RuleDiagnoser(Runbook(), cfg)
    crit = [_a("defect_critical")]
    hit = {"critical_hits": [{"name": "BOOT_DAMAGED", "inner_id": "1", "time": "10:00:00"}]}
    assert d.diagnose("defect", _ev(defects=hit), crit)["cause"] == "critical_defect"
    assert d.diagnose("defect", _ev(defects=hit, timeouts={"alg_timeout_30min": 4}),
                      crit)["cause"] == "system_forced_ng"
    burst = [_a("ng_streak")]
    assert d.diagnose("defect", _ev(defects={"streak": 9, "top": [["DOT_GREASE_MISS", 9]]}),
                      burst)["cause"] == "defect_burst"
    rm = d.diagnose("defect", _ev(model_load={"minutes_since": 12.0, "last_cmd": "09:10",
                                              "name": "THETA3", "changed": True},
                                  defects={"ng_rate_recent": 68.0,
                                           "ng_rate_before_change": 6.3}), burst)
    assert rm["cause"] == "recipe_mismatch"
    r = d.diagnose("system", _ev(restarts={"count_60min": 1, "pool_log_lines_60min": 5}),
                   [_a("restart_burst")], "restart_loop")
    assert r["cause"] == "restart_loop" and r["confirmed"] is False
    r = d.diagnose("system", _ev(restarts={"count_60min": 4, "pool_log_lines_60min": 20}),
                   [_a("restart_burst")], "restart_loop")
    assert r["confirmed"] is True


# ── LLM 클라이언트·분석기 ─────────────────────────────────────
def test_resolve_device_auto():
    gp = _parse_gpu_free("0, 9500, 10240, 3\n1, 1800, 10240, 85\n")
    cfg = {"device": "auto", "gpu_min_free_mb": 6000, "gpu_max_util": 40}
    assert resolve_device(cfg, gp)[0] == "gpu"
    assert resolve_device({**cfg, "gpu_index": 1}, gp)[0] == "cpu"
    assert resolve_device(cfg, [])[0] == "cpu"
    assert resolve_device({"device": "gpu"}, [])[0] == "gpu"
    # 자기 모델이 이미 GPU 에 상주하면 그 VRAM 때문에 CPU 로 비켜 가지 않는다
    busy = _parse_gpu_free("0, 3000, 10240, 5\n")
    assert resolve_device(cfg, busy)[0] == "cpu"
    assert resolve_device(cfg, busy, resident="gpu") == ("gpu", "모델이 이미 GPU 에 상주")
    opt = OllamaClient({"device": "cpu", "cpu_threads": 4}).options("cpu")
    assert opt["num_gpu"] == 0 and opt["num_thread"] == 4
    assert "num_gpu" not in OllamaClient({}).options("gpu")


def test_llm_analyst_two_step_and_hanzi_retry(fake_ollama):
    url = f"http://127.0.0.1:{fake_ollama.server_address[1]}"
    ev = _ev(alerts=[{"time": "09:28:03", "severity": "crit", "rule": "no_insp_thread",
                      "title": "검사 시작 거부", "evidence": ""}])
    ok_json = json.dumps({"evidence": ["진행 중 2건"], "root_cause": "throughput_saturation",
                          "confidence": 0.8, "actions": ["avoid_restart_now", "bogus"],
                          "notify": "vision_engineer", "severity": "crit",
                          "message_ko": "진행 중 검사 2건 — 평소 106초의 1.8배입니다."},
                         ensure_ascii=False)
    fake_ollama.replies = ["분석: 진행 중 2건.", ok_json.replace("입니다", "測試"), ok_json]
    an = LLMAnalyst({"url": url, "model": "qwen2.5:7b", "device": "cpu", "cpu_threads": 2,
                     "keep_alive": "0"}, Runbook())
    out = an.analyze("reject", ev)
    assert out["ok"] and out["root_cause"] == "throughput_saturation"
    assert out["actions"] == ["avoid_restart_now"]             # 어휘 밖 조치 제거
    assert out["calls"] == 3 and out["fact"]["ok"] and out["device"] == "cpu"
    reqs = fake_ollama.requests
    assert "format" not in reqs[0] and reqs[1]["format"]["properties"]["root_cause"]["enum"]
    assert reqs[0]["options"]["num_gpu"] == 0 and reqs[0]["options"]["num_thread"] == 2
    assert reqs[0]["keep_alive"] == "0"
    fake_ollama.replies = ["분석", "JSON 아님"]
    assert LLMAnalyst({"url": url}, Runbook()).analyze("reject", ev)["error"] == "JSON 파싱 실패"
    dead = LLMAnalyst({"url": "http://127.0.0.1:9", "timeout_s": 2}, Runbook()).analyze(
        "reject", ev)
    assert not dead["ok"] and dead["error"]


# ── 에이전트: 즉시 메일 + LLM 후속 메일, 묶음 메일 ─────────────
def _agent_env(tmp_path, **email_over):
    cfg = watch.load_config("")
    cfg["alert_dir"] = str(tmp_path)
    cfg["site"] = "PC3"
    cfg["agent"].update(enabled=True, use_llm=True, casekb=False)   # 로컬 Ollama 임베딩 차단
    cfg["email"].update(enabled=True, to=["lead@example.com"],
                        roles={"vision_engineer": ["vision@example.com"]}, **email_over)
    n = Notifier(cfg, replay=True)
    eng = RuleEngine(cfg, n)
    m = Mailer(cfg, str(tmp_path), replay=True)
    ag = IncidentAgent(cfg, eng, str(tmp_path), lambda: str(tmp_path), mailer=m, replay=True)
    n.listeners.append(ag.submit)
    return cfg, n, eng, ag


def test_agent_immediate_mail_then_llm_followup(tmp_path):
    _c, _n, eng, ag = _agent_env(tmp_path)
    calls = []

    def fake_llm(fam, ev):
        calls.append(ev["inspections"]["inflight"])
        return {"ok": True, "root_cause": "throughput_saturation", "actions": ["check_gpu_load"],
                "notify": "vision_engineer", "severity": "crit", "evidence": ["진행 중 2건"],
                "message_ko": "진행 중 검사 2건입니다.", "fact": {"ok": True, "hanzi": 0},
                "model": "fake", "device": "cpu", "wall_s": 1.0}
    ag.llm.analyze = fake_llm
    for i, inner in enumerate(("2000026072709204650", "2000026072709245100")):
        eng.feed(Event(ts=_BASE - 200 + 100 * i, ts_text="", kind="INSP_START",
                       inner_id=inner, value=1 - i))
    # 거부된 세 번째 검사: 도착 줄(INSP_START)이 거부 줄·설비 회신보다 먼저 들어온다
    eng.feed(Event(ts=_BASE, ts_text="", kind="INSP_START", inner_id="2000026072709262821",
                   value=0))
    eng.feed(Event(ts=_BASE, ts_text="", kind="INSP_REJECT"))
    ack = Event(ts=_BASE, ts_text="", kind="COMM_MSG", name="V2M_INSPECT_START_ACK",
                extra="V3.0,TALOS3,NoInspThread,2000026072709262821,1641694923,1")
    watch.Extractor._enrich_comm(ack)
    eng.feed(ack)
    assert ag.due() == pytest.approx(_BASE + 10)           # 심각 = 즉시(10초)
    eng.feed(Event(ts=_BASE + 5, ts_text="", kind="POOL_CREATE", name="eFunction"))
    ag.tick(ag.due())
    assert calls == [2]                                     # 경보 순간 상태(재시작 전)
    ob = os.path.join(str(tmp_path), "outbox_replay")
    files = sorted(os.listdir(ob))
    assert len(files) == 2 and files[1].endswith("_llm.eml")
    msgs = [email.message_from_binary_file(open(os.path.join(ob, f), "rb"),
                                           policy=policy.default) for f in files]
    assert "룰 판단" in msgs[0]["Subject"] and "vision@example.com" in msgs[0]["To"]
    assert msgs[1]["In-Reply-To"] == msgs[0]["Message-ID"]
    assert "판단 일치" in msgs[1]["Subject"]
    html = msgs[1].get_body(preferencelist=("html",)).get_content()
    assert "처리량 포화" in html and "진행 중 검사 2건" in html
    rec = [json.loads(ln) for ln in open(os.path.join(str(tmp_path),
                                                      "incidents_20260727_replay.jsonl"),
                                         encoding="utf-8")]
    assert rec[0]["verdict"]["mode"] == "agree" and rec[0]["verdict_initial"]["mode"] == \
        "rule_only"


def test_agent_digest_batches_warn_and_skips_false_alarm(tmp_path):
    cfg, n, eng, ag = _agent_env(tmp_path, min_severity="warn")
    ag.llm = None                                           # 룰 판단만
    n.emit(_a("defect_repeat", "warn", _BASE, "동일 결함 빈발: A", key="a"))
    n.emit(_a("defect_repeat", "warn", _BASE + 30, "동일 결함 빈발: B", key="b"))
    assert ag.due() == pytest.approx(_BASE + 60)            # 주의 = 묶음 60초
    ag.tick(_BASE + 59)
    assert ag.processed == 0
    ag.tick(_BASE + 60)
    assert ag.processed == 1
    rec = json.loads(open(os.path.join(str(tmp_path), "incidents_20260727_replay.jsonl"),
                          encoding="utf-8").readline())
    assert len(rec["alerts"]) == 2 and rec["email"]["eml"]
    # 재시작 1회를 풀 로그 5줄로 센 restart_burst → 오경보 의심(주의)로 강등
    for k in range(5):
        eng.feed(Event(ts=_BASE + 1000 + k * 0.01, ts_text="", kind="POOL_CREATE", name="e"))
    n.emit(_a("restart_burst", "crit", _BASE + 1001, "60분 내 재시작 5회", key="burst"))
    ag.tick(ag.due())
    last = [json.loads(ln) for ln in open(os.path.join(
        str(tmp_path), "incidents_20260727_replay.jsonl"), encoding="utf-8")][-1]
    assert last["verdict"]["label"].startswith("오경보 의심")
    assert last["evidence"]["restarts"]["count_60min"] == 1


# ── 메일 발송 (로컬 SMTP 수신기) ───────────────────────────────
def test_mailer_smtp_auth_routing_and_limits(tmp_path, smtp_sink, monkeypatch):
    cfg = watch.load_config("")
    cfg["site"] = "PC3"
    cfg["email"].update(enabled=True, smtp_host="127.0.0.1",
                        smtp_port=smtp_sink.server_address[1], security="none",
                        username="bot@example.com", password_env="TALOG_TEST_PW",
                        to=["lead@example.com"], roles={"quality": ["q@example.com"]},
                        min_interval_min=5, max_per_hour=2)
    m = Mailer(cfg, str(tmp_path))
    assert not m.check()[0]                                 # 비밀번호 없음
    monkeypatch.setenv("TALOG_TEST_PW", "pw-123")
    assert m.check() == (True, "접속·인증 OK")
    from talog.mailer import sample_incident
    inc = sample_incident("PC3")
    inc["verdict"]["notify"] = ["quality"]
    res = m.send_incident(inc)
    assert res["sent"] and res["to"] == ["lead@example.com", "q@example.com"]
    got = [x for x in smtp_sink.messages if x.get("data")]
    assert got and any("q@example.com" in r for r in got[-1]["rcpt"])
    assert smtp_sink.auth[-1][1:] == [b"bot@example.com", b"pw-123"]
    msg = email.message_from_string(got[-1]["data"], policy=policy.default)
    assert msg["Subject"].startswith("[talog][CRIT][PC3]")
    assert any(p.get_filename() == f"incident_{inc['id']}.json" for p in msg.iter_attachments())
    assert os.path.exists(res["eml"])
    # 속도 제한: 묶음은 간격 5분, 즉시는 시간당 상한만
    t = 1_000_000.0
    tok = m.reserve(t)
    assert m.next_allowed() == t + 300 and m.next_allowed(immediate=True) == t
    m.reserve(t + 10)
    assert m.next_allowed(immediate=True) == t + 3600       # 시간당 2통 상한
    m.release(tok)
    assert m.next_allowed(immediate=True) == t + 10
    assert m.is_immediate("crit") and not m.is_immediate("warn")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 는 Windows 전용")
def test_dpapi_roundtrip_and_mailer_uses_it(tmp_path):
    from talog.secret import protect, unprotect
    blob = protect("앱비밀번호-abcd")
    assert unprotect(blob) == "앱비밀번호-abcd" and "abcd" not in blob
    cfg = watch.load_config("")
    cfg["email"].update(password_env="TALOG_NOPE_ENV", password_dpapi=blob)
    m = Mailer(cfg, str(tmp_path))
    assert m.password() == "앱비밀번호-abcd" and m.password_source() == "암호화 저장(DPAPI)"


# ── 콘솔 API ─────────────────────────────────────────────────
def test_console_dump_config_roundtrip():
    from talog.console import dump_config
    cfg = watch.load_config("")
    cfg["rules"]["patterns"] = [{"name": "비상정지", "match": "re:V2M_EMERGENCY_STOP#1",
                                 "files": ["comm.log"], "severity": "crit", "count": 1}]
    text = dump_config(cfg)
    assert yaml.safe_load(text) == cfg
    assert "# 폴링 주기" in text and "# default(핵심 9종)" in text


def test_console_http_token_host_save(tmp_path):
    from talog.console import Console, _handler_factory
    path = os.path.join(str(tmp_path), "watch.yaml")
    con = Console(path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(con))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(p, body=None, token=True, host=None):
        hdr = {"Content-Type": "application/json"}
        if token:
            hdr["X-Talog-Token"] = con.token
        if host:
            hdr["Host"] = host
        req = urllib.request.Request(base + p, data=None if body is None else
                                     json.dumps(body).encode(), headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, page_cfg = call("/api/config")
        assert code == 200 and page_cfg["cfg"]["tracking"]["mode"] == "default"
        assert len(page_cfg["rules"]) >= 15 and page_cfg["presets"]
        new = page_cfg["cfg"]
        new["site"] = "T1"
        new["rules"]["patterns"] = [{"name": "x", "match": "re:(", "severity": "warn"}]
        assert call("/api/config", {"cfg": new}, token=False)[0] == 403
        assert call("/api/config", {"cfg": new}, host="evil.example")[0] == 403
        code, res = call("/api/config", {"cfg": new})
        assert code == 400 and "정규식 오류" in res["errors"][0]     # 잘못된 패턴은 거부
        new["rules"]["patterns"] = [{"name": "비상정지", "match": "V2M_EMERGENCY_STOP"}]
        pw = "pw-secret-987" if sys.platform == "win32" else ""
        code, res = call("/api/config", {"cfg": new, "password": pw})
        assert code == 200 and res["ok"]
        saved = open(path, encoding="utf-8").read()
        assert yaml.safe_load(saved)["site"] == "T1" and "pw-secret-987" not in saved
        if pw:
            assert res["cfg"]["email"]["has_password"] and not res["cfg"]["email"][
                "password_dpapi"]
        call("/api/config", {"cfg": new})
        assert os.path.exists(path + ".bak")
        code, r = call("/api/test/pattern", {"pattern": {"match": "NoInspThread",
                                                          "files": ["comm.log"]},
                                             "line": "...ACK,V3.0,TALOS3,NoInspThread,1",
                                             "file": "Comm.log"})
        assert r["match"] and r["file_ok"]
    finally:
        srv.shutdown()
        srv.server_close()
