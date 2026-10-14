"""설정 화면의 시험 — LLM 응답·메일 연결/발송·경보 규칙 한 줄 시험·시험 경보 넣기."""

from __future__ import annotations

import copy
import time
from fnmatch import fnmatchcase

from .. import settings
from ..tracker import compile_patterns


class ChecksMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    def test_llm(self, body: dict) -> dict:
        from ..llm import OllamaClient
        lc = copy.deepcopy(self.cfg["llm"])
        for k in ("url", "model"):
            if body.get(k):
                lc[k] = body[k]
        lc["timeout_s"] = 300
        cli = OllamaClient(lc)
        if not cli.alive():
            return {"ok": False, "error": f"Ollama 서버 응답 없음: {cli.url}"}
        if cli.model not in cli.models():
            return {"ok": False, "error": f"모델 {cli.model} 가 서버에 없습니다 (ollama pull)"}
        msgs = [{"role": "system", "content": "한국어로만 답한다."},
                {"role": "user", "content": "검사 설비에서 NoInspThread 거부가 났다. 담당자에게 보낼 "
                                            "한 문장 알림을 써라."}]
        dev = body.get("device")
        r = cli.chat(msgs, num_predict=80, device=dev if dev in ("cpu", "gpu") else None)
        if r["error"]:
            return {"ok": False, "error": r["error"], "device": r["device"]}
        gen_s = max(0.01, r["wall_s"] - r["load_s"])
        return {"ok": True, "device": r["device"], "reason": r["device_reason"],
                "wall_s": r["wall_s"], "load_s": r["load_s"],
                "tok_s": round(r["gen_tokens"] / gen_s, 1), "reply": r["content"][:300],
                "model": cli.model}

    def llm_models(self, url: str) -> dict:
        from ..llm import OllamaClient
        cli = OllamaClient({"url": url or self.cfg["llm"].get("url")})
        alive = cli.alive()
        return {"alive": alive, "url": cli.url, "models": cli.models() if alive else []}

    def _mailer_for(self, body: dict):
        from ..mailer import Mailer
        s = self._with_secret(body.get("settings") or self.settings)
        cfg = settings.compile(s)
        m = Mailer(cfg, self.data_dir)
        typed = body.get("secret") or ""
        if typed:                                 # 저장 전 입력값으로 시험 (저장·출력 안 함)
            m.password = lambda: typed
            m.graph_secret = lambda: typed
        return m, s, bool(typed)

    def test_mail_check(self, body: dict) -> dict:
        m, s, typed = self._mailer_for(body)
        if s["mail"]["provider"] == "off":
            return {"ok": False, "detail": "발송 방식이 '보내지 않음' 입니다"}
        ok, detail = m.check()
        return {"ok": ok, "detail": detail, "transport": m.transport,
                "secret_source": "입력값" if typed else (m.password_source() or "없음")}

    def test_mail(self, body: dict) -> dict:
        from ..mailer import sample_incident
        m, s, _typed = self._mailer_for(body)
        if s["mail"]["provider"] == "off":
            return {"ok": False, "error": "발송 방식이 '보내지 않음' 입니다"}
        errs = settings.validate(s)
        if errs:
            return {"ok": False, "error": " / ".join(errs)}
        inc = sample_incident(s.get("site") or "설비")
        inc["verdict"]["notify"] = list((m.e.get("roles") or {}).keys())
        res = m.send_incident(inc)
        res["ok"] = bool(res.get("sent") or res.get("dry_run"))
        return res

    def test_rule(self, body: dict) -> dict:
        """경보 규칙 시험: 로그 한 줄(log 형) 또는 결함명(defect 형)이 걸리는가."""
        text = str(body.get("text") or "")
        fname = str(body.get("file") or "")
        out = []
        for i, it in enumerate(body.get("rules") or []):
            if not isinstance(it, dict):
                continue
            name = it.get("name") or f"규칙{i + 1}"
            if it.get("type") == "defect":
                pats = settings._as_list(it.get("match"))
                hit = any(fnmatchcase(text.strip().upper(), p.upper()) for p in pats)
                out.append({"name": name, "ok": True, "match": hit, "file_ok": True})
                continue
            rules, errs = compile_patterns([{k: v for k, v in it.items() if k != "type"}])
            if errs or not rules:
                out.append({"name": name, "ok": False, "error": "; ".join(errs) or "비어 있음"})
                continue
            r = rules[0]
            file_ok = (not fname) or r.applies(fname)
            out.append({"name": name, "ok": True, "match": bool(r.rx.search(text)) and file_ok,
                        "file_ok": file_ok})
        return {"ok": True, "results": out}

    def inject(self, kind: str) -> dict:
        if self.watch is None or not self.running():
            return {"ok": False, "error": "감시가 정지 상태입니다 — 먼저 시작하십시오"}
        from ..watch import Alert
        now = time.time()
        samples = {
            "noinsp": Alert(now, "no_insp_thread", "crit",
                            "[시험] 검사 시작 거부(NoInspThread) — 미검사 임박",
                            "콘솔에서 넣은 시험 경보입니다.", key="test-noinsp",
                            cooldown_min=0.05),
            "defect": Alert(now, "defect_critical", "crit", "[시험] 치명 결함 검출: TEST_DEFECT",
                            "콘솔에서 넣은 시험 경보입니다.", key="test-defect",
                            cooldown_min=0.05),
            "warn": Alert(now, "pattern", "warn", "[시험] 로그 문구 '시험' 3회",
                          "콘솔에서 넣은 주의 등급 시험 경보입니다.", key="test-warn",
                          cooldown_min=0.05),
        }
        a = samples.get(kind)
        if a is None:
            return {"ok": False, "error": f"알 수 없는 종류: {kind}"}
        self.watch.notifier.emit(a)
        return {"ok": True, "message": f"{a.title} — 실제 경보와 같은 경로로 처리됩니다"}
