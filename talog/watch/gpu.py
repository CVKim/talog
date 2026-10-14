"""GPU 감시 — nvidia-smi 로 온도·사용률·메모리를 주기적으로 읽고 문턱을 넘으면 경보한다."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from collections import deque

from .alerts import Alert, Notifier


def _parse_smi(text: str) -> list[dict]:
    """nvidia-smi CSV 출력 파싱: index, temp(°C), util(%), mem_used(MB), power(W)."""
    out = []
    for line in (text or "").strip().splitlines():
        toks = [t.strip() for t in line.split(",")]
        if len(toks) < 4:
            continue
        try:
            out.append({"gpu": int(toks[0]), "temp": float(toks[1]),
                        "util": float(toks[2]), "mem": float(toks[3]),
                        "power": float(toks[4]) if len(toks) > 4 and
                        toks[4].replace(".", "").isdigit() else 0.0})
        except ValueError:
            continue
    return out


def _query_gpu() -> list[dict]:
    """드라이버 기본 동봉 nvidia-smi 로 GPU 상태를 읽는다 (별도 설치 불필요)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu,utilization.gpu,"
             "memory.used,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000)
        if r.returncode != 0:
            return []
        return _parse_smi(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []


class GpuMonitor:
    """GPU 온도/부하 주기 수집 + 임계 경보 + 이력 JSONL 적재."""

    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg = cfg["rules"].get("gpu_temp", {})
        self.notify = notifier
        self.alert_dir = cfg["alert_dir"]
        self.available = bool(_query_gpu()) if self.cfg.get("enabled", True) \
            else False
        self._last_rec = 0.0
        self.last: list[dict] = []      # 상태 페이지용 최신 샘플
        self.hist: deque = deque(maxlen=120)   # (ts, 샘플) — 사건 분석 근거용 약 40분

    def poll(self, now: float):
        if not self.available:
            return
        gpus = _query_gpu()
        if not gpus:
            return
        self.last = gpus
        self.hist.append((now, gpus))
        limit = self.cfg.get("celsius", 85)
        for g in gpus:
            if g["temp"] >= limit:
                self.notify.emit(Alert(
                    now, "gpu_temp", "crit",
                    f"GPU{g['gpu']} 온도 {g['temp']:.0f}°C (임계 {limit}°C)",
                    f"사용률 {g['util']:.0f}% · 메모리 {g['mem']:.0f}MB · "
                    f"전력 {g['power']:.0f}W — 냉각/팬 상태를 점검하십시오",
                    key=f"gpu{g['gpu']}"))
        # 이력 적재 (기본 5분 간격 — 온도 추세 분석용)
        rec_iv = self.cfg.get("record_min", 5) * 60
        if now - self._last_rec >= rec_iv:
            self._last_rec = now
            day = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
            try:
                with open(os.path.join(self.alert_dir, f"gpu_{day}.jsonl"),
                          "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": dt.datetime.fromtimestamp(now)
                        .strftime("%H:%M:%S"),
                        "gpus": gpus}, ensure_ascii=False) + "\n")
            except OSError:
                pass
