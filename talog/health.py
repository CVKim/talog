"""설비 건강 — 이미 쌓이는 기록(시간별 KPI·기준선·판정 기록·GPU)으로 '곧 손볼 곳'을 일찍, 이유와 함께 알린다.

경보 규칙은 바꾸지 않는다 — 화면(운영 현황의 '설비 건강')과 하루 정리에만 쓴다. 남은 시간(언제 한계에 닿나)은 예측하지
않는다: 2.2 검토에서 04시 직선 예측은 '48~62시간 남음'이었는데 05:02 에 순간 튐(+3.6초)으로 거부가 났다(CHANGELOG 2.2.0).

항목 (점수 100 = 이상 없음 · 80 이상 좋음 · 50~79 주의 · 50 아래 점검)
  drift    처리 시간 흐름 — 그룹마다 시간당 느린 검사(p99) 점유를 기준선에 대어 위로만 누적합(CUSUM).
           한 칸 = 기준의 5%, 기준보다 5% 넘게 늘어난 만큼만 쌓고(k = 1) 4칸 주의 · 8칸 위험 — 증가 폭도 +10% · +15% 이상일 때만.
           마지막으로 0 이던 다음 시간 = '늘기 시작한 때'   (NIST/SEMATECH e-Handbook 6.3.2.3 CUSUM)
  spike    버틸 여유 vs 지난 순간 튐 — 여유(한계 − 지금 p99)가 지난 기록에서 시간마다 '최대 − 가운데' 점유 차이(튐)의
           최댓값보다 작으면 위험, 상위 5% 값보다 작으면 주의 (12시간 이상 쌓였을 때만)
  quality  판정 분포 — 최근 2시간 NG 비율을 그 앞 기록 p0 에 대어 p-관리도(3σ 주의 · 6σ 위험, 1%p 이상 차이일 때만),
           결함 종류 비율 PSI(0.1 주의 · 0.25 위험, Evidently 기본값). 검사 200건·결함 30건 이상일 때만
  device   장비 — GPU 온도(경보 임계 5°C 안 주의 · 임계 이상 위험), talos 메모리 증가(최근 6시간 기울기, 경보 기준 이상 주의)
  open     마무리 안 된 오늘 사건 — 심각이면 위험, 주의면 주의
설비 점수 = 가장 낮은 항목 점수 (평균은 위험 하나를 가린다).
"""

from __future__ import annotations

import math
import statistics

WORD = {"ok": "좋음", "warn": "주의", "risk": "점검", "none": "기록 쌓는 중"}
STEP, K, H_WARN, H_RISK = 0.05, 1.0, 4.0, 8.0
WARN_PCT, RISK_PCT = 10.0, 15.0
SPIKE_MIN_H = 12
Q_MIN_N, Q_MIN_DEF, Q_WIN_H = 200, 30, 2


def _grade(state: str, x: float = 0.0) -> int:
    """상태와 정도(0~1) → 점수. 좋음 85~100 · 주의 50~79 · 점검 20~49"""
    x = max(0.0, min(1.0, x))
    return {"ok": 100 - round(15 * x), "warn": 79 - round(29 * x), "risk": 49 - round(29 * x)}.get(state, 100)


def _hh(hour: str) -> str:
    """'2026-10-12 03:00' → '10/12 03시'"""
    return f"{hour[5:7]}/{hour[8:10]} {hour[11:13]}시" if len(hour) >= 13 else hour


def _q(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def item(kind, key, title, state, x=0.0, reason="", todo="", rank=0.0, **more) -> dict:
    """rank = 점수가 같을 때 더 급한 것(클수록 먼저) — 한계를 이미 넘은 그룹 > 사건 > 그 밖은 정도"""
    return {"kind": kind, "key": key, "title": title, "state": state,
            "score": None if state == "none" else _grade(state, x), "reason": reason, "todo": todo, "rank": rank, **more}


# ── drift: 처리 시간 흐름 ─────────────────────────────────────────────
def cusum_up(xs: list[float], mu: float, step: float, k: float = K) -> tuple[list[float], int]:
    """위로만 누적합. (누적합들, 마지막으로 0 이던 다음 칸 — 늘기 시작한 곳)"""
    s, out, start = 0.0, [], 0
    for i, x in enumerate(xs):
        s = max(0.0, s + (x - mu) / step - k)
        out.append(s)
        if s == 0.0:
            start = i + 1
    return out, start


def drift(group, series: list[tuple[str, float]], base_p99: float | None) -> dict:
    """그룹 하나 — series = [(시, p99)] 시간순(기준선 뒤), base_p99 = 기준선의 p99(없으면 처음 3시간의 가운데 값)"""
    pts = [(h, v) for h, v in series if v is not None]
    title = f"그룹 {group} 처리 시간"
    if base_p99 is None:
        if len(pts) < 4:
            return item("drift", f"drift:{group}", title, "none", reason="기준선을 잡는 중입니다")
        base_p99, pts = statistics.median(v for _h, v in pts[:3]), pts[3:]
    if not pts or not base_p99:
        return item("drift", f"drift:{group}", title, "none", reason="기준선 뒤 기록이 아직 없습니다")
    step = max(STEP * base_p99, 0.02)
    s, start = cusum_up([v for _h, v in pts], base_p99, step)
    last, now = pts[-1][1], s[-1]
    pct = 100 * (last - base_p99) / base_p99
    # 누적합만으로는 평소보다 조금(5~9%) 높은 상태가 며칠 이어져도 쌓인다 — 증가 폭도 넘을 때만 주의(+10%)·점검(+15%)
    st = "risk" if now >= H_RISK and pct >= RISK_PCT else "warn" if now >= H_WARN and pct >= WARN_PCT else "ok"
    since = _hh(pts[min(start, len(pts) - 1)][0])
    if st == "ok":
        return item("drift", f"drift:{group}", title, "ok", min(1.0, now / H_WARN),
                    f"그룹 {group} 느린 검사 처리 시간 {last:.2f}초 — 기준 {base_p99:.2f}초와 비슷합니다",
                    cusum=round(now, 1), base=base_p99, last=last, pct=round(pct))
    return item("drift", f"drift:{group}", title, st, min(1.0, (now - (H_RISK if st == "risk" else H_WARN)) / H_RISK),
                f"그룹 {group} 느린 검사 처리 시간이 {since}부터 늘고 있습니다 — 기준 {base_p99:.2f}초 → 최근 {last:.2f}초 ({pct:+.0f}%)",
                "다음 계획 정지(쉬는 시간) 때 재시작을 계획하십시오 — 시간이 갈수록 느려지는 처리 열화일 수 있습니다",
                rank=pct, cusum=round(now, 1), since=since, base=base_p99, last=last, pct=round(pct))


# ── spike: 버틸 여유 vs 지난 순간 튐 ─────────────────────────────────
def spike(group, history: list[dict], p99_now: float | None, limit_now: float | None) -> dict:
    """history = 그 그룹의 시간별 KPI 행들(지난 기록), p99_now·limit_now = 지금(또는 마지막 시간) 값"""
    title = f"그룹 {group} 버틸 여유"
    sp = [r["hold_max"] - r["hold_p50"] for r in history
          if r.get("hold_max") is not None and r.get("hold_p50") is not None and (r.get("n") or 0) >= 50]
    if p99_now is None or not limit_now:
        return item("spike", f"spike:{group}", title, "none", reason="지금 값이 없습니다")
    room = limit_now - p99_now
    if room <= 0:
        return item("spike", f"spike:{group}", title, "risk", 1.0,
                    f"그룹 {group} 느린 검사({p99_now:.2f}초)가 이미 한계({limit_now:.2f}초)를 넘습니다 — 검사 시작 거부가 날 수 있습니다",
                    "다음 정지 때 바로 재시작하고, 그 전까지 투입·GPU 부하를 늘리지 마십시오", rank=1000 - room, room=round(room, 2))
    if len(sp) < SPIKE_MIN_H:
        return item("spike", f"spike:{group}", title, "none",
                    reason=f"순간 튐 기록이 쌓이는 중입니다 ({len(sp)}/{SPIKE_MIN_H}시간) — 지금 여유 {room:.1f}초", room=round(room, 2))
    big, p95 = max(sp), _q(sp, 0.95)
    if room < big:
        return item("spike", f"spike:{group}", title, "risk" if room < p95 else "warn", min(1.0, 1 - room / big),
                    f"그룹 {group} 여유 {room:.1f}초 — 지난 {len(sp)}시간 가운데 가장 큰 순간 튐 {big:.1f}초를 버티지 못합니다",
                    "다음 계획 정지 때 재시작을 계획하십시오 — 순간 지연이 오면 검사 시작 거부가 날 수 있습니다",
                    rank=300 + big - room, room=round(room, 2), spike_max=round(big, 2), spike_p95=round(p95, 2))
    return item("spike", f"spike:{group}", title, "ok", min(1.0, p95 / room) if room else 1.0,
                f"그룹 {group} 여유 {room:.1f}초 — 지난 가장 큰 순간 튐 {big:.1f}초도 버팁니다",
                room=round(room, 2), spike_max=round(big, 2))


# ── quality: 판정 분포 ─────────────────────────────────────────────────
def psi(base: dict, cur: dict, top: int = 8) -> tuple[float, str, float, float]:
    """결함 종류 비율 PSI — (값, 가장 많이 늘어난 결함, 평소 비율, 최근 비율). 칸이 0 이면 0.5/n 을 더한다.
    가리키는 결함은 늘어난 쪽에서 고른다 — 할 일이 '그 결함 이미지를 열어 보라'이므로"""
    keys = [k for k, _v in sorted({**base, **cur}.items(), key=lambda kv: -(base.get(kv[0], 0) + cur.get(kv[0], 0)))][:top]
    nb, nc = sum(base.values()), sum(cur.values())
    if not nb or not nc:
        return 0.0, "", 0.0, 0.0
    total, worst = 0.0, ("", 0.0, 0.0, -1.0)
    up = worst
    for k in keys + ["__기타"]:
        b = base.get(k, 0) if k != "__기타" else nb - sum(base.get(x, 0) for x in keys)
        c = cur.get(k, 0) if k != "__기타" else nc - sum(cur.get(x, 0) for x in keys)
        e, a = (b + 0.5) / (nb + 0.5), (c + 0.5) / (nc + 0.5)
        part = (a - e) * math.log(a / e)
        total += part
        if k != "__기타" and part > worst[3]:
            worst = (k, b / nb, c / nc, part)
        if k != "__기타" and a > e and part > up[3]:
            up = (k, b / nb, c / nc, part)
    pick = up if up[0] else worst
    return total, pick[0], pick[1], pick[2]


def quality(rows: list[dict]) -> dict:
    """rows = group 'quality' 시간별 행 시간순. 최근 2시간 vs 그 앞 기록"""
    title = "판정 분포"
    if len(rows) <= Q_WIN_H:
        return item("quality", "quality", title, "none", reason="판정 기록이 쌓이는 중입니다")
    cur, base = rows[-Q_WIN_H:], rows[:-Q_WIN_H]
    n_c, n_b = sum(r.get("n", 0) for r in cur), sum(r.get("n", 0) for r in base)
    if n_c < Q_MIN_N or n_b < Q_MIN_N:
        return item("quality", "quality", title, "none", reason=f"검사 {Q_MIN_N}건 이상 쌓이면 봅니다")
    p0, p = sum(r.get("ng", 0) for r in base) / n_b, sum(r.get("ng", 0) for r in cur) / n_c
    sig = math.sqrt(max(p0 * (1 - p0), 1e-9) / n_c)
    z = (p - p0) / sig if sig else 0.0
    gap = p - p0
    out = item("quality", "quality", title, "ok", min(1.0, max(0.0, z) / 3), f"NG 비율 최근 2시간 {p:.1%} — 평소 {p0:.1%}와 비슷합니다",
               p0=round(p0, 4), p=round(p, 4))
    if z >= 3 and gap >= 0.01:
        out = item("quality", "quality", title, "risk" if z >= 6 and gap >= 0.02 else "warn", min(1.0, (z - 3) / 6),
                   f"NG 비율이 평소 {p0:.1%} → 최근 2시간 {p:.1%}로 늘었습니다",
                   "카메라·조명 오염과 기종 교체·소재 변경을 먼저 확인하십시오 — 제품이 아니라 촬상 조건이 바뀌었을 수 있습니다",
                   rank=200 + z, p0=round(p0, 4), p=round(p, 4))
    bd, cd = {}, {}
    for r in base:
        for k, v in (r.get("defects") or {}).items():
            bd[k] = bd.get(k, 0) + v
    for r in cur:
        for k, v in (r.get("defects") or {}).items():
            cd[k] = cd.get(k, 0) + v
    if sum(bd.values()) >= Q_MIN_DEF and sum(cd.values()) >= Q_MIN_DEF:
        v, k, e, a = psi(bd, cd)
        if v >= 0.1:
            st = "risk" if v >= 0.25 else "warn"
            alt = item("quality", "quality", title, st, min(1.0, (v - 0.1) / 0.3),
                       f"결함 종류 비율이 평소와 다릅니다 — '{k}' {e:.0%} → {a:.0%} (PSI {v:.2f})",
                       "그 결함 이미지를 몇 장 열어 보고 실제 불량인지 과검출인지 확인하십시오 — 과검출이면 촬상 조건·모델 점검",
                       rank=200 + 10 * v, psi=round(v, 3), defect=k)
            if alt["score"] < out["score"]:
                out = alt
        out["psi"] = round(v, 3)
    return out


# ── device · open ───────────────────────────────────────────────────
def device(gpus: list[dict], temp_limit: float, mem_rows: list[dict], mb_per_hour: float) -> list[dict]:
    out = []
    for g in gpus:
        t = g.get("temp")
        if t is None:
            continue
        st = "risk" if t >= temp_limit else "warn" if t >= temp_limit - 5 else "ok"
        out.append(item("device", f"gpu:{g.get('gpu')}", f"GPU{g.get('gpu')} 온도", st,
                        min(1.0, max(0.0, (t - (temp_limit - 15)) / 15)),
                        f"GPU{g.get('gpu')} {t}°C (경보 {temp_limit:.0f}°C)",
                        "냉각팬·필터·PC 주변 통풍을 확인하십시오" if st != "ok" else "", rank=400 + t if st == "risk" else t, temp=t))
    pts = [(i, r["mem_mb_max"]) for i, r in enumerate(mem_rows[-6:]) if r.get("mem_mb_max") is not None]
    if len(pts) >= 4:
        mx, my = statistics.mean(p[0] for p in pts), statistics.mean(p[1] for p in pts)
        den = sum((x - mx) ** 2 for x, _y in pts) or 1
        slope = sum((x - mx) * (y - my) for x, y in pts) / den
        st = "risk" if slope >= 2 * mb_per_hour else "warn" if slope >= mb_per_hour else "ok"
        out.append(item("device", "memory", "talos 메모리", st, min(1.0, max(0.0, slope / (2 * mb_per_hour))),
                        f"talos 메모리 최근 {len(pts)}시간 {slope:+,.0f}MB/h (기준 {mb_per_hour:,.0f}MB/h)",
                        "메모리가 계속 늘고 있습니다 — 다음 계획 정지 때 재시작을 계획하십시오" if st != "ok" else "",
                        slope=round(slope)))
    return out


def open_incidents(incs: list[dict]) -> dict:
    op = [r for r in incs if (r.get("state") or "open") in ("open", "ack")]
    crit = [r for r in op if r.get("severity") == "crit"]
    if crit:
        return item("open", "open", "마무리 안 된 사건", "risk", 0.4, f"심각 사건 {len(crit)}건이 아직 마무리되지 않았습니다",
                    "사건 화면에서 원인과 할 일을 확인하고 '해결 — 이대로' 또는 '오경보'로 마무리하십시오", rank=500)
    if op:
        return item("open", "open", "마무리 안 된 사건", "warn", 0.0, f"사건 {len(op)}건이 아직 마무리되지 않았습니다",
                    "사건 화면에서 마무리하십시오")
    return item("open", "open", "마무리 안 된 사건", "ok", 0.0, "오늘 사건은 모두 마무리됐습니다" if incs else "오늘 사건이 없습니다")


# ── 합치기 ───────────────────────────────────────────────────────────
def summarize(items: list[dict]) -> dict:
    """설비 점수 = 가장 낮은 항목. 할 일은 주의·점검 항목에서 겹치지 않게 최대 3개"""
    scored = [i for i in items if i["score"] is not None]
    if not scored:
        return {"score": None, "state": "none", "label": WORD["none"], "top": None, "todo": [], "items": items}
    order = sorted(scored, key=lambda i: (i["score"], -i["rank"]))
    top, sc = order[0], order[0]["score"]
    state = "ok" if sc >= 80 else "warn" if sc >= 50 else "risk"
    # 같은 종류로 주의·점검인 곳이 더 있으면 '외 N곳' (예: 그룹 12개가 모두 느려짐)
    more = sum(1 for i in scored if i is not top and i["kind"] == top["kind"] and i["state"] in ("warn", "risk"))
    todo, seen = [], set()
    for i in order:
        if i["state"] in ("warn", "risk") and i["todo"] and i["todo"] not in seen:
            seen.add(i["todo"])
            todo.append({"text": i["todo"], "why": i["reason"], "title": i["title"], "state": i["state"], "key": i["key"]})
    return {"score": sc, "state": state, "label": WORD[state], "top": top, "more": more, "todo": todo[:3],
            "items": order + [i for i in items if i["score"] is None]}
