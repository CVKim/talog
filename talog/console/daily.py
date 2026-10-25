"""하루 정리 — 한 번 눌러 여는 인쇄용 한 장(교대 인수인계·보고용). 이미 쌓인 기록으로만 만든다(LLM 을 쓰지 않는다).

  설비 건강(그날 끝까지의 기록으로) · 할 일 · 숫자(검사·NG 비율·경보·사건과 마무리·가장 빠듯한 여유) ·
  시간별 표(검사·NG·가장 낮은 버틸 여유·거부·경보) · 사건 목록(상태 포함)
페이지는 파일 하나·외부 리소스 없음(현장 PC 오프라인). 브라우저의 인쇄(Ctrl+P)로 PDF 로 남긴다.
"""

from __future__ import annotations

import datetime as dt
import glob
import html
import json
import os

from .. import __version__
from .health import _rows

_E = html.escape
_CSS = """
:root{color-scheme:light;--ink:#101828;--ink2:#344054;--sub:#475467;--mute:#667085;--line:#e4e7ec;--bg:#f4f5f7;--brand:#ff5900;
  --crit:#b42318;--crit-s:#fef3f2;--warn:#8f5f00;--warn-s:#fef9e7;--ok:#067647;--ok-s:#ecfdf3}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 "Pretendard Variable",Pretendard,"Segoe UI","Malgun Gothic","맑은 고딕","Apple SD Gothic Neo","Noto Sans KR",sans-serif;word-break:keep-all}
main{max-width:980px;margin:0 auto;padding:28px 22px 60px;background:#fff}
header{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;border-bottom:3px solid var(--brand);padding-bottom:12px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:26px 0 8px}.m{color:var(--mute);font-size:12.5px}
.top{display:flex;gap:16px;align-items:center;margin-top:18px;padding:14px 16px;border:1px solid var(--line);border-radius:12px}
.sc{min-width:86px;text-align:center}.sc b{display:block;font-size:34px;line-height:1.1}.sc span{font-weight:700;font-size:13px}
.ok .sc b,.ok .sc span{color:var(--ok)}.warn .sc b,.warn .sc span{color:var(--warn)}.crit .sc b,.crit .sc span{color:var(--crit)}.none .sc b{color:var(--mute)}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:14px}
.k{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.k b{display:block;font-size:20px}.k span{font-size:12px;color:var(--sub)}
ol.todo{margin:6px 0 0;padding-left:20px}ol.todo li{margin:4px 0}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--mute);font-weight:600}
td.n,th.n{text-align:right}tr.bad td{background:var(--crit-s)}tr.tight td{background:var(--warn-s)}
.chip{display:inline-block;padding:0 7px;border-radius:5px;font-size:11.5px;font-weight:700;background:var(--bg);color:var(--sub)}
.chip.crit{background:var(--crit-s);color:var(--crit)}.chip.warn{background:var(--warn-s);color:var(--warn)}.chip.ok{background:var(--ok-s);color:var(--ok)}
footer{margin-top:28px;color:var(--mute);font-size:12px;border-top:1px solid var(--line);padding-top:10px}
.pbtn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;font:inherit;font-weight:600;cursor:pointer}
@media print{body{background:#fff}main{padding:0}.pbtn{display:none}tr{break-inside:avoid}}
@media (max-width:700px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}header,.top{flex-direction:column;align-items:flex-start}.sc{text-align:left}}
"""
_STATE = {"ok": ("좋음", "ok"), "warn": ("주의", "warn"), "risk": ("점검", "crit"), "none": ("기록 쌓는 중", "none")}
_INC = {"open": "새 사건", "ack": "확인 중", "resolved": "해결", "false_alarm": "오경보"}


class DailyMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    def daily_html(self, day: str = "", replay: bool = False) -> str:
        today = dt.datetime.now().strftime("%Y%m%d")
        day = day if (len(day) == 8 and day.isdigit()) else today
        sfx = "_replay" if replay else ""
        end = f"{day[:4]}-{day[4:6]}-{day[6:]} 24:00"
        # 설비 건강 — 그날 끝까지의 기록(지난 30일)으로
        rows = [r for r in _rows(self._kpi_files(replay, 400)) if r["hour"] < end]
        H = self._health_calc(rows, "replay" if replay else "live", current=(day == today and not replay))
        drows = [r for r in rows if r["hour"][:10].replace("-", "") == day]
        q = [r for r in drows if r.get("group") == "quality"]
        n_q, ng = sum(r.get("n", 0) for r in q), sum(r.get("ng", 0) for r in q)
        # 시간별 — 검사·거부·가장 낮은 버틸 여유는 timeline, NG 는 판정 기록, 경보는 그날 경보 파일
        tl = next((t for t in self.timeline(400).get("days", []) if t["day"] == day and t["replay"] == replay), None)
        sev = {"crit": 0, "warn": 0, "info": 0}
        for p in glob.glob(os.path.join(self.data_dir, f"alerts_{day}{sfx}.jsonl")):
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    try:
                        a = json.loads(ln)
                    except ValueError:
                        continue
                    if a.get("severity") in sev:
                        sev[a["severity"]] += 1
        incs = [r for r in self.incidents(days=400, replay=replay, limit=5000) if r["day"] == day and r["replay"] == replay]
        done = sum(1 for r in incs if r.get("state") in ("resolved", "false_alarm"))
        n_insp = sum(x for x in (tl or {}).get("n", []) if x) if tl else n_q
        tol = [x for x in (tl or {}).get("tol_pct", []) if x is not None]
        word, cls = _STATE[H["state"]]
        title = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        top = H.get("top") or {}
        k = [(f"{n_insp:,}" if n_insp else "—", "검사"), (f"{100 * ng / n_q:.1f}%" if n_q else "—", "NG 비율"),
             (f"{sev['crit']} · {sev['warn']}", "경보 (심각 · 주의)"), (f"{len(incs)}건 · 마무리 {done}", "사건"),
             (f"{min(tol):.0f}%" if tol else "—", "가장 낮은 버틸 여유")]
        hours = []
        qh = {r["hour"][11:13]: r for r in q}
        T = tl or {"n": [None] * 24, "tol_pct": [None] * 24, "rejects": [None] * 24, "alerts": [[0, 0, 0]] * 24}
        for h in range(24):
            n, t, rj, al, qq = T["n"][h], T["tol_pct"][h], T["rejects"][h], T["alerts"][h], qh.get(f"{h:02d}")
            if not n and not qq and not any(al):
                continue
            row_cls = "bad" if (t is not None and t < 20) or al[0] else "tight" if (t is not None and t < 40) or al[1] else ""
            ngp = f"{100 * qq['ng'] / qq['n']:.1f}%" if qq and qq.get("n") else "—"
            hours.append(f"<tr class='{row_cls}'><td>{h:02d}시</td><td class='n'>{(n or (qq or {}).get('n') or 0):,}</td>"
                         f"<td class='n'>{ngp}</td><td class='n'>{'—' if t is None else f'{t:.0f}%'}</td>"
                         f"<td class='n'>{rj or 0}</td><td class='n'>{al[0]} · {al[1]}</td></tr>")
        inc_rows = "".join(
            f"<tr><td>{_E(r.get('time') or '')}</td><td><span class='chip {'crit' if r.get('severity') == 'crit' else 'warn' if r.get('severity') == 'warn' else ''}'>"
            f"{_E({'crit': '심각', 'warn': '주의', 'info': '정보'}.get(r.get('severity'), '-'))}</span></td><td>{_E(r.get('title') or '')}</td>"
            f"<td>{_E(r.get('cause') or '-')}</td><td>{_E(_INC.get(r.get('state') or 'open', ''))}</td></tr>"
            for r in sorted(incs, key=lambda r: r.get("time") or ""))
        items = [i for i in H.get("items", []) if i["state"] in ("warn", "risk")]
        item_rows = "".join(f"<tr><td><span class='chip {_STATE[i['state']][1]}'>{_STATE[i['state']][0]}</span></td><td>{_E(i['title'])}</td>"
                            f"<td>{_E(i['reason'])}</td></tr>" for i in items[:12])
        todo = "".join(f"<li><b>{_E(t['text'])}</b> <span class='m'>— {_E(t['title'])}</span></li>" for t in H.get("todo", []))
        basis = "지난 기록(경보 재현)" if replay else ("오늘 지금까지" if day == today else "그날 기록")
        return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>하루 정리 {title} · {_E(self.settings.get('site') or 'talog')}</title><style>{_CSS}</style></head>
<body><main>
<header><div><div class="m">talog 하루 정리 · {_E(basis)}</div><h1>{_E(self.settings.get('site') or '설비')} — {title}</h1></div>
<div class="m">{dt.datetime.now():%Y-%m-%d %H:%M} 만듦 <button class="pbtn" onclick="print()">인쇄 · PDF</button></div></header>
<div class="top {cls}"><div class="sc"><b>{H['score'] if H['score'] is not None else '—'}</b><span>설비 건강 · {_E(word)}</span></div>
<div>{_E(top.get('reason') or '주의·점검 항목이 없습니다.')}{f" · 같은 흐름 {H['more']}곳 더" if H.get('more') else ''}
{f'<ol class="todo">{todo}</ol>' if todo else ''}</div></div>
<div class="kpis">{''.join(f'<div class="k"><b>{_E(v)}</b><span>{_E(lab)}</span></div>' for v, lab in k)}</div>
<h2>주의 · 점검 항목</h2>
{f'<table><thead><tr><th>상태</th><th>항목</th><th>이유</th></tr></thead><tbody>{item_rows}</tbody></table>' if item_rows else '<p class="m">없습니다.</p>'}
<h2>사건 {len(incs)}건</h2>
{f'<table><thead><tr><th>시각</th><th>등급</th><th>제목</th><th>원인</th><th>마무리</th></tr></thead><tbody>{inc_rows}</tbody></table>' if inc_rows else '<p class="m">없습니다.</p>'}
<h2>시간별</h2>
{f'<table><thead><tr><th>시</th><th class="n">검사</th><th class="n">NG 비율</th><th class="n">가장 낮은 버틸 여유</th><th class="n">거부</th><th class="n">경보 (심각 · 주의)</th></tr></thead><tbody>{"".join(hours)}</tbody></table>' if hours else '<p class="m">기록이 없습니다.</p>'}
<footer>talog {__version__} · 이 PC 에 쌓인 기록(시간별 KPI · 경보 · 사건 · 판정)으로 만든 글입니다 — AI 가 지어낸 숫자는 없습니다.
설비 건강은 그날 끝까지의 기록으로 계산했고, 언제 한계에 닿을지는 예측하지 않습니다.</footer>
</main></body></html>"""
