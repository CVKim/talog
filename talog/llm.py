"""로컬 LLM(Ollama) 클라이언트 — watch 사건 분석·주기 점검 공용.

외부 전송 없음: 기본 주소는 127.0.0.1:11434 이며 `llm.url` 로 GPU 를 고정한
전용 서버나 사내 분석 PC 를 지정할 수 있다.

장치 선택(`llm.device`):
  cpu  — num_gpu=0 (+ num_thread 상한). 검사 GPU 를 전혀 쓰지 않는다 (기본)
  gpu  — Ollama 자동 오프로드. 특정 GPU 는 서버 쪽 CUDA_VISIBLE_DEVICES 로 고정
  auto — 요청마다 nvidia-smi 로 여유 VRAM·사용률을 보고 gpu/cpu 를 고른다
         (검사가 GPU 를 많이 쓰는 순간에는 CPU 로 비켜 간다)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"

# 한자(CJK 통합 한자 + 확장 A) — 한국어 알림에 섞이면 안 된다 (Qwen 계열 중국어 누출)
_HANZI_RE = re.compile("[㐀-䶿一-鿿]")


def hanzi_count(text: str) -> int:
    return len(_HANZI_RE.findall(text or ""))


def hangul_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum("가" <= c <= "힣" for c in letters) / len(letters)


def parse_json(text: str):
    """응답에서 JSON 객체 하나를 꺼낸다 (앞뒤 설명문·코드펜스 허용)."""
    text = (text or "").strip()
    try:
        j = json.loads(text)
        return j if isinstance(j, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    s, e = text.find("{"), text.rfind("}")
    if 0 <= s < e:
        try:
            j = json.loads(text[s:e + 1])
            return j if isinstance(j, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
def _parse_gpu_free(text: str) -> list[dict]:
    """nvidia-smi CSV: index, memory.free(MB), memory.total(MB), utilization(%)."""
    out = []
    for line in (text or "").strip().splitlines():
        toks = [t.strip() for t in line.split(",")]
        if len(toks) < 4:
            continue
        try:
            out.append({"gpu": int(toks[0]), "free": float(toks[1]),
                        "total": float(toks[2]), "util": float(toks[3])})
        except ValueError:
            continue
    return out


def query_gpu_free() -> list[dict]:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.free,memory.total,"
             "utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return []
        return _parse_gpu_free(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []


def resolve_device(cfg: dict, gpus: list[dict] | None = None,
                   resident: str = "") -> tuple[str, str]:
    """llm 설정으로 이번 요청의 장치(cpu|gpu)와 그 이유를 정한다.

    resident: 모델이 이미 서버에 올라가 있는 장치("gpu"/"cpu"/""). GPU 에 상주 중이면
    그 VRAM 은 우리 몫이므로 여유 VRAM 판정 없이 GPU 를 쓴다(자기 모델 때문에 CPU 로
    재로드하는 왕복 방지)."""
    dev = str(cfg.get("device", "cpu") or "cpu").lower()
    if dev in ("cpu", "gpu"):
        return dev, "설정 고정"
    if dev != "auto":
        return "cpu", f"알 수 없는 device '{dev}' — CPU 로 처리"
    if resident == "gpu":
        return "gpu", "모델이 이미 GPU 에 상주"
    gpus = query_gpu_free() if gpus is None else gpus
    idx = int(cfg.get("gpu_index", -1) if cfg.get("gpu_index") is not None else -1)
    cands = [g for g in gpus if idx < 0 or g["gpu"] == idx]
    if not cands:
        return "cpu", "GPU 미검출(nvidia-smi 없음)"
    best = max(cands, key=lambda g: g["free"])
    need = float(cfg.get("gpu_min_free_mb", 6000))
    umax = float(cfg.get("gpu_max_util", 40))
    if best["free"] >= need and best["util"] <= umax:
        return "gpu", (f"GPU{best['gpu']} 여유 {best['free']:.0f}MB·"
                       f"사용률 {best['util']:.0f}%")
    return "cpu", (f"GPU 여유 부족 — GPU{best['gpu']} 여유 {best['free']:.0f}MB"
                   f"(기준 {need:.0f})·사용률 {best['util']:.0f}%(기준 {umax:.0f})")


# ---------------------------------------------------------------------------
class OllamaClient:
    """Ollama /api/chat 호출기. 실패는 예외 대신 error 필드로 돌려준다."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.url = str(self.cfg.get("url") or DEFAULT_URL).rstrip("/")
        self.model = self.cfg.get("model") or "qwen2.5:7b"
        self.timeout = float(self.cfg.get("timeout_s", 300) or 300)

    def _get(self, path: str, timeout: float = 3.0) -> dict:
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def alive(self) -> bool:
        try:
            self._get("/api/tags")
            return True
        except (OSError, ValueError):
            return False

    def models(self) -> list[str]:
        try:
            return [m.get("name", "") for m in self._get("/api/tags").get("models", [])]
        except (OSError, ValueError):
            return []

    def resident(self) -> str:
        """이 모델이 지금 서버에 올라가 있는 장치 ("gpu" / "cpu" / "" = 안 올라감)."""
        try:
            for m in self._get("/api/ps", timeout=2.0).get("models", []):
                if m.get("name") == self.model or m.get("model") == self.model:
                    return "gpu" if (m.get("size_vram") or 0) > 0 else "cpu"
        except (OSError, ValueError):
            pass
        return ""

    def options(self, device: str) -> dict:
        # 경보 판단은 같은 근거에 같은 답이어야 한다 — 기본 temperature 0 + 고정 seed
        # (0.1 에서는 같은 사건의 원인이 회차마다 바뀌었다, 9/29 Tenneco 0730 11:21)
        opt = {"temperature": float(self.cfg.get("temperature", 0.0)),
               "seed": int(self.cfg.get("seed", 0))}
        if self.cfg.get("num_ctx"):
            opt["num_ctx"] = int(self.cfg["num_ctx"])
        if device == "cpu":
            opt["num_gpu"] = 0                          # 검사 GPU 를 쓰지 않음
            n = int(self.cfg.get("cpu_threads", 0) or 0)
            if n > 0:
                opt["num_thread"] = n                   # 검사 SW 의 CPU 몫 보호
        return opt

    def chat(self, messages: list, fmt=None, num_predict: int | None = None,
             device: str | None = None) -> dict:
        if device:
            dev, why = device, "호출 지정"
        else:
            auto = str(self.cfg.get("device", "cpu")).lower() == "auto"
            dev, why = resolve_device(self.cfg, resident=self.resident() if auto else "")
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": self.options(dev)}
        if num_predict:
            body["options"]["num_predict"] = int(num_predict)
        if fmt is not None:
            body["format"] = fmt
        ka = self.cfg.get("keep_alive")
        if ka not in (None, ""):
            body["keep_alive"] = ka
        if self.cfg.get("think") is not None:
            body["think"] = bool(self.cfg["think"])
        out = {"content": "", "device": dev, "device_reason": why, "wall_s": 0.0,
               "prompt_tokens": 0, "gen_tokens": 0, "load_s": 0.0, "error": ""}
        t0 = time.time()
        try:
            req = urllib.request.Request(
                self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                r = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except OSError:
                detail = ""
            out["error"] = f"HTTP {e.code} {detail}".strip()
        except (OSError, ValueError) as e:          # 연결 거부·타임아웃·JSON 이상
            out["error"] = f"{type(e).__name__}: {e}"
        else:
            msg = r.get("message") or {}
            out.update(content=msg.get("content", "") or "",
                       prompt_tokens=int(r.get("prompt_eval_count", 0) or 0),
                       gen_tokens=int(r.get("eval_count", 0) or 0),
                       load_s=round((r.get("load_duration", 0) or 0) / 1e9, 2))
        out["wall_s"] = round(time.time() - t0, 2)
        return out
