"""Tests for the run infrastructure, using a local bare repo as the 'GitHub' remote."""

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_commit_msg import check  # noqa: E402
from uncertainscene.infra.budget import Budget, load_budget  # noqa: E402
from uncertainscene.infra.guard import Guard, ResourceLimit, Usage  # noqa: E402
from uncertainscene.infra.publish import Publisher, PublishError  # noqa: E402
from uncertainscene.infra.runner import Run  # noqa: E402
from uncertainscene.infra.state import DONE, FAILED, INTERRUPTED, RUNNING, RunState  # noqa: E402


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def remote(tmp_path):
    """A bare 'GitHub' repo with one commit on main, and a factory for clones of it."""
    bare = tmp_path / "remote.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(bare), str(seed), cwd=tmp_path)
    for k, v in [("user.email", "t@t"), ("user.name", "t")]:
        sh("git", "config", k, v, cwd=seed)
    (seed / "README.md").write_text("x\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-q", "-m", "docs: seed", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)

    def clone(name):
        d = tmp_path / name
        sh("git", "clone", "-q", "-b", "main", str(bare), str(d), cwd=tmp_path)
        for k, v in [("user.email", "t@t"), ("user.name", name)]:
            sh("git", "config", k, v, cwd=d)
        return d

    return clone


def fake_usage(ram_used=4.0, ram_total=12.7, disk_free=60.0):
    return lambda: Usage(ram_used_gb=ram_used, ram_total_gb=ram_total, disk_free_gb=disk_free)


def make_run(repo_dir, tmp_path, account, reader=None, run_id="r1"):
    budget = replace(Budget(), repo_dir=str(repo_dir), work_dir=str(tmp_path / f"work-{account}"))
    pub = Publisher(repo_dir, "main", retries=2)
    guard = Guard(budget, reader=reader or fake_usage())
    return Run(run_id, "recon", "main", pub, budget, guard, account)


# ---- budget / guard ----------------------------------------------------------------------
def test_budget_file_loads():
    b = load_budget()
    assert 0 < b.ram_soft_frac < b.ram_hard_frac < 1
    assert b.disk_hard_free_gb < b.disk_soft_free_gb


def test_guard_levels():
    g = Guard(Budget(), reader=fake_usage())
    assert g.level(fake_usage()())[0] == "ok"
    assert g.level(fake_usage(ram_used=10.5)())[0] == "soft"
    assert g.level(fake_usage(ram_used=12.0)())[0] == "hard"
    assert g.level(fake_usage(disk_free=8)())[0] == "soft"
    assert g.level(fake_usage(disk_free=3)())[0] == "hard"


def test_check_runs_soft_callbacks_and_raises_on_hard():
    calls = []
    g = Guard(Budget(), reader=fake_usage(ram_used=10.5))
    g.soft_callbacks.append(lambda: calls.append(1))
    g.check()
    assert calls == [1]
    g.read = fake_usage(ram_used=12.5)
    with pytest.raises(ResourceLimit):
        g.check()
    assert "RAM" in g.tripped


def test_ensure_room_refuses_steps_that_do_not_fit():
    g = Guard(Budget(), reader=fake_usage(disk_free=20))
    g.ensure_room(disk_gb=10, ram_gb=5, minutes=60)
    with pytest.raises(ResourceLimit, match="disk"):
        g.ensure_room(disk_gb=16)
    with pytest.raises(ResourceLimit, match="RAM"):
        g.ensure_room(ram_gb=9)
    late = Guard(Budget(), reader=fake_usage(), start_time=time.time() - 10.5 * 3600)
    with pytest.raises(ResourceLimit, match="min"):
        late.ensure_room(minutes=30)


# ---- state -------------------------------------------------------------------------------
def test_state_roundtrip_and_claim_expiry(tmp_path):
    s = RunState("r1", "recon", "main")
    s.claim("a", "acct-1", expected_minutes=10, commit="abc")
    s.save(tmp_path)
    t = RunState.load_or_new(tmp_path, "r1", "recon", "main")
    assert t.step("a").status == RUNNING
    assert t.claimed_by_other("a", "acct-2")
    assert not t.claimed_by_other("a", "acct-1")
    t.step("a").expires = time.time() - 1
    assert not t.claimed_by_other("a", "acct-2")


# ---- publisher ---------------------------------------------------------------------------
def test_publish_refuses_big_files(remote):
    repo = remote("a")
    (repo / "results").mkdir()
    (repo / "results" / "big.bin").write_bytes(b"0" * (2 * 1024**2))
    pub = Publisher(repo, "main", max_file_mb=1)
    with pytest.raises(PublishError, match="artifact store"):
        pub.publish(["results"], "results(recon): big")


def test_concurrent_publishers_both_land(remote):
    a, b = remote("a"), remote("b")
    for repo, name in [(a, "x"), (b, "y")]:
        (repo / "results" / name).mkdir(parents=True)
        (repo / "results" / name / "m.json").write_text("{}")
    assert Publisher(a, "main").publish(["results/x"], "results(recon): x")
    assert Publisher(b, "main", retries=2).publish(["results/y"], "results(recon): y")  # rebases
    c = remote("c")
    assert (c / "results" / "x" / "m.json").exists() and (c / "results" / "y" / "m.json").exists()


def test_check_push_access_syncs_when_remote_advances(remote):
    a, b = remote("a"), remote("b")
    (b / "remote.txt").write_text("advanced\n")
    Publisher(b, "main").publish(["remote.txt"], "docs: advance remote")

    pub = Publisher(a, "main")
    pub.check_push_access()

    assert pub.git("rev-parse", "HEAD").stdout.strip() == pub.git("rev-parse", "origin/main").stdout.strip()


def test_publish_only_stages_given_paths(remote):
    repo = remote("a")
    (repo / "stray.txt").write_text("not mine")
    (repo / "results").mkdir()
    (repo / "results" / "ok.json").write_text("{}")
    Publisher(repo, "main").publish(["results"], "results(recon): ok")
    assert not (remote("c") / "stray.txt").exists()


# ---- runner ------------------------------------------------------------------------------
def test_step_runs_publishes_and_is_skipped_next_session(remote, tmp_path):
    def fn(out, work):
        assert work.is_dir()
        (out / "metrics.json").write_text(json.dumps({"x": 1}))
        return "fine"

    r1 = make_run(remote("a"), tmp_path, "acct-1")
    assert r1.step("s1", fn) == DONE
    assert not (r1.work_root / "s1").exists()  # scratch removed

    r2 = make_run(remote("b"), tmp_path, "acct-2")  # a different account, fresh clone
    assert r2.step("s1", lambda o, w: pytest.fail("should be skipped")) == "skipped"
    assert (r2.out_dir("s1") / "metrics.json").exists()
    info = json.loads((r2.out_dir("s1") / "step_info.json").read_text())
    assert info["status"] == DONE and info["note"] == "fine"


def test_live_claim_from_other_account_is_skipped(remote, tmp_path):
    r1 = make_run(remote("a"), tmp_path, "acct-1")
    r1._refresh()
    assert r1._claim("s1", minutes=30)
    r2 = make_run(remote("b"), tmp_path, "acct-2")
    assert r2.step("s1", lambda o, w: pytest.fail("claimed elsewhere")) == "skipped"


def test_step_that_does_not_fit_is_not_started(remote, tmp_path):
    r = make_run(remote("a"), tmp_path, "acct-1", reader=fake_usage(disk_free=10))
    assert r.step("big", lambda o, w: pytest.fail("no room"), needs={"disk_gb": 20}) == "not_started"


def test_interrupted_step_is_recorded_and_resumed(remote, tmp_path):
    def blowup(out, work):
        raise ResourceLimit("RAM at 95%")

    r1 = make_run(remote("a"), tmp_path, "acct-1")
    with pytest.raises(ResourceLimit):
        r1.step("s1", blowup)
    r2 = make_run(remote("b"), tmp_path, "acct-2")
    r2._refresh()
    assert r2.state.step("s1").status == INTERRUPTED
    assert r2.step("s1", lambda o, w: None) == DONE


def test_failed_step_needs_explicit_retry(remote, tmp_path):
    r = make_run(remote("a"), tmp_path, "acct-1")
    assert r.step("s1", lambda o, w: 1 / 0) == FAILED
    assert (r.out_dir("s1") / "error.txt").exists()
    assert r.step("s1", lambda o, w: None) == "skipped"
    assert r.step("s1", lambda o, w: None, retry_failed=True) == DONE


def test_runner_commit_messages_pass_the_checker(remote, tmp_path):
    repo = remote("a")
    make_run(repo, tmp_path, "acct-1").step("s1", lambda o, w: None)
    log = subprocess.run(["git", "log", "--format=%s", "-3"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    for msg in log[:2]:
        assert check(msg) is None, msg


# ---- commit message checker --------------------------------------------------------------
@pytest.mark.parametrize("msg,ok", [
    ("feat(recon): register recordings on static background", True),
    ("docs: update outline", True),
    ("results(change): 2026-10-12_pair01/detect done", True),
    ("Merge branch 'exp/recon/a' into path/recon", True),
    ("Added stuff", False),
    ("feat(recon): Register things", False),
    ("feat(recon): register things.", False),
    ("wip: things", False),
    ("feat: " + "x" * 80, False),
])
def test_commit_message_checker(msg, ok):
    assert (check(msg) is None) == ok


def test_background_monitor_stops_a_running_step(remote, tmp_path):
    usage = {"ram": 4.0}
    r = make_run(remote("a"), tmp_path, "acct-1", reader=lambda: fake_usage(ram_used=usage["ram"])())
    r.budget = replace(r.budget, monitor_interval_s=0.05)
    r.guard.budget = r.budget

    def long_step(out, work):
        r.guard.start()
        usage["ram"] = 12.5  # memory climbs past the hard limit mid-step
        time.sleep(5)
        pytest.fail("monitor did not interrupt")

    with pytest.raises(ResourceLimit):
        r.step("s1", long_step)
    r.guard.stop()
    assert r.state.step("s1").status == INTERRUPTED
    assert "RAM" in r.state.step("s1").note


def test_live_log_is_pushed_while_a_step_runs(remote, tmp_path, monkeypatch):
    """A session that dies mid-step must still leave its progress on the branch."""
    monkeypatch.setenv("UNCERTAINSCENE_HEARTBEAT_MIN", str(0.3 / 60))   # every 0.3 s
    a = remote("a")
    run = make_run(a, tmp_path, "colab-1")

    def slow(out, work):
        (out / "step_log.txt").write_text("stage done: reconstruction\nstage done: splat of before\n")
        time.sleep(1.5)
        return "ok"

    assert run.step("s1", slow) == DONE
    b = remote("b")
    live = b / run.live_rel()
    assert live.exists()
    text = live.read_text()
    assert "step s1" in text and "splat of before" in text and "machine:" in text
