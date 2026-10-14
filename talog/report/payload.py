"""페이지 스크립트가 쓰는 JSON 자료 — 검사 요약·상세(_build_payload)와 레시피 종속성 그래프(_build_graph)."""

from __future__ import annotations

import json

from ..assemble import ChannelRun, Inspection
from .context import _BAD_STATUSES, ReportContext
from .util import _translate_alglist


def _build_payload(ctx: ReportContext) -> tuple[str, str]:
    """(요약 JSON, 상세 JSON) 을 만든다. 고케이던스 사이트는 요약을 상한한다."""
    src = ctx.inspections
    # 전 건 내장이 원칙 (일 4.3만 실측 ≈ 3.8MB 로 브라우저 무리 없음).
    # 안전 상한 6만 초과 시에만 이상 전건 + 최근 순으로 절사한다.
    if len(src) > 60000:
        bad = [i for i in src if i.status in _BAD_STATUSES]
        src = bad + [i for i in src if i.status not in _BAD_STATUSES][-60000:]
        src.sort(key=lambda i: i.start_ts or 0)
    summary = []
    for it in src:
        summary.append([
            it.inner_id, it.product_id, round(it.start_ts, 3), it.start_text[:12],
            it.status, round((it.end_ts - it.start_ts), 2) if it.end_ts and it.start_ts else 0,
            it.n_done, it.n_fed, it.ack_status, it.end_result,
        ])

    # 상세 대상 선정: 이상 전건 + 검사시간 상위 + 최근
    by_inner: dict[str, list[ChannelRun]] = {}
    for r in ctx.runs:
        by_inner.setdefault(r.inner_id, []).append(r)

    picked: list[Inspection] = [i for i in ctx.inspections if i.status in _BAD_STATUSES]
    done = [i for i in ctx.inspections if i.status == "complete" and i.end_ts]
    done.sort(key=lambda i: i.end_ts - i.start_ts, reverse=True)
    for i in done[: max(0, ctx.detail_cap - len(picked)) // 2]:
        picked.append(i)
    rest = max(0, ctx.detail_cap - len(picked))
    for i in sorted(done, key=lambda i: i.start_ts, reverse=True)[:rest]:
        if i not in picked:
            picked.append(i)

    detail: dict[str, dict] = {}
    for it in picked:
        rs = sorted(by_inner.get(it.inner_id, []), key=lambda r: (r.infer_start_ts, r.alg_idx))
        base = it.start_ts or (rs[0].infer_start_ts if rs else 0)
        detail[it.inner_id] = {
            "st": it.start_text[:12], "status": it.status,
            "remain": _translate_alglist(ctx.recipe, it.remain_list),
            "lost": it.lost_channels, "nofeed": it.nofeed_channels[:30],
            "lostIdx": it.lost_idx, "nofeedIdx": it.nofeed_idx,
            "skipIdx": it.skipped_idx,
            "runs": [[r.alg_idx, r.channel[:28], r.exec_no,
                      round(r.infer_start_ts - base, 2),
                      round(r.infer_ms / 1000, 2) if r.infer_ms else 0,
                      r.status, r.model[:40], round(r.pre_ms, 1)] for r in rs],
        }

    def dumps(o) -> str:
        return json.dumps(o, ensure_ascii=False, separators=(",", ":")) \
                   .replace("</", "<\\/")

    return dumps(summary), dumps(detail)


def _build_graph(ctx: ReportContext) -> str:
    """종속성 그래프 데이터(JSON): IMG → ROI → ALG 계층 + 일자 요약 통계.

    레시피가 있으면 recipe 의 requireroiidx/baseimgidx 로, 없으면 관측된
    FEED(roi)→ALG 관계로 축약 구성한다.
    """
    day_stats: dict[int, dict] = {}
    for r in ctx.runs:
        s = day_stats.setdefault(r.alg_idx, {"done": 0, "lost": 0})
        if r.status == "done":
            s["done"] += 1
        elif r.status == "lost":
            s["lost"] += 1

    algs = []
    rois: dict[int, dict] = {}
    imgs: dict[int, dict] = {}
    if ctx.recipe and ctx.recipe.algs:
        for idx, a in sorted(ctx.recipe.algs.items()):
            if idx not in ctx.dl_channels and idx not in day_stats:
                # 로그에 없는 alg 도 레시피 구조는 보여준다 (rb/dummy 포함)
                pass
            st = day_stats.get(idx, {"done": 0, "lost": 0})
            algs.append({"id": idx, "label": (a.name or a.alg_id)[:30],
                         "roi": a.roi_idx, "done": st["done"], "lost": st["lost"],
                         "dl": idx in ctx.dl_channels})
            for ri in a.roi_idx:
                r = ctx.recipe.rois.get(ri)
                rois.setdefault(ri, {"id": ri,
                                     "label": (r.name if r else f"ROI{ri}")[:24],
                                     "img": r.base_img if r else 0})
                if r and r.base_img:
                    imgs.setdefault(r.base_img, {"id": r.base_img,
                                                 "label": f"IMG {r.base_img}"})
        has_recipe = True
    else:
        # 관측 기반 축약: 채널 런의 roi_idx → alg
        roi_of_alg: dict[int, set] = {}
        for r in ctx.runs:
            if r.roi_idx:
                roi_of_alg.setdefault(r.alg_idx, set()).add(r.roi_idx)
        for idx, ch in sorted(ctx.dl_channels.items()):
            st = day_stats.get(idx, {"done": 0, "lost": 0})
            rlist = sorted(roi_of_alg.get(idx, []))
            algs.append({"id": idx, "label": ch[:30], "roi": rlist,
                         "done": st["done"], "lost": st["lost"], "dl": True})
            for ri in rlist:
                rois.setdefault(ri, {"id": ri, "label": f"ROI{ri}", "img": 0})
        has_recipe = False

    graph = {"imgs": sorted(imgs.values(), key=lambda x: x["id"]),
             "rois": sorted(rois.values(), key=lambda x: x["id"]),
             "algs": algs, "hasRecipe": has_recipe}
    return json.dumps(graph, ensure_ascii=False, separators=(",", ":")) \
               .replace("</", "<\\/")
