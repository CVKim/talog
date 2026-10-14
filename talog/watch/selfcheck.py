"""현장 설치 자가 점검(경로·파일·알림·LLM·메일)과 예시 사건 메일 발송."""

from __future__ import annotations

import os

from ..tracker import resolve_targets
from .alerts import Notifier
from .config import _today_dir
from .gpu import _query_gpu
from .rules import _defect_rules


def run_check(cfg: dict) -> int:
    """현장 설치 자가 점검: 경로/파일/알림 채널을 확인하고 테스트 토스트를 쏜다."""
    print("=" * 56)
    print(" talog 설치 자가 점검")
    print("=" * 56)
    ok = True

    root = cfg["watch_root"]
    print(f"[1] 감시 루트: {root}", "→ 존재" if os.path.isdir(root) else "→ 없음!")
    ok &= os.path.isdir(root)

    day = _today_dir(root)
    if os.path.isdir(day):
        print(f"[2] 오늘 날짜 폴더: {day} → 존재")
        tc = cfg.get("tracking") or {}
        mode = str(tc.get("mode", "default")).lower()
        found = []
        for p in resolve_targets(day, tc):
            sz = os.path.getsize(p)
            found.append(f"{os.path.relpath(p, day)} ({sz / 1024:.0f}KB)")
        want = ("개 (핵심 로그 + 그룹별 seq)" if mode == "default" else "개")
        print(f"[3] 추적 모드 {mode} — 대상 파일 {len(found)}{want} 발견:")
        for f in found[:30]:
            print(f"     - {f}")
        if len(found) > 30:
            print(f"     … 외 {len(found) - 30}개")
        if not found:
            print("     ! 감시 대상 파일이 없습니다 — talos 가동 여부나 tracking.files 를 "
                  "확인하십시오")
    else:
        print(f"[2] 오늘 날짜 폴더 없음: {day}")
        print("     ! talos 가 오늘 아직 기동되지 않았거나 경로 설정이 다릅니다")

    try:
        os.makedirs(cfg["alert_dir"], exist_ok=True)
        probe = os.path.join(cfg["alert_dir"], "_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        print(f"[4] 알림 폴더 쓰기: {cfg['alert_dir']} → OK")
    except OSError as e:
        print(f"[4] 알림 폴더 쓰기 실패: {e}")
        ok = False

    r = cfg["rules"]
    print(f"[5] 감지 룰: 에러반복 {r['error_repeat']['count']}회/"
          f"{r['error_repeat']['window_min']}분 · NoInspThread 즉시 · "
          f"정체 {r['insp_stall']['factor_x_median']}×중앙값 · "
          f"재시작 {r['restart_burst']['count']}회/{r['restart_burst']['window_min']}분 · "
          f"메모리 +{r['memory_trend']['mb_per_hour']}MB/h")
    print(f"[6] 사이트: '{cfg.get('site') or '(미설정 — 콘솔 설정에서 지정 권장)'}'"
          f" / 웹훅: {'설정됨' if cfg['notify'].get('webhook') else '없음(토스트/JSONL만)'}")

    llm_cfg, a_cfg, e_cfg = cfg["llm"], cfg["agent"], cfg["email"]
    need_llm = a_cfg.get("enabled") and a_cfg.get("use_llm", True)
    if need_llm:
        from ..llm import OllamaClient, resolve_device
        cli = OllamaClient(llm_cfg)
        alive = cli.alive()
        has = cli.model in cli.models() if alive else False
        dev, why = resolve_device(llm_cfg)
        print(f"[7] LLM: {cli.url} {'가동 중' if alive else '미가동!'} · 모델 {cli.model} "
              f"{'설치됨' if has else '없음!(ollama pull 필요)'} · 장치 설정 "
              f"{llm_cfg.get('device')} → 지금은 {dev} ({why})")
        ok &= alive and has
    else:
        print("[7] LLM: 비활성 (룰 진단만)")
    dw = cfg["rules"].get("defect_watch", {})
    drs = _defect_rules(dw)
    print(f"[7b] 결함 규칙 {len(drs)}개"
          + "".join(f" · {'/'.join(r['match'])} {r['severity']} {r['count']}건"
                    for r in drs[:6])
          + f" · 연속 NG {dw.get('ng_streak') or '끔'} · NG 비율 "
          f"{str(dw.get('ng_rate_percent')) + '%/' + str(dw.get('ng_rate_window')) + '검사' if dw.get('ng_rate_window') else '끔'}")
    print(f"[7c] 사건 분석 에이전트: {'활성' if a_cfg.get('enabled') else '비활성'}"
          + (f" (기준 {a_cfg.get('min_severity')}, 묶음 {a_cfg.get('batch_seconds')}초, "
             f"{'룰+LLM' if a_cfg.get('use_llm', True) else '룰만'})"
             if a_cfg.get("enabled") else ""))
    if e_cfg.get("enabled"):
        from ..mailer import Mailer
        m = Mailer(cfg, cfg["alert_dir"])
        to_all = m.recipients(list((e_cfg.get("roles") or {}).keys()))
        if m.dry_run:
            print(f"[7d] 이메일: dry_run — SMTP 없이 {m.outbox} 에 .eml 만 저장 "
                  f"(수신자 {len(to_all)}명 설정)")
        else:
            src = m.password_source() or "없음"
            good, detail = m.check()
            where = ("Microsoft 365 Graph" if m.transport == "graph" else
                     f"{e_cfg.get('smtp_host')}:{e_cfg.get('smtp_port')} "
                     f"{e_cfg.get('security')}")
            print(f"[7d] 이메일: {where} · 비밀 {src} · 수신자 "
                  f"{len(to_all)}명 · 접속 {'OK' if good else '실패'} ({detail})")
            ok &= good and bool(to_all)
            if not to_all:
                print("     ! email.to / email.roles 에 받는 사람을 적으십시오")
        print("     실제 발송 확인: talog run --test-email (또는 콘솔 설정 → 테스트 메일)")
    else:
        print("[7d] 이메일: 비활성 (기본)")

    gpus = _query_gpu()
    if gpus:
        stat = " · ".join(f"GPU{g['gpu']} {g['temp']:.0f}°C/{g['util']:.0f}%"
                          for g in gpus)
        print(f"[8] GPU 감시: nvidia-smi OK — {stat} "
              f"(임계 {cfg['rules'].get('gpu_temp', {}).get('celsius', 85)}°C)")
    else:
        print("[8] GPU 감시: nvidia-smi 미검출 — GPU 온도 룰 비활성")

    if cfg["notify"].get("toast", True):
        Notifier._toast("[talog] 설치 점검", "테스트 알림입니다 - 이 팝업이 보이면 정상")
        print("[9] 테스트 토스트 발사 — 화면 우하단 팝업을 확인하십시오")
    print("=" * 56)
    print(" 점검 " + ("통과 — run_talog.bat(콘솔) 또는 talog run 으로 시작하십시오"
                     if ok else "실패 항목 있음 — 설정(talog.yaml)의 경로를 확인하십시오"))
    return 0 if ok else 1


def run_test_email(cfg: dict) -> int:
    """예시 사건 메일 1통을 설정된 경로로 보낸다 (dry_run 이면 outbox 에만)."""
    from ..mailer import Mailer, sample_incident
    if not cfg["email"].get("enabled"):
        print("메일이 꺼져 있습니다 — 콘솔 설정의 '알림' 에서 발송 방식을 먼저 고르십시오.")
        return 2
    notifier = Notifier(cfg, replay=True)            # alert_dir 폴백만 빌려 쓴다
    m = Mailer(cfg, notifier.alert_dir)
    inc = sample_incident(cfg.get("site") or "설비")
    inc["verdict"]["notify"] = list((cfg["email"].get("roles") or {}).keys())
    res = m.send_incident(inc)
    print(f"[talog watch] 테스트 메일: 수신 {res['to'] or '(없음)'} · 제목 {res['subject']}")
    if res["eml"]:
        print(f"  보관: {res['eml']}")
    if res["dry_run"]:
        print("  dry_run — SMTP 로 보내지 않았습니다 (email.dry_run: false 로 실제 발송)")
        return 0
    print("  → 발송 " + ("성공" if res["sent"] else f"실패: {res['error']}"))
    return 0 if res["sent"] else 1
