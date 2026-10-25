"""모든 시험 공통 — 기본 기록 폴더(D:\\AIV_LOG\\TalogWatch)를 시험마다 임시 폴더로 바꾼다.

설정 파일 없이 만든 콘솔·엔진이 이 PC 의 실제 기록 폴더에 쓰지 않게 한다(v2.5 개발 중 시험이 한 번 그 폴더에 썼다).
읽기만 하는 로그 폴더 기본값은 그대로 둔다 — 기본값을 확인하는 시험이 있다.
"""
import pytest

from talog import settings
from talog.watch import config as watch_config


@pytest.fixture(autouse=True)
def _temp_data_dir(tmp_path_factory, monkeypatch):
    d = str(tmp_path_factory.mktemp("TalogWatch"))
    monkeypatch.setitem(settings.DEFAULTS, "data_dir", d)
    monkeypatch.setitem(watch_config._DEFAULT_CFG, "alert_dir", d)
