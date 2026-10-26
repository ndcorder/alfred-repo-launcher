"""Run: /usr/bin/python3 -B -m unittest test_repo  (from this folder). Uses temp dirs only."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import repo


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_repo(path, head="ref: refs/heads/main\n", files=()):
    touch(os.path.join(path, ".git", "HEAD"), head)
    for name in files:
        touch(os.path.join(path, name))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def names(self, depth=4):
        return sorted(r["name"] for r in repo.find_repos([self.root], depth))

    def test_nested_repos_inside_a_repo_are_not_listed(self):
        # Submodules and vendored checkouts belong to their parent; listing them is noise.
        make_repo(os.path.join(self.root, "app"))
        make_repo(os.path.join(self.root, "app", "libs", "submodule"))
        self.assertEqual(self.names(), ["app"])

    def test_worktree_with_git_file_counts_as_repo(self):
        touch(os.path.join(self.root, "wt", "feature", ".git"), "gitdir: /x/.git/worktrees/feature\n")
        self.assertEqual(self.names(), ["feature"])

    def test_heavy_and_hidden_dirs_are_skipped(self):
        # Walking node_modules or .cache trees would make the scan slow for no gain.
        for d in ("node_modules", "vendor", ".venv", "venv", "dist", "build", ".cache"):
            make_repo(os.path.join(self.root, d, "pkg"))
        make_repo(os.path.join(self.root, "group", "real"))
        self.assertEqual(self.names(), ["real"])

    def test_depth_limit(self):
        make_repo(os.path.join(self.root, "a", "b", "c", "d"))
        make_repo(os.path.join(self.root, "a", "b", "c", "d2", "e"))
        self.assertEqual(self.names(depth=4), ["d"])

    def test_missing_root_is_ignored(self):
        self.assertEqual(repo.find_repos([os.path.join(self.root, "nope")], 4), [])


class Branch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_branch_with_slashes(self):
        make_repo(self.root, head="ref: refs/heads/feat/login\n")
        self.assertEqual(repo.read_branch(self.root), "feat/login")

    def test_detached_head_shows_short_sha(self):
        make_repo(self.root, head="3c8dace0f00dbabe1234567890abcdef12345678\n")
        self.assertEqual(repo.read_branch(self.root), "3c8dace0")

    def test_worktree_follows_absolute_gitdir(self):
        # A worktree's own .git is a file; its branch lives in the main repo's worktrees dir.
        main_git = os.path.join(self.root, "main", ".git")
        touch(os.path.join(main_git, "HEAD"), "ref: refs/heads/develop\n")
        touch(os.path.join(main_git, "worktrees", "wt", "HEAD"), "ref: refs/heads/fix/42\n")
        wt = os.path.join(self.root, "wt")
        touch(os.path.join(wt, ".git"), "gitdir: %s\n" % os.path.join(main_git, "worktrees", "wt"))
        self.assertEqual(repo.read_branch(wt), "fix/42")

    def test_worktree_follows_relative_gitdir(self):
        touch(os.path.join(self.root, "main", ".git", "worktrees", "wt", "HEAD"), "ref: refs/heads/rel\n")
        wt = os.path.join(self.root, "wt")
        touch(os.path.join(wt, ".git"), "gitdir: ../main/.git/worktrees/wt\n")
        self.assertEqual(repo.read_branch(wt), "rel")

    def test_broken_gitdir_gives_empty_branch_not_crash(self):
        touch(os.path.join(self.root, ".git"), "gitdir: /does/not/exist\n")
        self.assertEqual(repo.read_branch(self.root), "")


class IdeDetection(unittest.TestCase):
    def test_dotnet_wins_over_package_json(self):
        # A .NET solution with a JS frontend is still a Rider project.
        self.assertEqual(repo.detect_ide(["package.json", "App.sln"], "phpstorm"), "rider")

    def test_composer_wins_over_package_json(self):
        # Laravel apps ship package.json for assets; they are PHP projects.
        self.assertEqual(repo.detect_ide(["package.json", "composer.json"], "x"), "phpstorm")

    def test_python_wins_over_package_json(self):
        self.assertEqual(repo.detect_ide(["package.json", "requirements.txt"], "x"), "pycharm")

    def test_precedence_chain(self):
        cases = [
            (["web.csproj", "composer.json"], "rider"),
            (["composer.json", "go.mod"], "phpstorm"),
            (["go.mod", "Cargo.toml"], "goland"),
            (["Cargo.toml", "pyproject.toml"], "rustrover"),
            (["setup.py", "deno.json"], "pycharm"),
            (["deno.json"], "webstorm"),
        ]
        for files, want in cases:
            self.assertEqual(repo.detect_ide(files, "default"), want, files)

    def test_unknown_project_uses_default(self):
        self.assertEqual(repo.detect_ide(["README.md", "Makefile"], "goland"), "goland")

    def test_missing_launcher_falls_back_to_open(self):
        with mock.patch.object(repo, "LAUNCHER_DIR", "/nonexistent"):
            self.assertEqual(repo.launcher_for("phpstorm"), "/usr/bin/open")


TOOLBOX_LAUNCHER = """#!/bin/bash
#Generated by JetBrains Toolbox 3.8.1.88030 at 2026-10-14T13:47:08

declare -a intellij_args=()
declare -- wait=""

for o in "$@"; do
  if [[ "$o" = "--wait" || "$o" = "-w" ]]; then
    wait="-W"
    o="--wait"
  fi
  if [[ "$o" =~ " " ]]; then
    intellij_args+=("\\"$o\\"")
  else
    intellij_args+=("$o")
  fi
done

open -na "%s" $wait --args "${intellij_args[@]}"
"""


class IdeLaunch(unittest.TestCase):
    """The Toolbox launcher wraps arguments containing spaces in literal quotes, so the IDE
    receives '"/x/my projects/repo"' and fails. ↩ must bypass it with the same open call."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.binary = os.path.join(self.dir, "Php Storm.app", "Contents", "MacOS", "phpstorm")
        touch(self.binary)
        self.write_launcher(TOOLBOX_LAUNCHER % self.binary)
        p = mock.patch.object(repo, "LAUNCHER_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_launcher(self, text):
        path = os.path.join(self.dir, "phpstorm")
        touch(path, text)
        os.chmod(path, 0o755)
        self.launcher = path

    def test_path_with_spaces_is_one_argv_item_to_the_ide_binary(self):
        path = "/Users/x/src/my scripts/helpers"
        self.assertEqual(repo.open_argv("phpstorm", path),
                         ["/usr/bin/open", "-na", self.binary, "--args", path])

    def test_open_action_runs_argv_without_a_shell(self):
        path = "/Users/x/src/side projects/notes"
        with mock.patch.object(sys, "argv", ["repo.py", "--open", "phpstorm", path]), \
                mock.patch.object(repo.subprocess, "run") as run:
            repo.main()
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["/usr/bin/open", "-na", self.binary, "--args", path])
        self.assertNotIn("shell", run.call_args[1])

    def test_unparseable_launcher_falls_back_to_launcher(self):
        self.write_launcher("#!/bin/sh\nexec /somewhere/else \"$@\"\n")
        self.assertEqual(repo.open_argv("phpstorm", "/r"), [self.launcher, "/r"])

    def test_launcher_naming_missing_binary_falls_back_to_launcher(self):
        # A stale launcher after the IDE was removed: let the launcher report the error.
        self.write_launcher(TOOLBOX_LAUNCHER % "/gone/PhpStorm.app/Contents/MacOS/phpstorm")
        self.assertEqual(repo.open_argv("phpstorm", "/r"), [self.launcher, "/r"])

    def test_missing_launcher_opens_path(self):
        self.assertEqual(repo.open_argv("goland", "/r"), ["/usr/bin/open", "/r"])

    def test_plist_enter_action_uses_open_mode_with_item_ide(self):
        import plistlib
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "info.plist"), "rb") as f:
            wf = plistlib.load(f)
        sf = [o["uid"] for o in wf["objects"] if o["type"] == "alfred.workflow.input.scriptfilter"][0]
        dest = [c["destinationuid"] for c in wf["connections"][sf] if c["modifiers"] == 0][0]
        script = [o for o in wf["objects"] if o["uid"] == dest][0]["config"]["script"]
        self.assertEqual(script, '/usr/bin/python3 -B repo.py --open "$ide" "$1"')


class Ranking(unittest.TestCase):
    INDEX = [
        {"name": "tools", "path": repo.HOME + "/code/editor-stuff/tools", "ide": "x"},
        {"name": "zeta-editor", "path": repo.HOME + "/code/zeta-editor", "ide": "x"},
        {"name": "editor", "path": repo.HOME + "/code/editor", "ide": "x"},
        {"name": "editor-core", "path": repo.HOME + "/code/editor-core", "ide": "x"},
    ]

    def titles(self, q):
        return [r["name"] for r in repo.rank(self.INDEX, q)]

    def test_name_matches_rank_above_path_only_matches(self):
        # "tools" lives under an editor-* folder, but you typed a repo name.
        self.assertEqual(self.titles("editor"), ["editor", "editor-core", "zeta-editor", "tools"])

    def test_partial_name_finds_repo(self):
        self.assertEqual(self.titles("eta"), ["zeta-editor"])

    def test_case_insensitive_multi_word(self):
        self.assertEqual(self.titles("EDITOR core"), ["editor-core"])

    def test_no_match(self):
        self.assertEqual(self.titles("zzz"), [])


class Icons(unittest.TestCase):
    # Most repos open in PhpStorm, so the IDE icon alone can't tell them apart;
    # the monogram + colour must be distinct and stable.
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.patch = mock.patch.object(repo, "CACHE_DIR", self.cache)
        self.patch.start()
        repo._apps.clear()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.cache)
        repo._apps.clear()

    def test_monogram_rules(self):
        cases = {"zeta-editor": "ZE", "alfred_ssh": "AS", "phpMailer": "PM", "mailkit": "Ma",
                 "0day": "0d", "x": "X", "life engine": "LE", "--": "?"}
        for name, want in cases.items():
            self.assertEqual(repo.monogram(name), want, name)

    def test_repo_families_get_distinct_monograms(self):
        # acmedocs/acmecloud/acmephone would all read "Ac"; the letter after the shared stem tells them apart.
        names = ["acmedocs", "acmecloud", "acmephone", "acmeparq", "acmepulse", "acme-engine",
                 "acmecloud-admin", "mailkit", "mailkit-docs", "mailkit-extras", "tools"]
        stems = repo.branching_stems(names)
        got = {n: repo.monogram(n, stems) for n in names}
        self.assertEqual([got["acmedocs"], got["acmecloud"], got["acmephone"]], ["AD", "AC", "AP"])
        # "acmep" is shared by three names but is not a word anywhere, so it isn't a stem.
        self.assertNotIn("acmep", stems)
        self.assertEqual(got["acmeparq"], "AP")
        # mailkit's siblings all continue with "-", so there is no branching stem: keep "Ma".
        self.assertEqual(got["mailkit"], "Ma")
        self.assertEqual(got["acmecloud-admin"], "AA")
        self.assertEqual(got["tools"], "To")

    def test_index_carries_stem_aware_monograms(self):
        root = tempfile.mkdtemp()
        try:
            for n in ("acmedocs", "acmecloud", "acmephone", "acme-engine"):
                make_repo(os.path.join(root, n))
            idx = repo.build_index({"roots": [root], "depth": 2, "ide_default": "phpstorm"})
            self.assertEqual(sorted(r["mono"] for r in idx), ["AC", "AD", "AE", "AP"])
        finally:
            shutil.rmtree(root)

    def test_colour_is_stable_and_from_palette(self):
        self.assertEqual(repo.color_for("acmewatch"), repo.color_for("acmewatch"))
        self.assertIn(repo.color_for("acmewatch"), repo.PALETTE)

    def test_icon_files_differ_by_case_on_case_insensitive_disks(self):
        # APFS is case-insensitive by default: "Ph" and "PH" must not share a file.
        a = {"name": "phonebook", "ide": "phpstorm"}
        b = {"name": "php-helpers", "ide": "phpstorm"}
        with mock.patch.object(repo, "color_for", return_value="#1D4ED8"):
            self.assertEqual((repo.monogram(a["name"]), repo.monogram(b["name"])), ("Ph", "PH"))
            self.assertNotEqual(repo.icon_spec(a)[0].lower(), repo.icon_spec(b)[0].lower())

    def test_ensure_icons_batches_unique_missing_icons_once(self):
        repos = [{"name": "a-b", "ide": "phpstorm"}, {"name": "a-b", "ide": "phpstorm"},
                 {"name": "cd", "ide": "rider"}]
        seen = {}

        def fake_run(argv, **kw):
            with open(argv[-1]) as f:
                seen["jobs"] = json.load(f)
        with mock.patch.object(repo, "ide_app", return_value=None), \
                mock.patch.object(repo.subprocess, "run", side_effect=fake_run) as run:
            repo.ensure_icons(repos)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(seen["jobs"]), 2)
        self.assertEqual(os.listdir(repo.icon_dir()), [])  # job file cleaned up

    def test_render_failure_falls_back_to_ide_icon(self):
        r = {"name": "acmewatch", "ide": "phpstorm"}
        with mock.patch.object(repo, "ide_app", return_value="/Applications/PhpStorm.app"), \
                mock.patch.object(repo.subprocess, "run", side_effect=OSError("no osascript")):
            repo.ensure_icons([r])
            self.assertEqual(repo.icon_for(r), {"type": "fileicon", "path": "/Applications/PhpStorm.app"})

    def test_rendered_icon_is_used(self):
        r = {"name": "acmewatch", "ide": "phpstorm"}
        out, _ = repo.icon_spec(r)
        touch(out)
        self.assertEqual(repo.icon_for(r), {"path": out})


class CacheAndOutput(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self.patch = mock.patch.object(repo, "CACHE_DIR", self.cache)
        self.patch.start()
        self.icons = mock.patch.object(repo, "ensure_icons")  # no osascript in unit tests
        self.icons.start()
        self.env = mock.patch.dict(os.environ, {"REPO_ROOTS": self.root, "REPO_DEPTH": "3",
                                                "CLAUDE_CMD": "claude --resume"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.icons.stop()
        self.patch.stop()
        shutil.rmtree(self.cache)
        shutil.rmtree(self.root)

    def run_main(self, query):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["repo.py", query]), mock.patch.object(sys, "stdout", out):
            repo.main()
        return json.loads(out.getvalue())["items"]

    def test_cached_index_is_used_within_ttl(self):
        make_repo(os.path.join(self.root, "first"))
        self.run_main("")
        make_repo(os.path.join(self.root, "second"))
        with mock.patch.object(repo, "build_index") as build:
            self.run_main("first")
        build.assert_not_called()

    def test_unknown_name_triggers_rescan_for_fresh_clone(self):
        make_repo(os.path.join(self.root, "first"))
        self.run_main("")
        old = time.time() - 60  # older than RESCAN_MIN_AGE
        path = repo.cache_path(repo.settings())
        os.utime(path, (old, old))
        make_repo(os.path.join(self.root, "newclone"))
        self.assertEqual(self.run_main("newclone")[0]["title"], "newclone")

    def test_corrupt_cache_rescans(self):
        make_repo(os.path.join(self.root, "first"))
        touch(repo.cache_path(repo.settings()), '[{"name": "tru')
        self.assertEqual(self.run_main("first")[0]["title"], "first")

    def test_item_actions_quote_paths_with_spaces(self):
        make_repo(os.path.join(self.root, "my projects", "side project"), files=["composer.json"])
        it = self.run_main("side project")[0]
        path = os.path.join(self.root, "my projects", "side project")
        self.assertEqual(it["arg"], path)
        self.assertEqual(it["text"]["copy"], path)
        self.assertEqual(it["mods"]["alt"]["arg"], "cd '%s'" % path)
        self.assertEqual(it["mods"]["ctrl"]["arg"], "cd '%s' && claude --resume" % path)
        self.assertEqual(it["mods"]["cmd"]["arg"], path)
        self.assertEqual(it["mods"]["shift"]["arg"], path)
        self.assertIn("main", it["subtitle"])
        self.assertIn("phpstorm", it["subtitle"])
        self.assertEqual(it["variables"], {"ide": "phpstorm"})  # feeds repo.py --open "$ide"

    def test_emitted_mods_match_plist_connections(self):
        # A mod without a plist connection does nothing in Alfred; keep them in sync.
        plist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "info.plist")
        import plistlib
        with open(plist, "rb") as f:
            wf = plistlib.load(f)
        sf = [o["uid"] for o in wf["objects"] if o["type"] == "alfred.workflow.input.scriptfilter"][0]
        masks = {c["modifiers"] for c in wf["connections"][sf]}
        make_repo(os.path.join(self.root, "x"))
        mods = self.run_main("x")[0]["mods"]
        bits = {"cmd": 1048576, "alt": 524288, "ctrl": 262144, "shift": 131072}
        self.assertEqual(masks, {0} | {bits[m] for m in mods})

    def test_empty_roots_give_helpful_item(self):
        items = self.run_main("")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["valid"])
        self.assertIn("No git repos found", items[0]["title"])


if __name__ == "__main__":
    unittest.main()
