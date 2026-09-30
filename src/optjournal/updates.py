"""New versions from GitHub Releases: whether there is one, and fetching it.

FOR A DOWNLOADED ZIP, which is how this app reaches people who do not use a
terminal. A ZIP has no `.git`, so `optjournal update` (a fast-forward) cannot
serve them; a git clone is refused here and keeps using that command.

RELEASES, NOT WHATEVER IS ON MAIN, so the person publishing decides when
friends get a version: bump `version` in `pyproject.toml`, then publish a
GitHub release tagged `v<version>`. `latest_release` reads GitHub's "latest",
which already skips drafts and pre-releases.

TWO HALVES, IN TWO PROCESSES. This module downloads and checks a release and
unpacks it into `STAGING` beside the code, then the server exits asking for a
restart. The launcher (`launcher/app.py`) swaps the staged files in while
nothing is running, and starts the server again. Swapping from inside the
server would replace code under a running process, and on Windows a running
process locks its files.

NOTHING OF THE JOURNAL IS IN A RELEASE: a release is `git archive` of tracked
files, and every data file is gitignored. `stage` still refuses an archive that
names any of `config.DATA_NAMES`, so a mistaken commit cannot become a release
that overwrites someone's journal.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen

from optjournal.config import DATA_NAMES, ROOT

__all__ = [
    "READY",
    "STAGING",
    "Release",
    "UpdateRefused",
    "check",
    "current_version",
    "latest_release",
    "stage",
]

log = logging.getLogger(__name__)

REPO_ENV = "OPTJOURNAL_UPDATE_REPO"
DEFAULT_REPO = "robert-mp/optjournal"

#: Where a verified release waits for the launcher. Read by `launcher/app.py`,
#: which cannot import this module (see its docstring); `tests/test_updates.py`
#: pins the two copies of these names together.
STAGING = ".update-staging"
#: Written LAST into `STAGING`: a staging folder without it is a download that
#: was interrupted, and the launcher ignores it.
READY = ".ready"

#: A release archive is a few hundred KB. Anything past this is not one.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
TIMEOUT_S = 15
#: How long a check is trusted. GitHub allows 60 unauthenticated API calls an
#: hour per address, and a release is not something anyone needs within minutes.
CHECK_EVERY_S = 6 * 3600


class UpdateRefused(RuntimeError):
    """This install cannot, or must not, be updated from a release."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    zip_url: str
    page_url: str


def _version_key(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.strip().lstrip("v").split("."))
    except ValueError:
        return None


def current_version(root: Path = ROOT) -> str:
    """The version of the code on disk, from `pyproject.toml`.

    Read from the file rather than from package metadata, because the metadata
    is written when `uv` installs the project and lags a swap until the next
    start; the file is what the swap replaced.
    """
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def _get(url: str, *, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise UpdateRefused(f"refusing a non-HTTPS download: {url}")
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "optjournal-updater",
    })
    with urlopen(request, timeout=TIMEOUT_S) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise UpdateRefused(f"{url} sent more than {limit} bytes")
    return body


def latest_release(repo: str | None = None) -> Release:
    """The newest published release. Raises `URLError`/`UpdateRefused` on failure."""
    repo = repo or os.environ.get(REPO_ENV) or DEFAULT_REPO
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest",
                           limit=1024 * 1024))
    tag = str(data.get("tag_name") or "")
    if _version_key(tag) is None:
        raise UpdateRefused(f"release tag {tag!r} is not a version like v1.2.3")
    return Release(
        version=tag.lstrip("v"),
        notes=str(data.get("body") or ""),
        zip_url=str(data["zipball_url"]),
        page_url=str(data.get("html_url") or ""),
    )


def cannot_apply(root: Path = ROOT) -> str | None:
    """Why this install cannot update itself from a release, or None."""
    if (root / ".git").exists():
        return "this is a git clone: run `optjournal update` instead"
    if not os.environ.get("OPTJOURNAL_SUPERVISED"):
        return "start optjournal with its Start file to update from the page"
    return None


#: The last check, and when it ran: one per process, like the server itself.
_last: tuple[float, dict[str, object]] | None = None


def check(*, root: Path = ROOT, force: bool = False) -> dict[str, object]:
    """What the page's banner needs. Never raises: offline is a normal state."""
    global _last
    now = time.monotonic()
    if not force and _last and now - _last[0] < CHECK_EVERY_S:
        return dict(_last[1])
    current = current_version(root)
    reply: dict[str, object] = {"current": current, "available": False,
                                "cannot_apply": cannot_apply(root)}
    try:
        release = latest_release()
    except (URLError, OSError, UpdateRefused, ValueError, KeyError) as exc:
        reply["error"] = f"could not check for updates: {exc}"
    else:
        newer = (_version_key(release.version) or ()) > (_version_key(current) or ())
        reply.update(latest=release.version, available=newer,
                     notes=release.notes, url=release.page_url)
    _last = (now, reply)
    return dict(reply)


def _members(archive: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """The archive's single top folder, and its members. Refuses anything else.

    GitHub's zipball holds one folder, `<owner>-<repo>-<sha>/`. Every member must
    sit inside it with no absolute path and no `..`, or extracting could write
    outside the staging folder.
    """
    infos = archive.infolist()
    tops = {PurePosixPath(i.filename).parts[0] for i in infos if i.filename}
    if len(tops) != 1:
        raise UpdateRefused(f"expected one top folder, found {sorted(tops)}")
    (top,) = tops
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise UpdateRefused(f"unsafe path in the release: {info.filename!r}")
    return top, infos


def stage(release: Release, *, root: Path = ROOT) -> Path:
    """Download, verify and unpack `release` into `root/STAGING`. Returns it.

    Verifies that the archive is optjournal at the version the release claims,
    and that it names no journal data. Nothing outside `STAGING` is written.
    """
    reason = cannot_apply(root)
    if reason:
        raise UpdateRefused(reason)
    body = _get(release.zip_url, limit=MAX_DOWNLOAD_BYTES)
    if not zipfile.is_zipfile(io.BytesIO(body)):
        raise UpdateRefused("the download is not a release archive (an error page?)")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        top, infos = _members(archive)
        meta = tomllib.loads(archive.read(f"{top}/pyproject.toml").decode())
        project = meta.get("project", {})
        if project.get("name") != "optjournal":
            raise UpdateRefused("the release is not optjournal")
        if project.get("version") != release.version:
            raise UpdateRefused(f"release v{release.version} holds version "
                                f"{project.get('version')}")
        entries = {PurePosixPath(i.filename).parts[1]
                   for i in infos if len(PurePosixPath(i.filename).parts) > 1}
        clash = sorted(entries & {*DATA_NAMES, STAGING})
        if clash:
            raise UpdateRefused(f"the release names journal data: {clash}")

        staging = root / STAGING
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        for info in infos:
            relative = PurePosixPath(info.filename).relative_to(top)
            if not relative.parts:
                continue
            target = staging.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            # The unix mode rides in the high bits. Without it the macOS Start
            # file loses its executable bit and stops opening on double-click.
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                target.chmod(mode)
    (staging / READY).write_text(release.version)
    log.info("staged v%s in %s", release.version, staging)
    return staging
