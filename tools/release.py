"""Build a release into dist/: barrens-voice-<version>.zip and manifest.json (`mise run build`).

The archive is what bv/install.py puts into <install>\\app\\<version>: companion/ (the code, without the
tests), addon/BarrensVoice, pyproject.toml, uv.lock, LICENSE, news.toml (the companion writes the addon's «Что
нового» from it), and Barrens Voice.exe when built (on Windows). The manifest tells the launcher's updater
what is there: the app's version, the strip protocol, the addon's version, the archive's name, size
and SHA-256 (`mise run sign` adds a signature). A release (not a test build) needs the addon at its release version
and a dated «Что нового» entry of its own version in news.toml - the program's, so a release of the program alone
has one too. dist/notes.md, the GitHub release's description, is made from that entry (`notes`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "companion"))
from bv import news  # noqa: E402 - news.toml's reading and checks, the companion's own

REPO_URL = "https://github.com/" + os.environ.get("BV_RELEASE_REPO", "rgeraskin/barrens-voice")
TOC = "addon/BarrensVoice/BarrensVoice.toc"
INSTALL = "Установка: скачай `BarrensVoice-Setup.exe` и запусти его."
# the uv the installer ships and the updates bring (the manifest names it: an install with another fetches it)
UV_VERSION = "0.13.7"
UV_ZIP = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"


def uv_entry() -> dict:
    """The uv of this release for the manifest: version, where its Windows build is, its published SHA-256."""
    import urllib.request
    request = urllib.request.Request(UV_ZIP + ".sha256", headers={"User-Agent": "barrens-voice-build"})
    with urllib.request.urlopen(request, timeout=60) as r:  # noqa: S310
        sha = r.read().decode().split()[0]
    return {"version": UV_VERSION, "url": UV_ZIP, "sha256": sha}
# only tracked files are packed (files()): what is left out of those - the tests and the source-run setup
SKIP_DIRS = {"tests"}
SKIP_FILES = {"requirements.txt", "BarrensVoice.cmd"}


def files() -> list[tuple[Path, str]]:
    """(source, name in the archive): only what git tracks, so nothing made on this machine slips in."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "-z", "companion", "addon/BarrensVoice", "pyproject.toml", "uv.lock",
                              "LICENSE", news.SOURCE], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    out = []
    for name in filter(None, tracked):
        parts = Path(name).parts
        if SKIP_DIRS & set(parts) or parts[-1] in SKIP_FILES:
            continue
        out.append((ROOT / name, name))
    return out


def versions() -> dict:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    toc = (ROOT / TOC).read_text(encoding="utf-8")
    proto = re.search(r"^VERSION = (\d+)", (ROOT / "companion" / "bv" / "protocol.py").read_text(encoding="utf-8"), re.M)
    return {"version": project["version"], "addon": re.search(r"^## Version:\s*(\S+)", toc, re.M).group(1),
            "protocol": int(proto.group(1))}


def news_entries() -> list[dict]:
    """The players' «Что нового» of every release: news.toml at the root (bv/news.py), newest first."""
    return news.load(ROOT / news.SOURCE)


def released_tags() -> set[str] | None:
    """The tags that have a GitHub release (a tag whose build failed has none), or None when GitHub does not answer.
    The repository is public: no token needed, one is used when the environment has it (CI's rate limit)."""
    import urllib.request
    from bv.update import AGENT, REPO
    repo = os.environ.get("BV_RELEASE_REPO", REPO)
    RELEASES = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    headers = {"User-Agent": AGENT}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(RELEASES, headers=headers), timeout=15) as response:  # noqa: S310
            data = json.loads(response.read())
    except Exception:  # noqa: BLE001 - offline, rate-limited: the nearest tag will do
        return None
    return {r["tag_name"] for r in data if isinstance(r, dict) and isinstance(r.get("tag_name"), str)}


# a commit that touches only these is not listed: nothing a tester runs or sees - the docs (verified.md, to-test,
# bugs.md, README, CHANGELOG), the tests, CI and the hooks, the release tools, notes for the developers
NOT_LISTED = tuple(f":(exclude){path}" for path in (
    "docs", "companion/tests", ".github", ".githooks", "tools", ".claude",
    "README.md", "CHANGELOG.md", "CLAUDE.md", "DEVELOPMENT.md", ".mise.toml", ".gitignore", ".gitattributes",
    ".pkgmeta"))
# the addon's test-build version raised («Addon 0.4.0b11: the addon changes after beta 24 (0.4.0b10)»): the TOC's
# number alone, the changes it covers are listed by their own commits
VERSION_RAISE = re.compile(r"Addon \d+\.\d+\.\d+b\d+: the addon changes after ")


def _git(root: Path, *args: str) -> str:
    """git in `root`; another repository than this one (a test's) without a calling git's GIT_* (a hook sets them)."""
    import subprocess
    env = None if Path(root) == ROOT else {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          env=env).stdout.strip()


def build_subjects(tag: str, released: set[str] | None = None, root: Path = ROOT) -> list[str]:
    """The subjects of the commits a test build brings, oldest first, up to the tag (HEAD when the tag is not there
    yet: a local build): since the last tag that has a release (`released`, released_tags) - a tag whose build failed
    reached no tester, so its commits are listed with the next - or the nearest tag when that is not known. No
    merges, none that touch only what no tester runs (NOT_LISTED), no raise of the addon's version (VERSION_RAISE)."""
    def git(*args: str) -> str:
        return _git(root, *args)
    ref = tag if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}") else "HEAD"
    nearest = git("describe", "--tags", "--abbrev=0", "--match", "v*", f"{ref}^")
    previous, seen = nearest, nearest
    while released is not None and seen and seen not in released:      # back past the tags without a release
        seen = git("describe", "--tags", "--abbrev=0", "--match", "v*", f"{seen}^")
        previous = seen or nearest                                     # none released before it: the nearest
    span = f"{previous}..{ref}" if previous else ref
    out = git("log", "--no-merges", "--reverse", "--format=%s", span, "--", ".", *NOT_LISTED)
    return [s for s in out.splitlines() if s.strip() and not VERSION_RAISE.match(s)]


def build_history(tag: str, released: set[str] | None = None, root: Path = ROOT) -> list[tuple[str, list[str]]]:
    """(version, build_subjects) of every test build of the tag's release up to it, oldest first: a tester who skipped
    a build (17 -> 19) sees what the one between brought too. Only those that reached testers - a tag with a release
    (`released`); a tag whose build failed has none, and its commits are listed with the next. Every tag of the
    release's test builds when GitHub does not answer (None)."""
    m = TAG.fullmatch(tag)
    if not m or not m.group(2):
        return []
    base, number = m.group(1), int(m.group(2))
    ref = tag if _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}") else "HEAD"
    earlier = []
    for name in _git(root, "tag", "--list", f"v{base}-beta.*", "--merged", ref).split():
        other = TAG.fullmatch(name)
        if other and other.group(2) and int(other.group(2)) < number and (released is None or name in released):
            earlier.append(int(other.group(2)))
    return [(f"{base}b{n}", build_subjects(f"v{base}-beta.{n}", released, root)) for n in sorted(earlier)] \
        + [(f"{base}b{number}", build_subjects(tag, released, root))]


# what the game's font has not, in a commit's subject (news.FORBIDDEN): what it has instead; «|» is its markup
PLAIN = {"→": "->", "–": "—", "|": "/"}


def build_entry(version: str, subjects: list[str]) -> dict:
    """A test build's own «Что нового»: the list of its commits, as they are (English), in news.toml's shape - made
    at the build, not kept in news.toml (a release folds what players need into its own entry; test builds' entries
    never reach a release: news_problem). `kind` "big": the window opens for testers."""
    number = re.search(r"b(\d+)$", version).group(1)
    items = [re.sub("|".join(map(re.escape, PLAIN)), lambda m: PLAIN[m.group()], s) for s in subjects]
    # control characters (a DEL, say) TOML's strings may not hold: news.toml would not read, no news at all
    items = [re.sub(r"[\x00-\x1f\x7f]", "", item) for item in items]
    return {"version": version, "kind": "big", "title": f"Тестовая сборка {number}",
            "summary": f"тестовая сборка {number}: что в неё вошло",
            "items": items or ["Новых коммитов нет: та же сборка заново."]}


def with_build_entries(version: str, entries: list[dict], builds: list[tuple[str, list[str]]]) -> list[dict]:
    """news.toml's entries with the test builds' (build_entry: this build's and its release's earlier ones,
    build_history) where they are (newest first: a release before its test builds); as they are for a release. A
    test build whose entry news.toml has (written by hand) keeps that one."""
    if version == news.release_of(version):
        return entries
    have = {e["version"] for e in entries}
    made = [build_entry(v, subjects) for v, subjects in builds if v not in have]
    if not made:
        return entries
    return sorted(entries + made, key=lambda e: news.version_key(e["version"]), reverse=True)


def notes(version: str, entries: list[dict], tag: str = "") -> str:
    """The GitHub release's description, from news.toml: the release's entry (its title and items, in Russian), then
    a link to the full English list (CHANGELOG.md at the release's tag) and how to install. A test build's starts
    with the release it leads to, then the build's own entry when it has one (what this build brought), then that
    release's entry (a tagged build has one: news_problem)."""
    release = news.release_of(version)
    own = [e for e in entries if e["version"] == version] if version != release else []
    shown = own + [e for e in entries if e["version"] == release]
    parts = []
    if version != release:
        parts.append(f"Тестовая сборка выпуска {release}.")
    for entry in shown:
        parts.append(f"### {entry['title']}\n\n" + "\n".join(f"- {item}" for item in entry["items"]))
    parts.append(f"Полный список изменений, на английском: [CHANGELOG.md]({REPO_URL}/blob/{tag or 'master'}/CHANGELOG.md).")
    parts.append(INSTALL)
    return "\n\n".join(parts) + "\n"


def dictionary_revision() -> int:
    """The time of the last commit that changed Dictionary.lua - tools/dictionary_json.py's revision, so the
    online dictionary and the release's agree - or 0 outside git."""
    import subprocess

    from dictionary_json import revision
    try:
        return revision()
    except (OSError, subprocess.CalledProcessError, ValueError, SystemExit):
        return 0


DICTIONARY = "addon/BarrensVoice/Dictionary.lua"
# the CurseForge and Wago projects the TOC names (their apps match an addon installed elsewhere by them): the sites
# have releases only, so a test build's addon goes without - an app would take it for the project's and «update»
# it to the older release, and the launcher would put the test build back
SITE_IDS = re.compile(rb"(?m)^## X-(?:Curse-Project-ID|Wago-ID):[^\n]*\n")


def without_site_ids(toc: bytes) -> bytes:
    """A test build's TOC: the sites' project ids taken out."""
    return SITE_IDS.sub(b"", toc)


def toc_for(version: str, toc: bytes) -> bytes:
    """The archive's TOC: a release's as it is (the apps match it by the ids), a test build's without them."""
    return toc if version == news.release_of(version) else without_site_ids(toc)



def stamp_dictionary(data: bytes, revision: int) -> bytes:
    """Dictionary.lua with its D.REVISION: which default dictionary this is, against the dictionary online's (the
    repository's says 0). Every build of the addon stamps it: this archive's, and CurseForge's and Wago's zip
    (tools/addon_sites.py)."""
    return re.sub(rb"(?m)^D\.REVISION = \d+", f"D.REVISION = {revision}".encode(), data, count=1)


TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-beta\.(\d+))?")


def tag_version(tag: str, base: str) -> str | None:
    """The release's version from its tag: "v0.4.0" -> "0.4.0", "v0.4.0-beta.5" -> "0.4.0b5" (a test build,
    older than 0.4.0 for the updater); None when the tag is of another form or names another version than
    pyproject.toml's `base`."""
    m = TAG.fullmatch(tag)
    if not m or m.group(1) != base:
        return None
    return f"{base}b{m.group(2)}" if m.group(2) else base


def version_of(tag: str, base: str) -> str:
    """tag_version, or the build stops: the tag must name pyproject.toml's version."""
    version = tag_version(tag, base)
    if version is None:
        sys.exit(f"the tag {tag} and pyproject.toml's version {base} differ (tags: vX.Y.Z or vX.Y.Z-beta.N)")
    return version


def news_problem(version: str, entries: list[dict]) -> str | None:
    """Why a build cannot go out for its «Что нового» (DEVELOPMENT.md), or None: news.toml must be well formed and
    have the release's entry - dated for a release, while a test build's release may still be in work. The entry is
    the program's version's: a release of the program alone tells the players what it brought too. A release keeps
    only release entries: what its test builds' own entries said is folded into its entry by then."""
    found = news.problems(entries)
    if found:
        return "news.toml: " + "; ".join(found)
    release = news.release_of(version)
    entry = next((e for e in entries if e["version"] == release), None)
    if entry is None:
        return f"news.toml has no «Что нового» entry for {release}: add an [[entry]] with version = \"{release}\""
    if version != release:
        return None
    if "date" not in entry:
        return f"the «Что нового» entry of {release} has no date: add date = \"DD.MM.YYYY\" to it in news.toml"
    leftovers = [e["version"] for e in entries if e["version"] != news.release_of(e["version"])]
    if leftovers:
        return (f"news.toml still has test builds' entries ({', '.join(leftovers)}): fold what players need of them "
                f"into the {release} entry and remove them - a release keeps only release entries")
    return None


# a test build's version where CHANGELOG.md names one: "0.4.0b3", "0.4.0-beta.3", "0.4.0 beta 3"
BETA_IN_CHANGELOG = r"\d+\.\d+\.\d+(?:b\d+|[-. ]?beta[-. ]?\d*)"


def changelog_problem(version: str, text: str) -> str | None:
    """Why a build cannot go out for its CHANGELOG.md, or None: it has the release's section (Keep a Changelog,
    CLAUDE.md) - for a test build «Unreleased» will do; a release's is dated (the GitHub release's publication day,
    UTC) and has its link line at the bottom pointing at its tag, not at HEAD. Test builds never get sections or link
    lines of their own (their changes go into the release's section): a release refuses any."""
    release = news.release_of(version)
    heading = re.search(rf"^## \[{re.escape(release)}\] - (\d{{4}}-\d\d-\d\d|Unreleased)$", text, re.M)
    if heading is None:
        return f"CHANGELOG.md has no section «## [{release}] - YYYY-MM-DD» (or «- Unreleased» while in work)"
    if version != release:
        return None
    date = heading.group(1)
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return (f"CHANGELOG.md's section [{release}] is «{date}»: give it the release's date, "
                f"«## [{release}] - YYYY-MM-DD» (the GitHub release's publication day, UTC)")
    if not re.search(rf"^\[{re.escape(release)}\]: https://github\.com/\S*v{re.escape(release)}$", text, re.M):
        return (f"CHANGELOG.md has no link line for {release} at the bottom pointing at its tag: "
                f"«[{release}]: {REPO_URL}/compare/v<previous>...v{release}»")
    betas = re.findall(rf"^(?:## \[({BETA_IN_CHANGELOG})\]|\[({BETA_IN_CHANGELOG})\]:)", text, re.M)
    if betas:
        named = sorted({a or b for a, b in betas})
        return (f"CHANGELOG.md has sections or link lines of test builds ({', '.join(named)}): test builds have none, "
                f"their changes go into the [{release}] section")
    return None


COMPAT = "companion/tests/compat"     # frozen copies of the released clients' update.py and install.py


def compat_problem(tag: str, version: str, updater: str | None, copies: list[str]) -> str | None:
    """Why a tag's update code has no frozen copy, or None: every release's update.py and install.py stay under
    COMPAT, so that later releases are tested to install through the clients players have (OldClients). `updater`:
    the tag's update.py + "\0" + install.py (None: no updater yet), `copies`: the same of each copy under COMPAT."""
    if updater is None or updater in copies:
        return None
    folder = "v" + version.replace(".", "_")
    return (f"{tag} ships an update.py/install.py no frozen copy has: copy them to {COMPAT}/{folder}/ (with an "
            "__init__.py) and add an OldClients test in test_launcher.py")


def release_problems(tag: str, base: str, addon: str, entries: list[dict] | None, changelog: str | None,
                     updater: str | None = None, copies: list[str] = ()) -> list[str]:
    """Why `tag` cannot be released, in words ([] when it can): what the build refuses in CI, and the pre-push hook
    before the tag is public (tools/check_tag.py) - one list, so the two never disagree. `base`: pyproject.toml's
    version, `addon`: the .toc's, `entries`: news.toml's (None: no such file), `changelog`: CHANGELOG.md (None: no
    such file) - all as the tagged commit has them."""
    version = tag_version(tag, base)
    if version is None:
        return [f"the tag {tag} and pyproject.toml's version {base} differ (tags: vX.Y.Z or vX.Y.Z-beta.N)"]
    out = []
    if version == base and "b" in addon:
        out.append(f"the release {tag} would carry a test build of the addon ({addon}): give the addon its "
                   "release version in BarrensVoice.toc")
    out.append(news_problem(version, entries) if entries is not None else "news.toml is missing")
    out.append(changelog_problem(version, changelog) if changelog is not None else "CHANGELOG.md is missing")
    out.append(compat_problem(tag, version, updater, list(copies)))
    return [p for p in out if p]


def updater_pair(read, folder: str) -> str | None:
    """update.py + "\0" + install.py in `folder` (read(name) gives a file's text), None without both."""
    update, install = read(f"{folder}/update.py"), read(f"{folder}/install.py")
    return None if update is None or install is None else update + "\0" + install


def tag_problems(tag: str, read, listing=None) -> list[str]:
    """release_problems for a tag from the files at its commit: `read(name)` gives a file's text, None when the commit
    has no such file; `listing(folder)` the names in a folder at the commit (no compat check without it)."""
    pyproject, toc, toml = read("pyproject.toml"), read(TOC), read(news.SOURCE)
    try:
        base = tomllib.loads(pyproject or "")["project"]["version"]
    except (ValueError, KeyError, TypeError):
        return [f"{tag}: pyproject.toml is missing or has no [project] version"]
    addon = re.search(r"^## Version:\s*(\S+)", toc or "", re.M)
    entries = None
    if toml is not None:
        try:
            entries = news.parse(toml)
        except (ValueError, TypeError) as e:
            return [f"news.toml cannot be read: {e}"]
    updater, copies = None, []
    if listing is not None:
        updater = updater_pair(read, "companion/bv")
        copies = [c for c in (updater_pair(read, f"{COMPAT}/{name}") for name in listing(COMPAT)) if c is not None]
    return release_problems(tag, base, addon.group(1) if addon else "?", entries, read("CHANGELOG.md"), updater, copies)


def update_channel(data: bytes) -> bytes:
    """Set the update origin and trusted public key in packaged forks."""
    import base64
    repo = os.environ.get("BV_RELEASE_REPO", "rgeraskin/barrens-voice")
    if repo == "rgeraskin/barrens-voice":
        return data
    public = os.environ.get("BV_UPDATE_PUBLIC_KEY", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or len(base64.b64decode(public, validate=True)) != 32:
        raise ValueError("Set BV_UPDATE_PUBLIC_KEY to the fork's Ed25519 public key")
    for name, value in (("REPO", repo), ("PUBLIC_KEY", public)):
        data, count = re.subn(fr'(?m)^{name} = "[^"]*"(?=\r?$)'.encode(), f'{name} = "{value}"'.encode(), data)
        if count != 1:
            raise ValueError(f"Expected one {name} assignment in packaged update.py")
    return data


def build() -> Path:
    info = versions()
    base = info["version"]
    tag = os.environ.get("RELEASE_TAG", "")
    entries = news_entries()
    if tag:
        def disk(name: str) -> str | None:
            path = ROOT / name
            return path.read_text(encoding="utf-8") if path.is_file() else None
        problems = release_problems(tag, base, info["addon"], entries,
                                    (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                                    updater_pair(disk, "companion/bv"),
                                    [c for c in (updater_pair(disk, f"{COMPAT}/{d.name}")
                                                 for d in sorted((ROOT / COMPAT).glob("v*"))) if c is not None])
        if problems:
            sys.exit("\n".join(problems))
        info["version"] = version_of(tag, base)
        if info["version"] != news.release_of(info["version"]):          # a test build: its entries (GitHub asked)
            entries = with_build_entries(info["version"], entries, build_history(tag, released_tags()))
        problems = news.problems(entries)
        if problems:
            sys.exit("the test build's «Что нового»: " + "; ".join(problems))
    own_news = news.dumps(entries) if any(e["version"] == info["version"] != news.release_of(info["version"])
                                         for e in entries) else None
    if own_news is not None:                                               # what the companion will read: the same
        try:
            same = news.parse(own_news) == entries
        except (ValueError, TypeError) as e:
            same = e
        if same is not True:
            sys.exit(f"news.toml of the test build does not read back as written: {same}")
    DIST.mkdir(exist_ok=True)
    archive = DIST / f"barrens-voice-{info['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        extra = [(DIST / "Barrens Voice.exe", "Barrens Voice.exe")] if (DIST / "Barrens Voice.exe").is_file() else []
        for source, name in sorted(files() + extra, key=lambda f: f[1]):   # the starter: built first on Windows
            info_ = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))   # a fixed time, not the checkout's
            info_.compress_type = zipfile.ZIP_DEFLATED
            data = source.read_bytes()
            if name == "pyproject.toml" and info["version"] != base:     # the build's own version, beta and all
                data = re.sub(rb'(?m)^version = "[^"]*"', f'version = "{info["version"]}"'.encode(), data, count=1)
            if name == news.SOURCE and own_news is not None:               # with the test build's own entry
                data = own_news.encode("utf-8")
            if name == "companion/bv/update.py":
                data = update_channel(data)
            if name == DICTIONARY:                                         # which default this is (the dictionary online)
                data = stamp_dictionary(data, dictionary_revision())
            if name == TOC:                                                # a test build's: not the sites'
                data = toc_for(info["version"], data)
            z.writestr(info_, data)
    data = archive.read_bytes()
    manifest = {**info, "archive": archive.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "uv": uv_entry()}
    (DIST / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (DIST / "notes.md").write_text(notes(info["version"], entries, tag), encoding="utf-8")
    print(f"{archive.name}: {len(data) / 1e6:.1f} MB; manifest: {manifest}")
    return archive


if __name__ == "__main__":
    build()
    sys.exit(0)
