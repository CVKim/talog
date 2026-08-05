"""통합 인덱스(index.html) 생성 — 여러 설비-일자 리포트를 한 페이지로.

make_index.py(루트 스크립트)의 본체이며, fleet 모드에서도 사용된다.
"""

from __future__ import annotations

import glob
import html
import os
import sqlite3

from .guide import AI_GUIDE


def collect(out_dir: str) -> list[dict]:
    rows = []
    for db in sorted(glob.glob(os.path.join(out_dir, "*.sqlite"))):
        tag = os.path.splitext(os.path.basename(db))[0]
        if not os.path.exists(os.path.join(out_dir, tag + ".html")):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            st = dict(con.execute(
                "SELECT status, COUNT(*) FROM inspections GROUP BY status"))
            gens = con.execute("SELECT COUNT(*) FROM process_gens").fetchone()[0]
            crash = con.execute(
                "SELECT COUNT(*) FROM events WHERE kind='CRASH'").fetchone()[0]
            avg_dur = con.execute(
                "SELECT AVG(duration_s) FROM inspections "
                "WHERE status='complete' AND duration_s>0").fetchone()[0] or 0
            errs = con.execute(
                "SELECT COUNT(*) FROM events WHERE kind IN "
                "('ERROR','MODEL_FAIL','COMM_FAIL','RECIPE_FAIL','EXC_REDIRECT')"
            ).fetchone()[0]
            try:
                ng = con.execute("SELECT COUNT(*) FROM inspections "
                                 "WHERE end_result='NG'").fetchone()[0]
            except sqlite3.Error:
                ng = 0
        except sqlite3.Error:
            continue
        finally:
            con.close()
        bad = sum(st.get(k, 0) for k in ("rejected", "incomplete_lost",
                                         "incomplete", "unknown"))
        rows.append({
            "tag": tag, "total": sum(st.values()),
            "complete": st.get("complete", 0), "bad": bad,
            "lost": st.get("incomplete_lost", 0),
            "rejected": st.get("rejected", 0),
            "sim": st.get("sim_complete", 0) + st.get("sim_partial", 0),
            "gens": gens, "crash": crash, "errs": errs,
            "avg_dur": avg_dur, "ng": ng,
        })
    return rows


def _trend_section(rows: list[dict]) -> str:
    """설비별 일자 추이 비교 (2일 이상 데이터가 있는 설비만)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        parts = r["tag"].rsplit("_", 1)
        equip = parts[0] if len(parts) == 2 and parts[1][:1].isdigit() else "(단일)"
        groups.setdefault(equip, []).append(r)
    multi = {k: v for k, v in groups.items() if len(v) >= 2 and k != "(단일)"}
    if not multi:
        return ""
    out = ["<h2 style='font-size:16px;border-bottom:2px solid #2a78d6;"
           "padding-bottom:6px'>설비별 추이</h2>"]
    for equip, days in sorted(multi.items()):
        days.sort(key=lambda r: r["tag"])
        mx = max(d["total"] for d in days) or 1
        cells = []
        for d in days:
            day = d["tag"].rsplit("_", 1)[-1]
            h = max(6, int(d["total"] / mx * 46))
            bar_color = "#d03b3b" if d["bad"] else "#2a78d6"
            bad_txt = (f"<div style='color:#d03b3b;font-weight:700'>"
                       f"이상 {d['bad']}</div>") if d["bad"] else \
                "<div style='color:#0ca30c'>정상</div>"
            ng_txt = (f"<div style='color:#ec835a'>NG {d['ng']}</div>"
                      if d.get("ng") else "")
            cells.append(
                f"<td style='border:none;text-align:center;padding:6px 14px'>"
                f"<div style='height:50px;display:flex;align-items:flex-end;"
                f"justify-content:center'><div style='width:26px;height:{h}px;"
                f"background:{bar_color};border-radius:3px 3px 0 0'></div></div>"
                f"<div style='font-weight:700'>{html.escape(day)}일</div>"
                f"<div>{d['total']:,}건</div>{bad_txt}{ng_txt}"
                f"<div style='color:#898781'>{d['avg_dur']:.1f}s · 재시작 "
                f"{max(0, d['gens'] - 1)}</div></td>")
        out.append(
            f"<div style='display:inline-block;border:1px solid #e1e0d9;"
            f"border-radius:10px;margin:8px 12px 8px 0;padding:8px 6px;"
            f"vertical-align:top'><div style='font-weight:800;padding:2px 12px'>"
            f"{html.escape(equip)}</div><table style='border:none'><tr>"
            + "".join(cells) + "</tr></table></div>")
    return "".join(out)


def render(rows: list[dict]) -> str:
    body = []
    for r in rows:
        badge = (f"<span style='color:#d03b3b;font-weight:700'>{r['bad']}</span>"
                 if r["bad"] else "0")
        body.append(
            f"<tr><td><a href='{html.escape(r['tag'])}.html'><b>"
            f"{html.escape(r['tag'])}</b></a></td>"
            f"<td class='r'>{r['total']:,}</td><td class='r'>{r['complete']:,}</td>"
            f"<td class='r'>{badge}</td><td class='r'>{r['ng']}</td>"
            f"<td class='r'>{r['lost']}</td>"
            f"<td class='r'>{r['rejected']}</td><td class='r'>{r['sim']}</td>"
            f"<td class='r'>{r['avg_dur']:.1f}s</td>"
            f"<td class='r'>{max(0, r['gens'] - 1)}</td>"
            f"<td class='r'>{r['crash']}</td><td class='r'>{r['errs']}</td></tr>")
    total = sum(r["total"] for r in rows)
    bad = sum(r["bad"] for r in rows)
    ng = sum(r["ng"] for r in rows)
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>talog 리포트 인덱스</title>
<style>
 body {{ font-family: 'Malgun Gothic', sans-serif; margin: 30px; color: #0b0b0b; }}
 h1 {{ font-size: 21px; }}
 table {{ border-collapse: collapse; font-size: 13px; margin-top: 14px; }}
 th, td {{ border: 1px solid #e1e0d9; padding: 6px 10px; text-align: left; }}
 th {{ background: #fcfcfb; }} td.r {{ text-align: right; }}
 .sum {{ color: #52514e; margin-top: 6px; }}
 a {{ color: #2a78d6; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>talog 진단 리포트 인덱스</h1>
<div class="sum">총 {len(rows)}개 설비-일자 · 검사 {total:,}건 · 이상 {bad}건 ·
NG {ng}건 — 행을 클릭하면 해당 리포트가 열립니다.</div>
{_trend_section(rows)}
<table><thead><tr><th>리포트</th><th>검사</th><th>완료</th>
<th>이상</th><th>NG</th><th>소실</th><th>거부</th><th>시뮬</th><th>평균시간</th>
<th>재시작</th><th>크래시</th><th>에러</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
<div class="sum" style="margin-top:18px">재생성:
<code>talog fleet &lt;루트&gt;</code> 또는 <code>python make_index.py &lt;폴더&gt;</code></div>
</body></html>"""


def build(out_dir: str) -> str:
    rows = collect(out_dir)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(rows))
    with open(os.path.join(out_dir, "AI_GUIDE.md"), "w", encoding="utf-8") as f:
        f.write(AI_GUIDE)
    return path
