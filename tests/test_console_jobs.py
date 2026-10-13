"""콘솔 분석 작업(진단 리포트·경보 재현)은 별도 프로세스에서 돈다 — 콘솔 응답이 늦어지지 않게."""
import datetime as dt
import os
import tempfile
import time

from talog import settings
from talog.console import Console


def _write_day(logs: str, day: str = "12") -> str:
    """작은 하루치 로그 (스레드 2개·투입 3초·소요 2초, 30건)."""
    d = os.path.join(logs, day)
    os.makedirs(d, exist_ok=True)
    t0 = dt.datetime.now().replace(microsecond=0) - dt.timedelta(minutes=10)

    def tt(sec):
        return (t0 + dt.timedelta(seconds=sec)).strftime("%Y/%m/%d-%H:%M:%S.%f")[:-3]

    ins, com = [], []
    for k in range(1, 31):
        inner, s = f"20000261013{k:08d}", 3.0 * k
        ins.append(f"{tt(s)}\t[Debug][CCommAiv::OnCommRecv][11]\tRecive M2V_INSPECT_START. innerId: {inner}, "
                   f"productId: ProductID, groupId: 1")
        ins.append(f"{tt(s + 0.001)}\t[Debug][CInspStarter::StartSeq][22]\teAIVProtocol_M2V_InspStart is arrived. "
                   f"Waiting thread count = 2, innerid: {inner}, productId: ProductID")
        com.append(f"{tt(s + 0.002)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_START_ACK,V3.0,TALOS1,OK,{inner},ProductID,1")
        com.append(f"{tt(s + 2.0)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_END,V3.0,TALOS1,OK,{inner},ProductID,1")
    for name, lines in (("InspStarter.log", ins), ("comm.log", com)):
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
            f.write("".join(x + "\r\n" for x in lines))
    return d


def _wait(con, timeout=180):
    t0 = time.time()
    while con.job.get("state") == "running" and time.time() - t0 < timeout:
        time.sleep(0.2)
    return con.job


def test_report_and_replay_jobs_run_in_child_process(tmp_path):
    con = Console(os.path.join(str(tmp_path), "talog.yaml"))
    con.settings["data_dir"] = str(tmp_path / "data")
    con.settings["site"] = "시험 설비"
    con.cfg = settings.compile(con.settings)
    day = _write_day(str(tmp_path / "logs"))

    assert not con.start_job("report", str(tmp_path / "없는 폴더"))["ok"]
    assert not con.start_job("bogus", day)["ok"]
    assert con.start_job("report", day) == {"ok": True}
    assert "진행 중" in con.start_job("replay", day)["error"]            # 한 번에 하나
    j = _wait(con)
    assert j["state"] == "done", list(j["log"])
    assert j["pid"] != os.getpid()                                     # 콘솔과 다른 프로세스
    assert j["reports"] and "리포트 1건 완료" in "\n".join(j["log"])
    assert os.path.exists(os.path.join(con.report_dir, j["reports"][0] + ".html"))
    assert any(r["tag"] == j["reports"][0] for r in con.reports())
    tmp = tempfile.gettempdir()                                         # 넘긴 설정 파일은 지운다
    assert not [f for f in os.listdir(tmp) if f.startswith("talog_job_")
                and os.path.getmtime(os.path.join(tmp, f)) > j["started"]]

    assert con.start_job("replay", day) == {"ok": True}
    j = _wait(con)
    assert j["state"] == "done", list(j["log"])
    assert "리플레이" in "\n".join(j["log"])
