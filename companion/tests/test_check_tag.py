"""A release tag ready to push (tools/check_tag.py, .githooks/pre-push): its version, news.toml, CHANGELOG.md -
release.release_problems, the same list CI's build refuses by."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # gitenv, however the tests are started
import release  # noqa: E402

SH = shutil.which("sh")
HOOK = ROOT / ".githooks" / "pre-push"


def news_toml(version: str = "0.4.1", date: str | None = "25.11.2026") -> str:
    dated = f'date = "{date}"\n' if date else ""
    return (f'[[entry]]\nversion = "{version}"\n{dated}kind = "small"\ntitle = "Числа"\n'
            'summary = "числа читаются точнее"\nitems = ["Пункт."]\n')


def changelog(version: str = "0.4.1", date: str = "2026-11-25", link: str | None = None) -> str:
    link = link if link is not None else f"compare/v0.4.0...v{version}"
    return (f"# Changelog\n\n## [{version}] - {date}\n\n### Fixed\n\n- Numbers\n\n  Read right.\n\n"
            f"## [0.4.0] - 2026-11-20\n\n### Added\n\n- All\n\n"
            f"[{version}]: https://github.com/rgeraskin/barrens-voice/{link}\n"
            "[0.4.0]: https://github.com/rgeraskin/barrens-voice/compare/v0.3.0...v0.4.0\n")


def problems(tag="v0.4.1", base="0.4.1", addon="0.4.1", toml=None, log=None) -> list[str]:
    from bv import news
    entries = news.parse(news_toml() if toml is None else toml)
    return release.release_problems(tag, base, addon, entries, changelog() if log is None else log)



class Compat(unittest.TestCase):
    def test_a_tag_needs_its_update_codes_frozen_copy(self):
        # players' clients of every release must stay testable (OldClients): a tag whose update.py/install.py no copy
        # under companion/tests/compat has is refused before it is public, as CI's test would refuse it after
        from release import compat_problem
        self.assertIsNone(compat_problem("v0.4.1", "0.4.1", None, []))                  # no updater yet
        self.assertIsNone(compat_problem("v0.4.1", "0.4.1", "u\0i", ["x\0y", "u\0i"]))   # a copy has it
        said = compat_problem("v0.4.1-beta.2", "0.4.1b2", "u\0i", ["x\0y"])
        self.assertIn("companion/tests/compat/v0_4_1b2/", said)
        self.assertIn("OldClients", said)

class Checks(unittest.TestCase):
    def test_a_ready_release(self):
        self.assertEqual(problems(), [])
        self.assertEqual(problems(addon="0.4.0"), [])                        # the program alone: the addon kept
        self.assertEqual(problems(log=changelog(link="releases/tag/v0.4.1")), [])   # the first release's form

    def test_its_news(self):
        self.assertIn("no «Что нового» entry for 0.4.1", "".join(problems(toml=news_toml("0.4.0"))))
        self.assertIn("entry of 0.4.1 has no date", "".join(problems(toml=news_toml(date=None))))
        self.assertIn("news.toml: ", "".join(problems(toml=news_toml().replace('"small"', '"huge"'))))

    def test_no_test_builds_news_left_in_a_release(self):
        # their entries are folded into the release's by then - an older release's test build's too
        leftover = news_toml() + "\n" + news_toml("0.4.1b2", date=None)
        found = "".join(problems(toml=leftover))
        self.assertIn("still has test builds' entries (0.4.1b2)", found)
        self.assertIn("fold what players need of them into the 0.4.1 entry", found)
        older = news_toml() + "\n" + news_toml("0.4.0", "20.11.2026") + "\n" + news_toml("0.4.0b3", "18.11.2026")
        self.assertIn("(0.4.0b3)", "".join(problems(toml=older)))
        # while in work, a test build may carry them
        beta = dict(tag="v0.4.1-beta.3", log=changelog(date="Unreleased", link="compare/v0.4.0...HEAD"))
        self.assertEqual(problems(**beta, toml=news_toml(date=None) + "\n" + news_toml("0.4.1b2", date=None)), [])

    def test_no_test_builds_in_a_releases_changelog(self):
        # betas have no sections or link lines of their own: their changes go into the release's section
        log = changelog()
        section = log.replace("## [0.4.0] - 2026-11-20", "## [0.4.1b2] - 2026-11-22\n\n### Fixed\n\n- x\n\n"
                                                         "## [0.4.0] - 2026-11-20")
        self.assertIn("test builds (0.4.1b2)", "".join(problems(log=section)))
        link = log + "[0.4.0-beta.3]: https://github.com/rgeraskin/barrens-voice/compare/v0.4.0-beta.2...v0.4.0-beta.3\n"
        self.assertIn("test builds (0.4.0-beta.3)", "".join(problems(log=link)))
        spaced = log.replace("## [0.4.0] - 2026-11-20", "## [0.4.1 beta 2] - 2026-11-22\n\n## [0.4.0] - 2026-11-20")
        self.assertIn("(0.4.1 beta 2)", "".join(problems(log=spaced)))
        self.assertEqual(problems(tag="v0.4.1-beta.3", log=section), [])           # a test build is not held to it

    def test_its_changelog(self):
        self.assertIn("no section «## [0.4.1]", "".join(problems(log=changelog("0.4.0"))))
        self.assertIn("is «Unreleased»", "".join(problems(log=changelog(date="Unreleased"))))
        self.assertIn("is «2026-02-30»", "".join(problems(log=changelog(date="2026-02-30"))))
        self.assertIn("no link line for 0.4.1", "".join(problems(log=changelog(link="compare/v0.4.0...HEAD"))))
        no_link = "\n".join(l for l in changelog().splitlines() if not l.startswith("[0.4.1]:"))
        self.assertIn("no link line for 0.4.1", "".join(problems(log=no_link)))

    def test_a_test_build(self):
        # the release's entry and section are there, undated and unreleased will do; a test build of the addon too
        beta = dict(tag="v0.4.1-beta.3", addon="0.4.1b2")
        self.assertEqual(problems(**beta, toml=news_toml(date=None),
                                  log=changelog(date="Unreleased", link="compare/v0.4.0...HEAD")), [])
        self.assertIn("no «Что нового» entry for 0.4.1", "".join(problems(**beta, toml=news_toml("0.4.0"))))
        self.assertIn("no section «## [0.4.1]", "".join(problems(**beta, log=changelog("0.4.0"))))

    def test_its_version(self):
        for tag in ("v0.4.2", "v0.4.1-rc1", "0.4.1", "v0.4.1-beta"):
            self.assertEqual(problems(tag=tag), [f"the tag {tag} and pyproject.toml's version 0.4.1 differ "
                                                 "(tags: vX.Y.Z or vX.Y.Z-beta.N)"])
        self.assertIn("test build of the addon (0.4.1b2)", "".join(problems(addon="0.4.1b2")))

    def test_from_the_tagged_files(self):
        files = {"pyproject.toml": '[project]\nname = "x"\nversion = "0.4.1"\n', "news.toml": news_toml(),
                 "CHANGELOG.md": changelog(), release.TOC: "## Interface: 16001\n## Version: 0.4.1\n"}
        self.assertEqual(release.tag_problems("v0.4.1", files.get), [])
        self.assertEqual(release.tag_problems("v0.4.1", {**files, "news.toml": None}.get), ["news.toml is missing"])
        self.assertEqual(release.tag_problems("v0.4.1", {**files, "CHANGELOG.md": None}.get), ["CHANGELOG.md is missing"])
        self.assertIn("news.toml cannot be read", release.tag_problems("v0.4.1", {**files, "news.toml": "[[entry]"}.get)[0])
        self.assertIn("pyproject.toml is missing", release.tag_problems("v0.4.1", {**files, "pyproject.toml": None}.get)[0])
        with_beta_addon = {**files, release.TOC: "## Version: 0.4.1b1\n"}
        self.assertIn("test build of the addon", release.tag_problems("v0.4.1", with_beta_addon.get)[0])

    def test_the_build_refuses_by_the_same_list(self):
        # CI: tools/release.py stops a tagged build on any of them
        from unittest import mock
        with mock.patch.object(release, "versions", return_value={"version": "0.4.1", "addon": "0.4.1", "protocol": 1}), \
                mock.patch.object(release, "release_problems", return_value=["one", "two"]) as checked, \
                mock.patch.dict(os.environ, {"RELEASE_TAG": "v0.4.1"}), self.assertRaises(SystemExit) as stop:
            release.build()
        self.assertEqual(str(stop.exception), "one\ntwo")
        self.assertEqual(checked.call_args.args[:3], ("v0.4.1", "0.4.1", "0.4.1"))


@unittest.skipUnless(SH and shutil.which("git"), "no sh or git")
class Hook(unittest.TestCase):
    """.githooks/pre-push itself, on a repository of its own: a tag pushed is checked at its commit, a branch not."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        from gitenv import own_repository_env     # not a calling git's repository (a hook, a rebase)
        self.env = {**own_repository_env(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
                    # as on Windows, where Python writes a pipe in the ANSI code page: check_tag.py writes UTF-8 anyway
                    "PYTHONIOENCODING": "cp1252",
                    # the checker run by this Python (the hook runs it through mise): as a shell command line
                    "BV_CHECK_TAG": f"{Path(sys.executable).as_posix()} {(ROOT / 'tools' / 'check_tag.py').as_posix()}"}
        if " " in self.env["BV_CHECK_TAG"].split(" ", 1)[0]:
            self.skipTest("a Python path with spaces")
        self.git("init", "-q")
        self.write(pyproject='[project]\nname = "x"\nversion = "0.4.1"\n', news=news_toml(),
                   log=changelog(date="Unreleased", link="compare/v0.4.0...HEAD"))
        (self.repo / "addon" / "BarrensVoice").mkdir(parents=True)
        (self.repo / release.TOC).write_text("## Version: 0.4.1\n", encoding="utf-8", newline="\n")
        self.commit()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self, pyproject=None, news=None, log=None, newline="\n"):
        # "\n" on every system (write_text's default is os.linesep), and the test's git has no autocrlf: the blobs
        # are as written
        for name, text in (("pyproject.toml", pyproject), ("news.toml", news), ("CHANGELOG.md", log)):
            if text is not None:
                (self.repo / name).write_text(text, encoding="utf-8", newline=newline)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "x")

    def push(self, *refs: str) -> subprocess.CompletedProcess:
        """The hook as git runs it for a push of `refs` (a tag: refs/tags/..., a branch: refs/heads/...)."""
        lines = "".join(f"{ref} {self.git('rev-parse', ref)} {ref} {'0' * 40}\n" for ref in refs)
        return self.run_hook(HOOK, lines)

    def run_hook(self, hook: Path, lines: str) -> subprocess.CompletedProcess:
        return subprocess.run([SH, str(hook), "origin", "https://example.invalid/x.git"], cwd=self.repo, env=self.env,
                              input=lines, capture_output=True, text=True, encoding="utf-8")

    def test_a_tag_is_checked_at_its_commit(self):
        self.git("tag", "-a", "v0.4.1", "-m", "0.4.1")
        refused = self.push("refs/tags/v0.4.1")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("CHANGELOG.md's section [0.4.1] is «Unreleased»", refused.stderr)
        self.assertIn("git tag -f v0.4.1", refused.stderr)
        # fixed in the working tree only: the tagged commit is still what is pushed
        self.write(log=changelog())
        self.assertEqual(self.push("refs/tags/v0.4.1").returncode, 1)
        self.commit()
        self.git("tag", "-f", "-a", "v0.4.1", "-m", "0.4.1")
        ready = self.push("refs/tags/v0.4.1")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertIn("v0.4.1: ready", ready.stdout)

    def test_a_test_build_and_a_branch(self):
        self.git("tag", "v0.4.1-beta.2")                                       # a lightweight tag too
        self.assertEqual(self.push("refs/tags/v0.4.1-beta.2").returncode, 0)
        self.git("tag", "v0.4.2-beta.1")                                       # not pyproject.toml's version
        wrong = self.push("refs/tags/v0.4.1-beta.2", "refs/tags/v0.4.2-beta.1")    # one bad tag refuses the push
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("the tag v0.4.2-beta.1 and pyproject.toml's version 0.4.1 differ", wrong.stderr)
        # a branch alone, however unready the files: not checked; a deleted tag neither
        branch = subprocess.run([SH, str(HOOK), "origin", "x"], cwd=self.repo, env=self.env,
                                input=f"refs/heads/master {self.git('rev-parse', 'HEAD')} refs/heads/master {'0' * 40}\n"
                                      f"(delete) {'0' * 40} refs/tags/v0.4.2 {self.git('rev-parse', 'HEAD')}\n",
                                capture_output=True, text=True)
        self.assertEqual((branch.returncode, branch.stdout, branch.stderr), (0, "", ""))

    def test_files_committed_with_crlf(self):
        # git show gives a blob's bytes: CRLF ones (Windows without autocrlf) read as CI's build reads the checkout
        self.write(log=changelog(), newline="\r\n")
        self.commit()
        self.assertIn(b"\r\n", subprocess.run(["git", "show", "HEAD:CHANGELOG.md"], cwd=self.repo, env=self.env,
                                              capture_output=True, check=True).stdout)
        self.git("tag", "v0.4.1")
        ready = self.push("refs/tags/v0.4.1")
        self.assertEqual(ready.returncode, 0, ready.stderr)

    def test_the_hook_keeps_lf(self):
        # sh reads "\r" as part of the line: a hook checked out with CRLF (Git for Windows' core.autocrlf=true, as
        # on CI) does not even parse - .gitattributes keeps .githooks/* LF
        lines = f"refs/tags/v0.4.1 {self.git('rev-parse', 'HEAD')} refs/tags/v0.4.1 {'0' * 40}\n"
        crlf = self.repo / "pre-push-crlf"
        crlf.write_bytes(HOOK.read_bytes().replace(b"\n", b"\r\n"))
        # not a proof that sh rejects "\r" (this tag is not ready, so a hook that parses refuses it too): only that a
        # CRLF copy never lets a tag through; the guarantee is the next two checks - the hook's bytes and the
        # checkout's filters
        self.assertNotEqual(self.run_hook(crlf, lines).returncode, 0)
        self.assertNotIn(b"\r", HOOK.read_bytes())
        if subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True).returncode != 0:
            self.skipTest("not a git checkout")
        # what a checkout with core.autocrlf=true writes: the hook with LF, BarrensVoice.cmd with its CRLF
        for name, crlf_lines in ((".githooks/pre-push", False), ("companion/BarrensVoice.cmd", True)):
            out = subprocess.run(["git", "-C", str(ROOT), "-c", "core.autocrlf=true", "cat-file", "--filters",
                                  f":{name}"], env=self.env, capture_output=True, check=True).stdout
            self.assertEqual(out.count(b"\r\n"), out.count(b"\n") if crlf_lines else 0, name)


if __name__ == "__main__":
    unittest.main()


class ForkPackaging(unittest.TestCase):
    def test_windows_checkout_preserves_line_endings_and_sets_update_identity(self):
        from base64 import b64encode
        from unittest.mock import patch
        public = b64encode(bytes(32)).decode()
        source = b'REPO = "upstream/project"\r\nPUBLIC_KEY = "old"\r\n'
        with patch.dict("os.environ", BV_RELEASE_REPO="fork/project", BV_UPDATE_PUBLIC_KEY=public):
            self.assertEqual(release.update_channel(source),
                             f'REPO = "fork/project"\r\nPUBLIC_KEY = "{public}"\r\n'.encode())
