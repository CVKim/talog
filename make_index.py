"""out 폴더의 리포트들을 한 페이지에서 여는 index.html 생성기.

사용: python make_index.py [out 폴더 (기본 .\\out)]
"""

from __future__ import annotations

import glob
import html
import os
import sqlite3
import sys


def collect(out_dir: str) -> list[dict]:
    rows = []
    for db in sorted(glob.glob(os.path.join(out_dir, "*.sqlite"))):
        tag = os.path.splitext(os.path.basename(db))[0]
        if not os.path.exists(os.path.join(out_dir, tag + ".html")):
            continue
        con = sqlite3.connect(db)
        try:
            day_dir = (con.execute("SELECT value FROM meta WHERE key='day_dir'")
                       .fetchone() or [""])[0]
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
        finally:
            con.close()
        bad = sum(st.get(k, 0) for k in ("rejected", "incomplete_lost",
                                         "incomplete", "unknown"))
        rows.append({
            "tag": tag, "day_dir": day_dir,
            "total": sum(st.values()), "complete": st.get("complete", 0),
            "bad": bad, "lost": st.get("incomplete_lost", 0),
            "rejected": st.get("rejected", 0),
            "sim": st.get("sim_complete", 0) + st.get("sim_partial", 0),
            "gens": gens, "crash": crash, "errs": errs,
            "avg_dur": avg_dur,
        })
    return rows


def _trend_section(rows: list[dict]) -> str:
    """설비별 일자 추이 비교 (2일 이상 데이터가 있는 설비만)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        tag = r["tag"]
        parts = tag.rsplit("_", 1)
        equip = parts[0] if len(parts) == 2 and parts[1][:1].isdigit() else "(단일)"
        groups.setdefault(equip, []).append(r)
    multi = {k: v for k, v in groups.items() if len(v) >= 2 and k != "(단일)"}
    if not multi:
        return ""
    out = ["<h2 style='font-size:16px;border-bottom:2px solid #5c9ded;"
           "padding-bottom:6px'>설비별 추이</h2>"]
    for equip, days in sorted(multi.items()):
        days.sort(key=lambda r: r["tag"])
        mx = max(d["total"] for d in days) or 1
        cells = []
        for d in days:
            day = d["tag"].rsplit("_", 1)[-1]
            h = max(6, int(d["total"] / mx * 46))
            bar_color = "#c62828" if d["bad"] else "#5c9ded"
            bad_txt = (f"<div style='color:#c62828;font-weight:700'>"
                       f"이상 {d['bad']}</div>") if d["bad"] else \
                "<div style='color:#2e7d32'>정상</div>"
            cells.append(
                f"<td style='border:none;text-align:center;padding:6px 14px'>"
                f"<div style='height:50px;display:flex;align-items:flex-end;"
                f"justify-content:center'><div style='width:26px;height:{h}px;"
                f"background:{bar_color};border-radius:3px 3px 0 0'></div></div>"
                f"<div style='font-weight:700'>{html.escape(day)}일</div>"
                f"<div>{d['total']:,}건</div>{bad_txt}"
                f"<div style='color:#888'>{d['avg_dur']:.1f}s · 재시작 "
                f"{max(0, d['gens'] - 1)}</div></td>")
        out.append(
            f"<div style='display:inline-block;border:1px solid #e2e8f0;"
            f"border-radius:10px;margin:8px 12px 8px 0;padding:8px 6px;"
            f"vertical-align:top'><div style='font-weight:800;padding:2px 12px'>"
            f"{html.escape(equip)}</div><table style='border:none'><tr>"
            + "".join(cells) + "</tr></table></div>")
    return "".join(out)


def render(rows: list[dict]) -> str:
    body = []
    for r in rows:
        badge = (f"<span style='color:#c62828;font-weight:700'>{r['bad']}</span>"
                 if r["bad"] else "0")
        body.append(
            f"<tr><td><a href='{html.escape(r['tag'])}.html'><b>"
            f"{html.escape(r['tag'])}</b></a></td>"
            f"<td class='dim'>{html.escape(r['day_dir'])}</td>"
            f"<td class='r'>{r['total']:,}</td><td class='r'>{r['complete']:,}</td>"
            f"<td class='r'>{badge}</td><td class='r'>{r['lost']}</td>"
            f"<td class='r'>{r['rejected']}</td><td class='r'>{r['sim']}</td>"
            f"<td class='r'>{r['avg_dur']:.0f}s</td>"
            f"<td class='r'>{max(0, r['gens'] - 1)}</td>"
            f"<td class='r'>{r['crash']}</td><td class='r'>{r['errs']}</td></tr>")
    total = sum(r["total"] for r in rows)
    bad = sum(r["bad"] for r in rows)
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>talog 리포트 인덱스</title>
<style>
 body {{ font-family: 'Malgun Gothic', sans-serif; margin: 30px; color: #222; }}
 h1 {{ font-size: 21px; }}
 table {{ border-collapse: collapse; font-size: 13px; margin-top: 14px; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
 th {{ background: #f0f4fa; }} td.r {{ text-align: right; }}
 td.dim {{ color: #888; font-size: 11px; max-width: 380px; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; }}
 .sum {{ color: #555; margin-top: 6px; }}
 a {{ color: #1565c0; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>talog 진단 리포트 인덱스</h1>
<div class="sum">총 {len(rows)}개 설비-일자 · 검사 {total:,}건 · 이상 {bad}건 —
행을 클릭하면 해당 리포트가 열립니다.</div>
{_trend_section(rows)}
<table><thead><tr><th>리포트</th><th>대상 로그</th><th>검사</th><th>완료</th>
<th>이상</th><th>소실</th><th>거부</th><th>시뮬</th><th>평균시간</th>
<th>재시작</th><th>크래시</th><th>에러</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
<div class="sum" style="margin-top:18px">생성: talog — 재생성은
<code>run_talog.bat</code> 또는 <code>python -m talog &lt;로그폴더&gt;</code></div>
</body></html>"""


_AI_GUIDE = """# talog AI 분석 가이드 (LLM 에게 이 파일을 먼저 읽힌다)

이 폴더에는 talos 설비 로그를 구조화한 SQLite DB(설비-일자당 1개)와 HTML 리포트가 있다.
질문에 답할 때 원시 로그가 아니라 **이 DB를 조회**하라. 시간은 epoch 초(ts)와
"HH:MM:SS.fff"(ts_text) 병행이다.

## 테이블
- inspections: 검사 1건 = 1행. inner_id(바코드), product_id, start_ts/start_text,
  ack_status(OK|NoInspThread), end_ts(0=미완료), end_result, status
  (complete|rejected|incomplete_lost|incomplete|sim_complete|sim_partial|in_progress_eof),
  duration_s(검사시간), n_fed/n_done/n_lost/n_nofeed/n_skipped,
  lost_channels(실행 중 소실), nofeed_channels(이상 미투입), remain_list(플랫폼 REMAIN 덤프)
- channel_runs: 채널별 인퍼런스 실행. inner_id, alg_idx, channel, exec_no,
  infer_start_ts/infer_end_ts, infer_ms(InspectMC), pre_ms, post_ms, model,
  status(done|lost)
- events: 저수준 이벤트. kind ∈ RESET, FEED, TACT_PRE/INFER/POST, BLOCK_START,
  INSP_START(value=가용 Seq 스레드), INSP_REJECT, COMM_MSG(name=V2M/M2V 프로토콜,
  status=OK/NoInspThread/NG), REMAIN, POOL_CREATE/DESTROY(프로세스 세대),
  CRASH(unhandled exception), BATCH(name=talos_kill/start.bat), MODEL_FAIL,
  MODEL_GPU_LOAD(status=GPU번호, model, value=로드ms),
  DLINFER_EXEC(model, status=GPU, value=executeV2 ms),
  USAGE(value=RAM MB, status=CPU%, name=스레드수), ALG_DEACT(정상 스킵), ERROR,
  EXC_REDIRECT(name=원본 로그파일)
- process_gens: 프로세스 세대(재시작 이력). end_cause(kill|crash|destroy|eof)
- model_loads: kind=recipe_load(설비 명령, dur_s, status) | channel_init(채널 로드)
- recipe_algs / recipe_models: 레시피 조인 (alg_idx↔결함명↔모델↔GPU dev_index)

## 진단 패턴 (검증된 순서)
1. 미완료 검사: inspections WHERE status NOT IN ('complete','sim_complete',
   'in_progress_eof') → lost_channels / remain_list / ack_status 확인
2. 원인 시각 주변 재시작: process_gens 와 대조 (kill 직후 소실이 전형)
3. 처리량 병목: channel_runs 에서 infer_ms 상위 채널 → 같은 시간대 동시 실행
   개수와 상관 (GPU 경합 시 단독 대비 3~5배 열화가 정상 패턴)
4. GPU 경합: events DLINFER_EXEC 를 모델별 시계열로 — 특정 시간대 상승 = 경합
5. 메모리 릭: events USAGE 의 value(RAM) 5분 버킷 상한 추세
6. 원문 확인이 필요하면 events.file_id → files.path 로 원본 로그와 라인(line_no)을
   특정할 수 있다.

## 주의
- 로그의 [Id] 필드는 스레드가 아니라 인스턴스 주소다. 같은 값 = 같은 파이프라인.
- sim_* 상태는 설비 신호 없는 시뮬레이션 실행이므로 양산 통계에서 제외하라.
- exception.log 항목은 `, @원본파일.log` 역참조로 어느 모듈 문제인지 가리킨다.
"""


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "out")
    rows = collect(out_dir)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(rows))
    with open(os.path.join(out_dir, "AI_GUIDE.md"), "w", encoding="utf-8") as f:
        f.write(_AI_GUIDE)
    print(f"index: {path} ({len(rows)}개 리포트) + AI_GUIDE.md")


if __name__ == "__main__":
    main()
