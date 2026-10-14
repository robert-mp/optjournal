"""`scripts/release`, run for real against a local origin.

Nothing touches the network. Origin is a bare repository, and `gh` and `uv` are
stand-ins first on PATH that log each call and answer what the script asks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="a bash script for the maintainer's Mac and Linux")

SCRIPT = ROOT / "scripts" / "release"
RUN_URL = "https://github.com/o/r/actions/runs/4242"

_FAKE = r'''
import json, os, re, sys
from pathlib import Path

home = Path(__file__).resolve().parent.parent
argv = [Path(sys.argv[0]).name, *sys.argv[1:]]
with open(home / "calls.jsonl", "a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")

match argv[:3]:
    case ["gh", "auth", "status"]:
        sys.exit((home / "logged-out").exists())
    case ["gh", "run", "list"]:
        print(4242)
    case ["gh", "run", "watch"]:
        sys.exit(int((home / "ci").read_text()))
    case ["gh", "run", "view"]:
        print("https://github.com/o/r/actions/runs/4242")
    case ["gh", "release", "create"]:
        Path(argv[argv.index("--notes-file") + 1]).read_text()
        print("https://github.com/o/r/releases/tag/" + argv[3])
    case ["uv", "lock"]:
        version = re.search(r'^version = "(.*)"', Path("pyproject.toml").read_text(), re.M)[1]
        lock = Path("uv.lock")
        lock.write_text(re.sub(r'(name = "optjournal"\nversion = ")[^"]*',
                               rf"\g<1>{version}", lock.read_text()))
    case _:
        sys.exit(f"unexpected call: {argv}")
'''

_PYPROJECT = '[project]\nname = "optjournal"\nversion = "0.1.0"\n'
_LOCK = ('version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "optjournal"\n'
         'version = "0.1.0"\nsource = { editable = "." }\n')


class Repo:
    """A clone of a bare origin at 0.1.0, with the stand-ins on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        self.notes = root / "notes.md"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("gh", "uv"):
            fake = bin_dir / name
            fake.write_text(f"#!{sys.executable}\n{_FAKE}")
            fake.chmod(0o755)
        self.ci(0)
        self.notes.write_text("What changed\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.git("clone", "-q", str(self.origin), str(self.clone), cwd=root)
        (self.clone / "pyproject.toml").write_text(_PYPROJECT)
        (self.clone / "uv.lock").write_text(_LOCK)
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        self.git("push", "-q", "-u", "origin", "main")
        # A copy inside the clone, so a script that finds its repository from its
        # own location still lands here. Run from this repository instead, an
        # earlier version committed a fake release to the real main checkout.
        self.script = self.clone / "scripts" / "release"
        self.script.parent.mkdir()
        shutil.copy2(SCRIPT, self.script)

    def git(self, *argv: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *argv], cwd=cwd or self.clone, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def release(self, *argv: str, cwd: Path | None = None,
                script: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(script or self.script), *argv], cwd=cwd or self.clone,
                              env=self.env, capture_output=True, text=True, timeout=60)

    def ci(self, exit_code: int) -> None:
        (self.root / "ci").write_text(str(exit_code))

    def calls(self) -> list[dict]:
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def published(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:3] == ["gh", "release", "create"]]

    def locked(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][0] == "uv"]

    def origin_main(self) -> str:
        return self.git("rev-parse", "main", cwd=self.origin)

    def state(self) -> tuple[str, ...]:
        return (self.git("for-each-ref", cwd=self.origin),
                self.git("for-each-ref", "refs/heads", "refs/tags"),
                self.git("branch", "--show-current"), self.git("status", "--porcelain"),
                self.git("diff"))


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_a_release_merges_the_branch_bumps_and_publishes_the_commit_ci_passed(repo):
    repo.git("switch", "-qc", "feat/x")
    (repo.clone / "feature.txt").write_text("new")
    repo.git("add", "feature.txt")
    repo.git("commit", "-qm", "feat: x")
    repo.git("switch", "-q", "main")
    (repo.clone / "journal.db-wal").write_text("untracked files are allowed")

    done = repo.release("0.2.0", str(repo.notes), "feat/x")

    assert done.returncode == 0, done.stderr
    sha = repo.origin_main()
    assert repo.git("log", "-2", "--format=%s", sha, cwd=repo.origin).splitlines() == [
        "chore(release): 0.2.0", "feat: x"]
    for name in ("pyproject.toml", "uv.lock"):
        assert 'version = "0.2.0"' in repo.git("show", f"{sha}:{name}", cwd=repo.origin), name
    [listed] = [c["argv"] for c in repo.calls() if c["argv"][:3] == ["gh", "run", "list"]]
    assert [_flag(listed, f) for f in ("--commit", "--branch", "--workflow")] == [
        sha, "main", "quality.yml"]
    assert repo.published() == [["gh", "release", "create", "v0.2.0", "--target", sha,
                                  "--title", "0.2.0", "--notes-file", str(repo.notes)]]
    assert "https://github.com/o/r/releases/tag/v0.2.0" in done.stdout


def test_a_red_ci_publishes_nothing_and_the_same_command_then_resumes(repo):
    repo.ci(1)
    failed = repo.release("0.2.0", str(repo.notes))

    assert failed.returncode != 0
    assert repo.published() == []
    assert RUN_URL in failed.stderr
    assert "gh run rerun 4242 --failed" in failed.stderr
    assert f"{repo.script} 0.2.0 {repo.notes}" in failed.stderr
    bumped = repo.origin_main()

    still_red = repo.release("0.2.0", str(repo.notes))
    assert still_red.returncode != 0 and repo.published() == [], "a resume skipped the CI gate"

    repo.ci(0)
    resumed = repo.release("0.2.0", str(repo.notes))

    assert resumed.returncode == 0, resumed.stderr
    assert repo.origin_main() == bumped, "the resume pushed a new commit"
    assert len(repo.locked()) == 1, "the resume bumped again"
    assert [_flag(argv, "--target") for argv in repo.published()] == [bumped]


def test_after_a_real_failure_the_same_command_publishes_the_fix_on_main(repo):
    repo.ci(1)
    repo.release("0.2.0", str(repo.notes))
    (repo.clone / "fix.txt").write_text("fixed")
    repo.git("add", "fix.txt")
    repo.git("commit", "-qm", "fix: what CI caught")

    repo.ci(0)
    done = repo.release("0.2.0", str(repo.notes))

    assert done.returncode == 0, done.stderr
    fixed = repo.origin_main()
    assert repo.git("log", "-2", "--format=%s", fixed, cwd=repo.origin).splitlines() == [
        "fix: what CI caught", "chore(release): 0.2.0"]
    assert [_flag(argv, "--target") for argv in repo.published()] == [fixed]


def _origin_moves_on(repo: Repo, version: str) -> None:
    """Another clone pushes, so the clone's main is behind origin's."""
    other = repo.root / "elsewhere"
    repo.git("clone", "-q", str(repo.origin), str(other), cwd=repo.root)
    (other / "pyproject.toml").write_text(_PYPROJECT.replace("0.1.0", version))
    (other / "elsewhere.txt").write_text("pushed from another clone")
    repo.git("add", ".", cwd=other)
    repo.git("commit", "-qm", f"elsewhere at {version}", cwd=other)
    repo.git("push", "-q", "origin", "main", cwd=other)


def test_behind_an_origin_already_at_the_version_it_catches_up_and_resumes(repo):
    _origin_moves_on(repo, "0.2.0")

    done = repo.release("0.2.0", str(repo.notes))

    assert done.returncode == 0, done.stderr
    assert repo.git("rev-parse", "HEAD") == repo.origin_main()
    assert repo.locked() == [], "origin already said 0.2.0, yet it bumped"
    assert [_flag(argv, "--target") for argv in repo.published()] == [repo.origin_main()]


def _dirty(repo: Repo) -> None:
    (repo.clone / "pyproject.toml").write_text(_PYPROJECT + "# edited\n")


def _tagged(repo: Repo) -> None:
    repo.git("tag", "v0.2.0")
    repo.git("push", "-q", "origin", "v0.2.0")


@pytest.mark.parametrize(("argv", "setup", "says"), [
    pytest.param(["0.2.0"], _dirty, "uncommitted", id="dirty tree"),
    pytest.param(["0.2.0"], lambda repo: repo.git("switch", "-qc", "other"), "on other",
                 id="wrong branch"),
    pytest.param(["0.2.0"], lambda repo: repo.notes.write_text(""), "notes", id="empty notes"),
    pytest.param(["0.2.0"], _tagged, "already tagged", id="existing tag"),
    pytest.param(["0.2.0"], lambda repo: (repo.root / "logged-out").touch(), "gh auth login",
                 id="gh logged out"),
    pytest.param(["0.0.9"], lambda repo: None, "not newer", id="older version"),
    pytest.param(["0.9.0"], lambda repo: _origin_moves_on(repo, "0.10.0"), "not newer",
                 id="older than origin's main"),
    pytest.param(["0.2"], lambda repo: None, "1.2.3", id="not X.Y.Z"),
    pytest.param(["0.2.0", "feat/typo"], lambda repo: _origin_moves_on(repo, "0.1.0"),
                 "no branch feat/typo", id="missing branch, main behind origin"),
])
def test_a_refusal_is_one_line_and_changes_nothing(repo, argv, setup, says):
    setup(repo)
    before = repo.state()

    refused = repo.release(argv[0], str(repo.notes), *argv[1:])

    assert refused.returncode != 0
    assert len(refused.stderr.strip().splitlines()) == 1, refused.stderr
    assert says in refused.stderr
    assert repo.state() == before
    assert repo.locked() == [] and repo.published() == []


def test_run_from_a_worktree_it_releases_from_the_main_checkout(repo):
    worktree = repo.root / "trees" / "wt"
    repo.git("worktree", "add", "-q", "-b", "elsewhere", str(worktree))
    untouched = repo.git("rev-parse", "HEAD", cwd=worktree)
    script = worktree / "scripts" / "release"
    script.parent.mkdir()
    shutil.copy2(SCRIPT, script)

    done = repo.release("0.2.0", "../../notes.md", cwd=worktree, script=script)

    assert done.returncode == 0, done.stderr
    assert repo.git("rev-parse", "HEAD") == repo.origin_main()
    assert repo.git("rev-parse", "HEAD", cwd=worktree) == untouched
    assert [Path(c["cwd"]).resolve() for c in repo.locked()] == [repo.clone.resolve()]
    [published] = repo.published()
    assert Path(published[-1]).resolve() == repo.notes.resolve()


def test_run_from_outside_the_repository_it_still_releases(repo):
    done = repo.release("0.2.0", str(repo.notes), cwd=repo.root)

    assert done.returncode == 0, done.stderr
    assert repo.git("rev-parse", "HEAD") == repo.origin_main()
    assert len(repo.published()) == 1
