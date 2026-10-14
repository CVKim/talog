"""룰 진단 — 근거로 원인 하나를 고르고 확인한 사실(facts)과 조치를 붙인다."""

from __future__ import annotations

from .interpret import _gap_text, completion_gap
from .runbook import Runbook


class RuleDiagnoser:
    """런북 원인을 근거 수치로 판정한다 (결정적, 즉시). LLM 과 교차 확인하는 기준 의견."""

    def __init__(self, rb: Runbook, cfg: dict):
        self.rb = rb
        self.cfg = cfg

    def _out(self, family, cause, facts, confirmed=True) -> dict:
        spec = self.rb.cause_spec(family, cause)
        actions = list(spec.get("actions") or ["escalate_to_human"])
        return {"cause": cause, "name": self.rb.cause_name(family, cause),
                "facts": facts[:5], "confirmed": confirmed,
                "actions": actions, "notify": list(spec.get("notify") or []),
                "strength": len(facts)}

    def diagnose(self, family: str, ev: dict, alerts: list, sys_cause: str = "") -> dict:
        if family == "reject":
            return self._reject(ev, alerts)
        if family == "defect":
            return self._defect(ev, alerts)
        return self._system(ev, sys_cause, alerts)

    def _reject(self, ev: dict, alerts: list = ()) -> dict:
        p = self.rb.params
        g, r, t, i = ev["gpu_log"], ev["restarts"], ev["timeouts"], ev["inspections"]
        m, er = ev["model_load"], ev["errors"]
        w = int(p["gpu_fault_window_s"])
        nf, ne, ns = (g.get("fatal_near") or 0), (g.get("infer_errors_near") or 0), \
            (er.get("seh_near") or 0)
        if nf or ne or ns:
            f = [f"경보 ±{w}초 GPU 치명 오류(enqueueV3) {nf}건·인퍼런스 오류 {ne}건·"
                 f"SEH 예외 {ns}건"]
            if g.get("first_fatal"):
                f.append(f"첫 GPU 치명 오류 {g['first_fatal']}")
            if er.get("crash_near"):
                f.append("같은 구간 크래시 동반")
            return self._out("reject", "gpu_context_fault", f)
        since = r.get("since_restart_s")
        if (since is not None and since <= p["not_init_after_restart_s"]) or m.get("pending"):
            f = []
            if since is not None:
                f.append(f"마지막 재시작 {r.get('last_restart')} 후 {since:.0f}초 만의 거부")
            if m.get("pending"):
                f.append(f"모델 로드 명령 {m.get('last_cmd')} 뒤 완료 응답 없음")
            return self._out("reject", "not_initialized", f)
        inflight = i.get("inflight") or 0
        if (t.get("img_timeout_30min") or 0) >= 1 and inflight <= 1:
            return self._out("reject", "slot_leak_timeout", [
                f"30분 내 이미지 처리 타임아웃 {t['img_timeout_30min']}건(마지막 "
                f"{t.get('last')}) 뒤 거부", f"진행 중 검사 {inflight}건뿐인데 스레드 없음"])
        if completion_gap(i, ev.get("capacity") or {}):
            c = ev.get("capacity") or {}
            w = i.get("wait_threads_recent") or []
            f = [_gap_text(i),
                 f"최근 도착 순간 유휴 스레드 {', '.join(str(x) for x in w)}개 / 스레드 "
                 f"{c.get('pool') or i.get('pool')}개 — 스레드는 놀고 있음"]
            if c.get("hold_source") == "seq" and c.get("hold_p50") is not None:
                f.append(f"seq 점유 p50 {c['hold_p50']}초 · p99 {c.get('hold_p99')}초 — 처리는 평소대로")
            f.append(f"진행 중 {inflight}건(가장 오래된 것 {i.get('oldest_inflight_s')}초)은 완료 신호가 "
                     f"없어 쌓인 것")
            return self._out("reject", "completion_signal_lost", f)
        iso = self._isolated(ev, alerts)
        if iso:
            return self._out("reject", "isolated_stall", iso)
        slow = self._progressive(ev)
        if slow:
            return self._out("reject", "progressive_slowdown", slow)
        f = []
        sd, ed = i.get("slowdown_ratio"), g.get("exec_slowdown_ratio")
        doi = i.get("duration_over_interval")
        pool = i.get("pool")
        if doi and doi >= 1.0:
            if pool:
                # 스레드가 여럿인 그룹은 소요가 투입 간격보다 긴 게 정상 — 스레드 수로 나눠 본다
                f.append(f"검사 소요가 투입 간격({i.get('recent_interval_s')}초)의 {doi}배 — "
                         f"스레드 {pool}개로 감당할 수 있는 한계의 {doi / pool * 100:.0f}%")
            else:
                f.append(f"검사 소요가 투입 간격({i.get('recent_interval_s')}초)의 {doi}배 — "
                         f"투입이 처리보다 빠름")
        if sd and sd >= p["slowdown_ratio"]:
            f.append(f"최근 완료 소요 {i.get('recent_durations_s')}초 — 평소 중앙값 "
                     f"{i.get('baseline_duration_s')}초의 {sd}배")
        if ed and ed >= p["exec_slowdown_ratio"]:
            f.append(f"executeV2 {g.get('exec_slowest_model')} 중앙값 "
                     f"{g.get('exec_p50_before_ms')}→{g.get('exec_p50_recent_ms')}ms "
                     f"({ed}배)")
        if inflight >= 2:
            f.append(f"진행 중 검사 {inflight}건 (가장 오래된 것 "
                     f"{i.get('oldest_inflight_s')}초 경과)")
        if f:
            if i.get("recent_interval_s"):
                f.append(f"최근 투입 간격 중앙값 {i['recent_interval_s']}초")
            return self._out("reject", "throughput_saturation", f)
        return self._out("reject", "unknown", ["거부 전후로 느려짐·GPU 오류·타임아웃·"
                                               "재시작 근거가 없음"])

    @staticmethod
    def _isolated(ev: dict, alerts) -> list[str]:
        """단건 정체 — 정체 경보인데 남은 검사가 스레드 수의 절반 이하이고 최근 도착에 유휴 스레드가
        있다. 스레드가 모자란 것이 아니므로 처리량 포화가 아니다(소요 배율은 남은 건의 경과라 부푼 값)."""
        rules = {getattr(a, "rule", "") for a in alerts}
        if "insp_stall" not in rules or "no_insp_thread" in rules:
            return []
        i, c = ev.get("inspections") or {}, ev.get("capacity") or {}
        n = i.get("inflight") or 0
        pool = c.get("pool") or i.get("pool")
        w = i.get("wait_threads_recent") or []
        if n < 1 or n > (max(1, pool // 2) if pool else 2) or (w and max(w) < 1):
            return []
        f = [f"진행 중 {n}건만 완료 신호 없이 남음 (가장 오래된 것 {i.get('oldest_inflight_s')}초)",
             (f"최근 도착 순간 유휴 스레드 {', '.join(str(x) for x in w)}개"
              + (f" / 스레드 {pool}개" if pool else "") + " — 스레드는 남아 있음") if w else "최근 도착 없음"]
        if i.get("recent_durations_s"):
            f.append(f"다른 검사의 최근 완료 소요 {i['recent_durations_s']}초")
        if i.get("inflight_ids"):
            f.append(f"남은 inner id {', '.join(i['inflight_ids'][:2])}")
        return f

    def _progressive(self, ev: dict) -> list[str]:
        """시간 경과형 처리 열화 판정 — 근거 문장 목록 (아니면 빈 목록).

        기준선 대비 점유 시간이 늘었거나 예비(유휴) 스레드가 줄었고, 투입 간격은 그대로이고
        (부하 변화 아님) GPU 커널 시간도 그대로이며(경합 아님) 증가가 꾸준할 때."""
        p = self.rb.params
        t, c = ev.get("trend") or {}, ev.get("capacity") or {}
        hr = t.get("hold_ratio")
        if hr is None:
            return []
        im, ib = c.get("idle_median"), c.get("idle_baseline")
        drop = im is not None and ib is not None and im <= ib - 1
        chg = t.get("interval_change_pct")
        ed = (ev.get("gpu_log") or {}).get("exec_slowdown_ratio")
        if not (hr >= p["drift_ratio"] or drop):
            return []
        if chg is not None and abs(chg) > p["interval_tol_pct"]:
            return []                                   # 투입이 바뀌었다 — 부하 변화
        if ed is not None and ed >= p["exec_slowdown_ratio"]:
            return []                                   # GPU 커널이 느려졌다 — 경합 쪽
        if t.get("monotonic") is False:
            return []                                   # 오르내림 — 시간 경과형이 아니다
        g = c.get("group")
        f = [f"그룹 {g} 점유 시간 {t.get('hold_p50_baseline')}→{t.get('hold_p50_now')}초 "
             f"({hr}배, 기준선 {t.get('since')}부터"
             + (f", 순위상관 {t.get('spearman_hourly')})" if t.get("spearman_hourly") is not None else ")")]
        if ib is not None and im is not None:
            f.append(f"유휴 스레드 중앙값 {ib:g}→{im:g}개"
                     + (f", 일시 지연 내성 {c['tolerance_s']}초(한계 {c['limit_s']}초)"
                        if c.get("tolerance_s") is not None else ""))
        f.append(f"투입 간격 {t.get('interval_baseline_s')}→{t.get('interval_now_s')}초"
                 + (f", GPU 커널 {ed}배" if ed is not None else ""))
        nr, nb = len(t.get("groups_rising") or []), t.get("groups_with_baseline") or 0
        if nb >= 2:
            f.append(f"기준선 있는 {nb}개 그룹 중 {nr}개 동반 증가")
        tr = ev.get("transient") or {}
        if tr.get("groups"):
            f.append(f"직전 60초 {tr.get('n_groups')}개 그룹 동시 지연")
        return f

    def _defect(self, ev: dict, alerts: list) -> dict:
        p = self.rb.params
        d, t, er, m = ev["defects"], ev["timeouts"], ev["errors"], ev["model_load"]
        rules = {a.rule for a in alerts}
        top = d.get("top") or []
        forced_n = ((t.get("alg_timeout_30min") or 0) + (er.get("grab_fail_30min") or 0)
                    + (er.get("light_unstable_30min") or 0))
        tname = [n for n, _c in top if "TIME_OUT" in n.upper() or "TIMEOUT" in n.upper()]
        if forced_n or tname:
            f = [f"30분 내 알고리즘 타임아웃 {t.get('alg_timeout_30min', 0)}건·그랩 실패 "
                 f"{er.get('grab_fail_30min', 0)}건·조명 불안정 "
                 f"{er.get('light_unstable_30min', 0)}건"]
            if tname:
                f.append(f"결함명 {', '.join(tname)} (시스템 강제 NG)")
            return self._out("defect", "system_forced_ng", f)
        ms = m.get("minutes_since")
        rec, bef = d.get("ng_rate_recent"), d.get("ng_rate_before_change")
        if (ms is not None and ms <= p["recipe_change_window_min"] and rec is not None
                and bef is not None and rec >= max(2 * bef, bef + 20)
                and m.get("changed") is not False):
            return self._out("defect", "recipe_mismatch", [
                f"기종 로드 {m.get('last_cmd')}({m.get('name')}) 후 {ms}분",
                f"NG 비율 {bef}% → {rec}%"])
        f = []
        if top:
            f.append("30분 결함 분포 " + ", ".join(f"{n} {c}건" for n, c in top[:3]))
        if d.get("streak"):
            f.append(f"연속 NG {d['streak']}건")
        if d.get("ng_rate_recent") is not None:
            f.append(f"최근 {d.get('inspections_recent')}검사 NG {d['ng_rate_recent']}% "
                     f"(기준선 {d.get('ng_rate_baseline')}%)")
        if rules & {"ng_streak", "defect_repeat", "ng_rate"}:
            return self._out("defect", "defect_burst", f)
        if "defect_critical" in rules:
            hits = d.get("critical_hits") or []
            if hits:
                f.insert(0, "치명 결함 " + ", ".join(
                    f"{h['name']}(inner {h['inner_id']}, {h['time']})" for h in hits[-3:]))
            return self._out("defect", "critical_defect", f)
        return self._out("defect", "unknown", f or ["결함 경보의 근거 부족"])

    def _system(self, ev: dict, cause: str, alerts: list) -> dict:
        cause = cause if cause in self.rb.causes("system") else "unknown"
        facts = [f"{a.title} — {a.evidence}"[:160] for a in alerts[:2]]
        confirmed = True
        if cause == "restart_loop":
            r = ev["restarts"]
            need = int(self.cfg["rules"].get("restart_burst", {}).get("count", 3))
            facts = [f"실제 재시작 {r['count_60min']}회(30초 안의 흔적 병합, 풀 생성 로그 "
                     f"{r['pool_log_lines_60min']}줄)", f"kill 스크립트 {r['kill_60min']}회"]
            if r["count_60min"] < need:
                confirmed = False
                facts.append(f"기준 {need}회 미만 — 풀 생성 로그 중복 계수로 인한 오경보 의심")
        elif cause == "process_crash":
            er, g = ev["errors"], ev["gpu_log"]
            if er.get("seh_near"):
                facts.append(f"SEH 예외 {er['seh_near']}건 {er.get('seh_codes')}")
            if g.get("fatal_near"):
                facts.append(f"GPU 치명 오류 {g['fatal_near']}건 동반")
        elif cause == "custom_pattern":
            facts = []
            for p in ev.get("patterns") or []:
                facts.append(f"패턴 '{p['name']}' — " + (p["lines"][-1] if p["lines"]
                                                          else "원문 없음"))
            facts = facts or [f"{a.title} — {a.evidence}"[:160] for a in alerts[:2]]
        return self._out("system", cause, facts, confirmed)
