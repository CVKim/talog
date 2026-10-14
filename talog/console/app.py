"""Console — 콘솔 화면 하나의 상태와 동작. 영역별 믹스인을 물려받는다.

  ops.py        감시 수명·설정 저장·운영 현황·수집 폴더        incidents.py  사건·시간별 기록·메일 미리보기
  analysis.py   리포트·재현 작업·리포트 화면                  claude.py     Claude 연동(바로 묻기·스킬 카드)
  checks.py     설정 화면의 시험                               modes.py      모드(현장·운영자·개발자) 바꾸기
  http.py       HTTP 경로 → 이 메서드들 (경로마다 필요한 모드를 oper(…)·dev(…) 로 적는다)

믹스인끼리 주고받는 상태는 모두 여기 __init__ 에 있다 — 새 상태를 더하면 여기에 적는다.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from collections import deque

from .. import access, settings
from .analysis import AnalysisMixin
from .checks import ChecksMixin
from .claude import ClaudeMixin
from .incidents import IncidentsMixin
from .modes import ModeMixin
from .ops import OpsMixin


class Console(OpsMixin, IncidentsMixin, AnalysisMixin, ClaudeMixin, ChecksMixin, ModeMixin):
    """talog.yaml 하나의 콘솔. 감시(LiveWatch)는 작업 스레드, 분석 작업·Claude 는 하위 프로세스로 돈다.

    mode 를 주면 talog.yaml 의 mode 대신 쓴다 (`talog --mode developer` — 이번 실행만, 파일에는 쓰지 않는다)."""

    def __init__(self, config_path: str, port: int = 8778, mode: str = ""):
        self.config_path = os.path.abspath(config_path)
        self.port = port
        self.settings, self.legacy = settings.load(self.config_path)
        self.cfg = settings.compile(self.settings)
        self.mode = access.resolve_mode(self.settings.get("mode"), mode)
        self._pin_fail: list = []                  # 최근에 PIN 을 틀린 시각들
        self.token = secrets.token_urlsafe(24)
        self.log: deque = deque(maxlen=400)
        self.watch = None
        self._stop = None
        self._thread = None
        self._lock = threading.RLock()
        self.job: dict = {"state": "idle"}
        self.ask_job: dict = {"state": "idle"}     # Claude 에게 바로 묻기 — 지금(또는 마지막) 질문 하나
        self.ask_thread: dict = {"session": "", "turns": []}   # 한 대화 (이어 묻기는 같은 Claude 세션을 이어 간다)
        self.claude_usage: dict = {}               # 마지막으로 본 구독 사용량 (rate_limit_event)
        self._dbs: dict[str, sqlite3.Connection] = {}
        self._db_lock = threading.Lock()
        self._llm_alive = (0.0, False)
