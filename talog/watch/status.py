"""현장 모니터용 상태 페이지(status.html) — 최근 경보·GPU·사건 분석 표, 30초 자동 새로고침."""

from __future__ import annotations

import datetime as dt
import os
import time

from .alerts import Notifier
from .gpu import GpuMonitor


def _agent_status_html(agent) -> str:
    """상태 페이지의 '최근 사건 분석' 표 (에이전트가 없으면 빈 문자열)."""
    if agent is None:
        return ""
    import html as _html
    esc = _html.escape
    rows = "".join(
        f"<tr><td>{esc(r['time'] or '')}</td><td>{esc(r['severity'])}</td>"
        f"<td>{esc(r['title'])}</td><td>{esc(r['cause'])}</td><td>{esc(r['mode'])}</td>"
        f"<td>{esc(r['email'])}</td></tr>"
        for r in reversed(agent.status_rows()[-8:])) or \
        "<tr><td colspan='6' style='color:#2e7d32'>분석한 사건 없음</td></tr>"
    return ("<h3 style=\"font-size:15px\">최근 사건 분석 (최대 8건)</h3><table><thead><tr>"
            "<th>시각</th><th>심각도</th><th>사건</th><th>원인(룰)</th><th>판단</th>"
            f"<th>메일</th></tr></thead><tbody>{rows}</tbody></table>")


def _write_status(cfg: dict, notifier: Notifier, gpu: "GpuMonitor",
                  started: float, agent=None):
    """현장 모니터용 상태 페이지(status.html)를 갱신한다 (30초 자동 새로고침)."""
    now = dt.datetime.now()
    site = cfg.get("site") or "(사이트 미지정)"
    sev_color = {"crit": "#c62828", "warn": "#e65100", "info": "#546e7a"}
    rows = "".join(
        f"<tr><td>{dt.datetime.fromtimestamp(a.ts).strftime('%m/%d %H:%M:%S')}</td>"
        f"<td style='color:{sev_color.get(a.severity, '#333')};font-weight:700'>"
        f"{a.severity}</td><td>{a.title}</td><td>{a.evidence[:90]}</td></tr>"
        for a in reversed(notifier.sent[-15:])) or \
        "<tr><td colspan='4' style='color:#2e7d32'>경보 없음 — 정상 감시 중</td></tr>"
    gpus = " · ".join(f"GPU{g['gpu']} {g['temp']:.0f}°C/{g['util']:.0f}%"
                      for g in gpu.last) or "미수집"
    up_h = (time.time() - started) / 3600
    html_doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><title>talog watch — {site}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;margin:24px;background:#f8fafc}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;font-size:13px;
background:#fff}} th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
th{{background:#f0f4fa}} .meta{{color:#555;font-size:13px;margin:6px 0}}</style>
</head><body>
<h1>talog watch — {site} <span style="color:#2e7d32;font-size:14px">● 가동 중</span></h1>
<div class="meta">가동 {up_h:.1f}시간 · 마지막 갱신 {now.strftime('%H:%M:%S')}
 (30초 자동 새로고침) · GPU: {gpus}</div>
<h3 style="font-size:15px">최근 경보 (최대 15건)</h3>
<table><thead><tr><th>시각</th><th>심각도</th><th>제목</th><th>내용</th></tr></thead>
<tbody>{rows}</tbody></table>
{_agent_status_html(agent)}
<div class="meta">기록: {notifier.alert_dir}\\alerts_*.jsonl · 이 페이지는
talog watch 가 자동 갱신합니다</div></body></html>"""
    try:
        with open(os.path.join(notifier.alert_dir, "status.html"), "w",
                  encoding="utf-8") as f:
            f.write(html_doc)
    except OSError:
        pass
