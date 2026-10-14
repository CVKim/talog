"""talog watch — 예지보전 상주 감시 모드.

검사 프로그램(talos)이 메인으로 도는 설비 PC 에서 부하를 최소화하며
`D:\\AIV_LOG\\<프로세스>\\<YYYY_MM>\\<DD>\\` 로그를 증분(tail)으로 읽어
이상 징후를 조기 감지하고, 별도의 예지보전 로그(alerts JSONL)와
토스트 팝업/웹훅으로 알린다.

저부하 설계:
  - 폴링 tail (기본 20초, 새로 쓰인 바이트만 읽음)
  - 프로세스 우선순위 BELOW_NORMAL 강등
  - 감시 대상은 소형 플랫폼 로그 6종만 (alg/relgraph 등 대용량 제외)
  - LLM 은 선택 기능이며 기본 CPU 모드(num_gpu=0)로 검사 GPU 를 건드리지 않음

모듈:
    config     기본 설정·talog.yaml 읽기        alerts     Alert·Notifier
    tail       로그 증분 읽기(TailReader)        rules      룰 엔진(RuleEngine)
    gpu        GPU 감시                          status     현장 상태 페이지
    wiring     처리 여유 상태·에이전트 연결       live       실시간 감시(LiveWatch)
    replay     리플레이·사후 처리 여유 검토       selfcheck  설치 자가 점검·시험 메일
    cli        명령행 (`python -m talog.watch` 도 같다)

사용 (콘솔 없이):
  talog run [--config talog.yaml] [--once]
  talog run --replay <일자 폴더>      # 사고 재현 검증
  talog run --check                  # 설치 자가 점검
예전 `talog.watch` 단일 모듈의 이름은 모두 여기서 그대로 가져다 쓸 수 있다."""

from .config import _WATCH_FILES, _DEFAULT_CFG, _deep_merge, load_config, _today_dir
from .alerts import Alert, Notifier
from .tail import TailReader, iter_batchrun_line
from .rules import _defect_rules, RuleEngine
from .gpu import _parse_smi, _query_gpu, GpuMonitor
from .status import _agent_status_html, _write_status
from .wiring import _make_agent, _attach_capacity_state, _ENGINE_KINDS
from .live import _lower_priority, LiveWatch, run_live
from .replay import _replay_file, _REVIEW_KINDS, capacity_review, run_replay
from .selfcheck import run_check, run_test_email
from .cli import main

__all__ = ["_WATCH_FILES", "_DEFAULT_CFG", "_deep_merge", "load_config", "_today_dir", "Alert", "Notifier",
    "TailReader", "iter_batchrun_line", "_defect_rules", "RuleEngine", "_parse_smi", "_query_gpu",
    "GpuMonitor", "_agent_status_html", "_write_status", "_make_agent", "_attach_capacity_state",
    "_ENGINE_KINDS", "_lower_priority", "LiveWatch", "run_live", "_replay_file", "_REVIEW_KINDS",
    "capacity_review", "run_replay", "run_check", "run_test_email", "main"]


# 예전 단일 모듈에서 함께 보이던 이름 — 테스트가 watch.Extractor 로 쓴다
from ..events import Extractor  # noqa: E402

__all__ += ["Extractor"]
