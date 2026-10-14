"""talog 콘솔 — AI 비전 로그 운영 플랫폼의 로컬 웹 화면.

    talog [--config talog.yaml] [--port 8778] [--no-open] [--no-watch] [--mode field|operator|developer]

화면이 에이전트 흐름을 따른다 (왼쪽 위 모드 전환: 현장 · 운영자 · 개발자 — modes.py):
  운영 현황  수집 → 탐지 → 판단 → 알림 상태와 오늘의 사건·경보·처리 여유
  사건       사건별 근거·룰 진단·LLM 의견·메일, 사건에 대한 질문
  분석       로그 폴더 진단 리포트·경보 재현(리플레이)·리포트에 대한 자연어 질문
  Claude     구독 Claude Code 가 talog 도구로 답한다 (작업 스킬 카드·바로 묻기) — 개발자 모드에서만
  설정       모드 · 수집·탐지(경보 규칙 한 표)·판단(AI)·알림(메일) — talog.yaml 로 저장(운영자 모드 이상)

패키지 구성 (app.py 의 Console 이 영역별 믹스인을 물려받는다):
  app.py        Console 과 공유 상태       http.py      HTTP 경로 표·요청 처리·serve()
  ops.py        감시·설정·운영 현황         incidents.py 사건·시간별 기록·메일 미리보기
  analysis.py   리포트·재현 작업            claude.py    Claude 연동
  checks.py     설정 화면의 시험            jobs.py      분석 작업 하위 프로세스(talog _job)
  page.py       talog/web 소스 → 페이지 하나   util.py      경로·일자 폴더·리포트 이름
  modes.py      모드 바꾸기(위로 올릴 때 PIN)

- 실시간 감시(LiveWatch)는 작업 스레드로 돈다. 설정을 저장하면 파일을 .bak 으로 백업한 뒤 다시 쓰고
  감시를 새 설정으로 재시작한다.
- 127.0.0.1 에서만 연다. POST 는 페이지에 심은 세션 토큰(X-Talog-Token)이 있어야 받고 Host 헤더도
  확인한다(다른 사이트의 위조 요청·DNS 리바인딩 차단).
- 메일 비밀번호·클라이언트 암호는 평문으로 저장하지 않는다: 입력하면 Windows DPAPI 로 암호화해
  mail.secret 에 넣고, 화면·API 로 다시 내보내지 않는다.
"""

from .app import Console
from .http import GET_ROUTES, POST_ROUTES, _handler_factory, page_html, serve
from .jobs import job_main
from .page import build_page
from .util import _latest_day_dir, _report_tag, recent_days, tail2_path

__all__ = ["Console", "GET_ROUTES", "POST_ROUTES", "_handler_factory", "_latest_day_dir", "_report_tag", "build_page",
           "job_main", "page_html", "recent_days", "serve", "tail2_path"]
