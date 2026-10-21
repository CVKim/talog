"""v2.4 쉬운 화면 — 운영 현황은 지금 · 오늘 · 자세히 세 덩어리(숫자 카드·단계 카드는 없앰), 사건 상세는 세 줄 요약,
설정은 고급 칸을 접어 둔다. 운영자 모드 일(시험 경보·실행 로그·재현 포함)은 현장 모드에서 보이지 않는다."""
import re

from talog.console import build_page


def test_home_is_three_blocks_without_repeated_cards():
    page = build_page()
    for i in ("opsHero", "opsBrief", "briefList", "todaySeg", "opsInc", "opsAlerts", "opsMore", "moreTabs",
              "mp-cap", "mp-strip", "mp-gpu", "mp-test", "opsTol", "capBullets", "opsStrip", "gpuBox", "logDetails"):
        assert f'id="{i}"' in page, i
    # 같은 사실을 되풀이하던 숫자 카드·단계 카드와 사이드바 설명 줄은 없다
    for gone in ('id="opsKpis"', 'id="flow"', "renderKpis", "renderFlow", 'class="side-meta"'):
        assert gone not in page, gone


def test_operator_only_parts_hide_in_field_mode():
    page = build_page()
    assert 'html[data-mode="field"] .oper-only{display:none!important}' in page
    assert 'data-more="test" class="oper-only"' in page                       # 시험 경보 · 실행 로그 탭
    assert '<label class="switch oper-only"><input type="checkbox" id="incReplay"' in page
    assert 'rb.hidden = st.running && !atLeast("operator")' in page            # 감시 정지 버튼


def test_incident_summary_and_folded_settings():
    page = build_page()
    assert '<dl class="isum">' in page and '["sum", "요약"]' in page            # 무슨 일 · 원인 · 할 일
    for i in ("rulesBox", "rulesSum", "trkBox", "llmDev", "askCard", "repWrap"):
        assert f'id="{i}"' in page, i
    assert re.search(r'<details class="card hidden" id="advCard">', page)


def test_tour_shows_once_more_after_redesign():
    page = build_page()
    assert 'TOUR_VER = "2.4"' in page and "done !== TOUR_VER" in page
    assert 'hl: "#opsKpis"' not in page and 'hl: "#opsMore"' in page
