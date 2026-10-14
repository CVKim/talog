"""진단 리포트 — 하루치 로그 분석 결과를 독립 실행 HTML 한 장으로 그린다.

공개 진입점은 `ReportContext`(입력)와 `render(ctx) -> str`(HTML) 두 개다. 화면 구역마다 모듈이 나뉘어 있다:

    context      입력 자료와 검사 상태 어휘          charts     SVG 차트와 요약 문장
    util         이스케이프·색·시각·아이콘 도구       payload    페이지 스크립트용 JSON
    inspections  Tact·미완료·NG·재시작 구역          findings   자동 진단 소견 카드
    errors       에러·예외 구역                     models     모델 로드·모델별 GPU
    resources    메모리·GPU 자원 구역               document   페이지 조립 (assets/report.css·report.js)

새 구역을 넣을 때는 해당 모듈에 `_xxx_section(ctx) -> str` 을 만들고 document.render 의 본문 틀에 끼운다.
예전 `talog.report` 단일 모듈의 이름은 모두 여기서 그대로 가져다 쓸 수 있다."""

from .context import _STATUS_KO, _STATUS_COLOR, _OK_STATUSES, _BAD_STATUSES, ReportContext
from .util import _esc, _CVAR, _c, _fmt_ts, _translate_alglist, _ico
from .payload import _build_payload, _build_graph
from .charts import _timeline_svg, _timeline_summary, _hourly_svg, _dist_svg, _duration_hist_svg, _series_svg
from .inspections import _tact_section, _incomplete_section, _ng_recipe_segments, _ng_section, _gens_section
from .findings import _SEV_STYLE, _SEV_ORDER, _findings_section
from .errors import _ERR_KIND_DESC, _err_group_key, _errors_section, _errors_summary
from .models import _NO_ACK, _model_bar_summary, _models_section, _gpu_model_section
from .resources import _usage_mini, _usage_analysis, _usage_section, _gpu_mini, _gpu_resource_section
from .document import _wordmark, _JS, render

__all__ = ["_STATUS_KO", "_STATUS_COLOR", "_OK_STATUSES", "_BAD_STATUSES", "ReportContext", "_esc", "_CVAR",
    "_c", "_fmt_ts", "_translate_alglist", "_ico", "_build_payload", "_build_graph", "_timeline_svg",
    "_timeline_summary", "_hourly_svg", "_dist_svg", "_duration_hist_svg", "_series_svg", "_tact_section",
    "_incomplete_section", "_ng_recipe_segments", "_ng_section", "_gens_section", "_SEV_STYLE", "_SEV_ORDER",
    "_findings_section", "_ERR_KIND_DESC", "_err_group_key", "_errors_section", "_errors_summary", "_NO_ACK",
    "_model_bar_summary", "_models_section", "_gpu_model_section", "_usage_mini", "_usage_analysis",
    "_usage_section", "_gpu_mini", "_gpu_resource_section", "_wordmark", "_JS", "render"]
