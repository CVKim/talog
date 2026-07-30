"""talog CLI.

사용:
    python -m talog <설비-일자 폴더 | 설비 폴더> [--recipe <레시피 폴더>] [--out <출력 폴더>]

설비-일자 폴더(플랫폼 로그 + alg 하위)를 직접 주거나, 날짜 하위 폴더들을 가진
설비 폴더를 주면 일자별로 각각 리포트를 생성한다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .assemble import (build_inspections, build_model_loads,
                       build_process_gens, build_runs_for_file)
from .diagnose import diagnose
from .events import Event, Extractor
from .fileclass import scan_day_folder
from .recipe import Recipe, load_recipe
from .report import ReportContext, render, _usage_analysis
from . import store

_STITCH_CATEGORIES = ("alg", "comm")   # 익일 첫 구간을 이어붙일 파일 분류
_STITCH_HOURS = 1.0                    # 익일 몇 시까지 이어붙일지


def _is_day_folder(path: str) -> bool:
    try:
        return os.path.isdir(os.path.join(path, "alg")) or any(
            f.lower() == "inspstarter.log" for f in os.listdir(path)
            if os.path.isfile(os.path.join(path, f)))
    except OSError:
        # 접근 불가 폴더(권한/단절)는 일자 폴더가 아닌 것으로 취급
        return False


def _collect_day_folders(root: str) -> list[str]:
    if _is_day_folder(root):
        return [root]
    days = []
    try:
        entries = sorted(os.listdir(root))
    except OSError as err:
        print(f"[talog] 폴더 접근 실패: {root} — {err}", file=sys.stderr)
        return []
    for d in entries:
        p = os.path.join(root, d)
        if os.path.isdir(p) and _is_day_folder(p):
            days.append(p)
    return days


def _next_day_dir(day_dir: str) -> str:
    """같은 설비 폴더 안에서 바로 다음 날짜 폴더를 찾는다 (없으면 빈 문자열)."""
    day_dir = os.path.abspath(day_dir)      # 상대 경로 시 dirname="" 방지
    parent = os.path.dirname(os.path.normpath(day_dir))
    me = os.path.basename(os.path.normpath(day_dir))
    try:
        sibs = sorted(d for d in os.listdir(parent)
                      if os.path.isdir(os.path.join(parent, d))
                      and d[:1].isdigit())
        i = sibs.index(me)
    except (OSError, ValueError):
        return ""                            # 스티칭은 부가 기능 — 조용히 강등
    return os.path.join(parent, sibs[i + 1]) if i + 1 < len(sibs) else ""


def _llm_overview(findings, tag: str, model: str = "qwen2.5:7b") -> str:
    """룰 진단 소견을 로컬 LLM(Ollama)에 넘겨 종합 소견 한 단락을 받는다.

    제미나이式 3단 파이프라인의 3단계: 정제된 컨텍스트만 주입하므로
    로그 원문을 LLM 에 넣지 않는다. Ollama 미가동/실패 시 빈 문자열.
    """
    from .ask import OllamaBackend, _http_json, _OLLAMA
    if not OllamaBackend.alive():
        return ""
    lines = []
    for f in findings:
        lines.append(f"[{f.severity}] {f.title}")
        lines.extend(f"  - {e}" for e in f.evidence[:5])
    prompt = (
        f"당신은 검사 설비 로그 분석 전문가다. 아래는 '{tag}' 일자 로그에 대한 "
        f"룰 엔진 진단 소견이다. 현장 관리자에게 보고하듯 5문장 이내 한국어 "
        f"격식체로 종합 소견을 작성하라. 가장 심각한 문제부터, 수치를 인용하고, "
        f"마지막 문장은 권고 조치로 끝내라. 소견에 없는 내용은 지어내지 마라.\n\n"
        + "\n".join(lines))
    system = ("모든 출력은 반드시 한국어로만 작성한다. 중국어와 영어 문장을 "
              "절대 사용하지 않는다.")

    def _hangul_ratio(s: str) -> float:
        letters = [c for c in s if c.isalpha()]
        if not letters:
            return 0.0
        return sum("가" <= c <= "힣" for c in letters) / len(letters)

    try:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]
        text = ""
        for _ in range(2):   # 소형 모델이 중국어로 이탈하면 1회 재요청
            r = _http_json(f"{_OLLAMA}/api/chat",
                           {"model": model, "stream": False, "messages": msgs,
                            "options": {"temperature": 0.2}}, {}, timeout=180)
            text = ((r.get("message") or {}).get("content") or "").strip()
            if _hangul_ratio(text) >= 0.5:
                return text
            msgs.append({"role": "assistant", "content": text})
            msgs.append({"role": "user",
                         "content": "한국어가 아닙니다. 같은 내용을 전부 "
                                    "한국어로만 다시 작성하십시오."})
        return text if _hangul_ratio(text) >= 0.3 else ""
    except Exception:
        # LLM 은 부가 기능 — 어떤 실패도 리포트 생성을 막지 않는다
        return ""


def scan_day(day_dir: str, recipe: Recipe | None, out_dir: str, tag: str,
             fast: bool = False, detail_cap: int = 60, llm: bool = False) -> str:
    t0 = time.time()
    files = scan_day_folder(day_dir)
    if fast:
        for f in files:
            if f.category == "relgraph":
                f.core = False
    core = [f for f in files if f.core]
    print(f"[talog] {day_dir}")
    print(f"  파일 {len(files)}개 중 핵심 {len(core)}개 파싱 시작"
          + (" (fast: 종속성 그래프 생략)" if fast else ""))

    ex = Extractor()
    all_events: list[Event] = []      # 비-alg 이벤트 + alg 요약 이벤트(로드/에러)
    runs = []
    dl_channels: dict[int, str] = {}
    file_rows = []
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, f"{tag}.sqlite")
    try:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    except OSError:
        # 다른 프로세스(talog ask 등)가 DB 를 열고 있으면 새 파일로 강등
        db_path = os.path.join(out_dir, f"{tag}_{int(time.time())}.sqlite")
        print(f"  ! 기존 DB 가 사용 중 — {os.path.basename(db_path)} 로 진행")
    con = store.open_db(db_path)

    # 익일 스티칭 준비: 다음 날짜 폴더의 alg/comm 파일 맵
    nxt = _next_day_dir(day_dir)
    next_alg: dict[int, object] = {}
    next_comm = []
    if nxt:
        for nf in scan_day_folder(nxt):
            if nf.category == "alg" and nf.alg_kind == "dl":
                next_alg[nf.alg_idx] = nf
            elif nf.category == "comm":
                next_comm.append(nf)

    stitched = False
    max_ts = 0.0
    fid = 0
    # alg 요약으로 보존할 이벤트 종류 (원시 대량 이벤트는 런 조립 후 해제)
    _ALG_KEEP = ("MODEL_FAIL", "INIT_START", "INIT_END", "ERROR")

    comm_end = 0.0        # END 커버리지 종점 (comm 로그의 마지막 시각)
    for fi in files:
        parsed = fi.core
        evs: list[Event] = []
        n = 0
        if parsed:
            try:
                evs, n = ex.extract_file(fi, fid + 1)
            except (OSError, ValueError, UnicodeError) as err:
                print(f"  ! {fi.name}: {err}")
                parsed = False
        fid = store.insert_file(con, fi, parsed, n, len(evs))
        if parsed and evs:
            max_ts = max(max_ts, evs[-1].ts)
            if fi.category == "comm":
                comm_end = max(comm_end, evs[-1].ts)
            if fi.category == "alg":
                if fi.alg_kind == "dl":
                    dl_channels[fi.alg_idx] = fi.channel
                    # 익일 첫 구간 스티칭 (자정 직전 실행의 완료 복원)
                    nf = next_alg.get(fi.alg_idx)
                    if nf is not None:
                        try:
                            # 익일 자정 + N시간까지만 읽고 조기 종료 (성능)
                            cut = evs[-1].ts + (24 + _STITCH_HOURS) * 3600
                            nevs, _ = ex.extract_file(nf, 0, until_ts=cut)
                            if nevs:
                                cut = nevs[0].ts + _STITCH_HOURS * 3600
                                nevs = [e for e in nevs
                                        if e.ts > evs[-1].ts and e.ts <= cut]
                                if nevs:
                                    evs = evs + nevs
                                    stitched = True
                        except (OSError, ValueError, UnicodeError):
                            pass
                    runs.extend(build_runs_for_file(fi.alg_idx, fi.channel, evs))
                keep = [e for e in evs if e.kind in _ALG_KEEP]
                if keep:
                    store.insert_events(con, keep)
                    all_events.extend(keep)
            else:
                if fi.category == "dlinfer":
                    # 고케이던스 사이트의 GPU 계측 홍수는 균등 샘플링으로 상한
                    execs = [e for e in evs if e.kind == "DLINFER_EXEC"]
                    if len(execs) > 300_000:
                        step = len(execs) // 300_000 + 1
                        evs = [e for e in evs if e.kind != "DLINFER_EXEC"] \
                            + execs[::step]
                        evs.sort(key=lambda e: e.ts)
                store.insert_events(con, evs)
                all_events.extend(evs)
        if parsed:
            file_rows.append((fi.name, fi.category, fi.size, n, len(evs)))
        del evs

    all_events.sort(key=lambda e: e.ts)
    log_start = all_events[0].ts if all_events else 0.0
    log_end = max_ts or (all_events[-1].ts if all_events else 0.0)

    # comm 스티칭: 익일 INSPECT_END 로 경계 검사의 완료 신호를 복원
    if next_comm and log_end:
        cutoff = log_end + _STITCH_HOURS * 3600
        for nf in next_comm:
            try:
                nevs, _ = ex.extract_file(nf, 0, until_ts=cutoff)
            except (OSError, ValueError, UnicodeError):
                continue
            nevs = [e for e in nevs if log_end < e.ts <= cutoff]
            if nevs:
                stitched = True
                all_events.extend(nevs)
                comm_end = max(comm_end, nevs[-1].ts)   # END 커버리지 연장
        all_events.sort(key=lambda e: e.ts)
    if stitched:
        print(f"  익일({os.path.basename(nxt)}) 첫 {_STITCH_HOURS:.0f}시간 스티칭 적용")

    # 조립 ('이 날짜의 검사'만 집계: log_end 기준, END 판정은 comm 커버리지 기준)
    gens = build_process_gens(all_events, max(log_end, max_ts))
    inspections = build_inspections(all_events, runs, dl_channels, gens, log_end,
                                    comm_end_ts=comm_end)
    inspections = [i for i in inspections if not i.start_ts or i.start_ts <= log_end]
    model_loads = build_model_loads(all_events, dict(dl_channels))

    # DB 저장
    con.executemany(
        "INSERT INTO inspections(inner_id,product_id,start_ts,start_text,wait_threads,"
        "ack_status,end_ts,end_text,end_result,status,duration_s,n_fed,n_done,n_lost,"
        "n_nofeed,n_skipped,n_zones,n_zones_done,lost_channels,nofeed_channels,"
        "remain_list,gen_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i.inner_id, i.product_id, i.start_ts, i.start_text, i.wait_threads,
          i.ack_status, i.end_ts, i.end_text, i.end_result, i.status,
          (i.end_ts - i.start_ts) if i.end_ts and i.start_ts else 0,
          i.n_fed, i.n_done, i.n_lost, i.n_nofeed, i.n_skipped,
          i.n_zones, i.n_zones_done,
          ",".join(i.lost_channels), ",".join(i.nofeed_channels),
          i.remain_list, i.gen_id) for i in inspections])
    con.executemany(
        "INSERT INTO model_loads(kind,ts,ts_text,name,alg_idx,dur_s,status) "
        "VALUES(?,?,?,?,?,?,?)",
        [(m.kind, m.ts, m.ts_text, m.name, m.alg_idx, m.dur_s, m.status)
         for m in model_loads])
    con.executemany(
        "INSERT INTO channel_runs(inner_id,alg_idx,channel,exec_no,feed_ts,feed_text,"
        "roi_idx,pre_ms,infer_start_ts,infer_end_ts,infer_ms,post_ms,model,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.inner_id, r.alg_idx, r.channel, r.exec_no, r.feed_ts, r.feed_text,
          r.roi_idx, r.pre_ms, r.infer_start_ts, r.infer_end_ts, r.infer_ms,
          r.post_ms, r.model, r.status) for r in runs])
    con.executemany(
        "INSERT INTO process_gens(id,start_ts,start_text,end_ts,end_text,end_cause) "
        "VALUES(?,?,?,?,?,?)",
        [(g.gen_id, g.start_ts, g.start_text, g.end_ts, g.end_text, g.end_cause)
         for g in gens])
    if recipe:
        con.executemany(
            "INSERT INTO recipe_algs(idx,name,alg_id,alg_type,roi_idx,dl_model) "
            "VALUES(?,?,?,?,?,?)",
            [(a.idx, a.name, a.alg_id, a.alg_type,
              ",".join(map(str, a.roi_idx)), ",".join(map(str, a.dl_model)))
             for a in recipe.algs.values()])
        con.executemany(
            "INSERT INTO recipe_models(idx,name,model_file,dev_index,instance_count,infer_dll) "
            "VALUES(?,?,?,?,?,?)",
            [(m.idx, m.name, m.model_file, m.dev_index, m.instance_count, m.infer_dll)
             for m in recipe.models.values()])
    con.execute("INSERT INTO meta VALUES('day_dir',?)", (day_dir,))
    con.execute("INSERT INTO meta VALUES('version',?)", (__version__,))
    con.commit()
    con.close()

    # 리포트 + 자동 진단
    ctx = ReportContext(title=tag, day_dir=day_dir, recipe=recipe,
                        inspections=inspections, runs=runs, gens=gens,
                        events=all_events, dl_channels=dl_channels,
                        file_rows=file_rows, log_start=log_start, log_end=log_end,
                        stitched=stitched, detail_cap=detail_cap,
                        model_loads=model_loads)
    ctx.findings = diagnose(inspections, runs, gens, all_events, model_loads,
                            log_start, log_end, usage_result=_usage_analysis(ctx))
    # 사례 KB: 심각/주의 소견이 있으면 유사 과거 사례를 첨부한다
    if any(f.severity in ("crit", "warn") for f in ctx.findings):
        try:
            from .casekb import CaseKB
            kb = CaseKB()
            if kb.cases:
                q = "\n".join(f.title + " " + " ".join(f.evidence[:2])
                              for f in ctx.findings
                              if f.severity in ("crit", "warn"))
                ctx.similar_cases = [
                    (round(s, 2), c.title, c.cause, c.action, c.site, c.date)
                    for s, c in kb.search(q, 3)]
        except Exception:
            pass
    if llm:
        ctx.llm_summary = _llm_overview(ctx.findings, tag)
        if ctx.llm_summary:
            print("  로컬 LLM 종합 소견 생성 완료")
    out_html = os.path.join(out_dir, f"{tag}.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(render(ctx))
    # 진단 소견을 마크다운으로도 남긴다 (LLM 컨텍스트/보고용)
    with open(os.path.join(out_dir, f"{tag}_diagnosis.md"), "w",
              encoding="utf-8") as f:
        f.write(f"# {tag} 자동 진단 소견\n\n")
        for fd in ctx.findings:
            f.write(f"## [{fd.severity}] {fd.title}\n")
            for evd in fd.evidence:
                f.write(f"- {evd}\n")
            if fd.advice:
                f.write(f"- 권고: {fd.advice}\n")
            f.write("\n")

    ninsp = len(inspections)
    nbad = sum(1 for i in inspections
               if i.status not in ("complete", "in_progress_eof",
                                   "sim_complete", "sim_partial"))
    print(f"  검사 {ninsp}건 (이상 {nbad}건), 이벤트 {len(all_events):,}개, "
          f"세대 {len(gens)}개 — {time.time() - t0:.1f}s")
    print(f"  → {out_html}")
    return out_html


def main(argv=None):
    # 콘솔 코드페이지(cp949)에서도 안전하게 출력한다 (frozen exe 대응)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ask":
        from .ask import main as ask_main
        return ask_main(argv[1:])
    if argv and argv[0] == "watch":
        from .watch import main as watch_main
        return watch_main(argv[1:])
    if argv and argv[0] == "kb":
        from .casekb import main as kb_main
        return kb_main(argv[1:])
    ap = argparse.ArgumentParser(prog="talog", description="talos 로그 진단 분석기")
    ap.add_argument("logs", help="설비-일자 폴더 또는 설비 폴더")
    ap.add_argument("--recipe", help="레시피 폴더 (제품 폴더 또는 버전 폴더)")
    ap.add_argument("--recipe-hint", default="", help="레시피 버전 매칭 힌트 문자열")
    ap.add_argument("--out", default="", help="출력 폴더 (기본: <logs>\\talog_out)")
    ap.add_argument("--fast", action="store_true",
                    help="대용량 종속성 그래프 로그 생략 (스킵 판정 비활성)")
    ap.add_argument("--detail", type=int, default=60,
                    help="간트 상세를 내장할 검사 수 상한 (기본 60)")
    ap.add_argument("--open", action="store_true", help="완료 후 리포트 자동 열기")
    ap.add_argument("--llm", action="store_true",
                    help="로컬 LLM(Ollama) 종합 소견을 리포트에 추가")
    args = ap.parse_args(argv)

    recipe = None
    if args.recipe:
        try:
            recipe = load_recipe(args.recipe, args.recipe_hint)
            print(f"[talog] 레시피 로드: {recipe.root} [{recipe.version}] "
                  f"alg={recipe.alg_count} thread={recipe.thread_count} "
                  f"models={len(recipe.models)}")
        except (FileNotFoundError, OSError) as err:
            print(f"[talog] 경고: 레시피를 열 수 없어 레시피 없이 진행합니다 "
                  f"— {err}")

    args.logs = os.path.abspath(args.logs)
    if not os.path.isdir(args.logs):
        print(f"[talog] 오류: 로그 폴더가 존재하지 않습니다 — {args.logs}",
              file=sys.stderr)
        return 2
    days = _collect_day_folders(args.logs)
    if not days:
        print("[talog] 분석 가능한 로그 폴더를 찾지 못했습니다. 폴더 안에 "
              "alg\\ 하위 폴더 또는 InspStarter.log 가 있어야 합니다.",
              file=sys.stderr)
        return 2
    out_dir = args.out or os.path.join(args.logs, "talog_out")
    outputs = []
    for d in days:
        base = os.path.basename(os.path.normpath(args.logs))
        tag = f"{base}_{os.path.basename(os.path.normpath(d))}" if d != args.logs else base
        tag = tag.replace("#", "")
        try:
            outputs.append(scan_day(d, recipe, out_dir, tag,
                                    fast=args.fast, detail_cap=args.detail,
                                    llm=args.llm))
        except Exception as err:      # 일자 하나의 실패가 전체를 막지 않는다
            print(f"[talog] 오류: {d} 분석 실패 — {err}", file=sys.stderr)
    if not outputs:
        print("[talog] 생성된 리포트가 없습니다.", file=sys.stderr)
        return 1
    print(f"[talog] 완료 — 리포트 {len(outputs)}건")
    if args.open:
        for o in outputs:
            try:
                os.startfile(o)   # noqa: S606 — 로컬 리포트 열기
            except OSError:
                print(f"  ! 자동 열기 실패 — 직접 여십시오: {o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
