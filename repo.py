#!/usr/bin/env python3
"""Alfred Script Filter: find local git repos and open them in an IDE, Fork, a terminal or Claude Code."""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CACHE_DIR = os.environ.get("alfred_workflow_cache", "/tmp/alfred-repo-launcher")
CACHE_TTL = 300  # seconds; a new clone shows up after this, or at once when a query finds nothing
RESCAN_MIN_AGE = 15  # a no-hit query rescans only if the index is older than this
MAX_ITEMS = 300
SKIP_DIRS = {"node_modules", "vendor", ".venv", "venv", "dist", "build"}
LAUNCHER_DIR = "/usr/local/bin"
OPEN = "/usr/bin/open"

# First rule that matches a top-level file wins.
IDE_RULES = [
    ("rider", lambda n: n.endswith((".sln", ".csproj", ".fsproj"))),
    ("phpstorm", lambda n: n == "composer.json"),
    ("goland", lambda n: n == "go.mod"),
    ("rustrover", lambda n: n == "Cargo.toml"),
    ("pycharm", lambda n: n in ("pyproject.toml", "setup.py", "requirements.txt")),
    ("webstorm", lambda n: n in ("package.json", "deno.json")),
]


def settings():
    roots = os.environ.get("REPO_ROOTS") or "~/Projects:~/Developer"
    try:
        depth = int(os.environ.get("REPO_DEPTH") or 4)
    except ValueError:
        depth = 4
    return {
        "roots": [os.path.expanduser(r.strip()) for r in roots.split(":") if r.strip()],
        "depth": depth,
        "ide_default": os.environ.get("IDE_DEFAULT") or "phpstorm",
        "claude_cmd": os.environ.get("CLAUDE_CMD") or "claude",
    }


def detect_ide(names, default):
    for ide, matches in IDE_RULES:
        if any(matches(n) for n in names):
            return ide
    return default


def find_repos(roots, max_depth):
    """Return [{name, path, files}] for every repo under roots, without descending into repos."""
    repos = []

    def walk(path, depth):
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        names = [e.name for e in entries]
        if ".git" in names:  # dir for normal repos, file for worktrees and submodules
            repos.append({"name": os.path.basename(path), "path": path, "files": names})
            return
        if depth >= max_depth:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    walk(e.path, depth + 1)
            except OSError:
                continue

    for root in roots:
        walk(root.rstrip("/") or "/", 0)
    return repos


def build_index(cfg):
    repos = find_repos(cfg["roots"], cfg["depth"])
    stems = branching_stems([r["name"] for r in repos])
    return [{"name": r["name"], "path": r["path"], "ide": detect_ide(r["files"], cfg["ide_default"]),
             "mono": monogram(r["name"], stems)}
            for r in repos]


def cache_path(cfg):
    key = hashlib.sha1(json.dumps([cfg["roots"], cfg["depth"], cfg["ide_default"]]).encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, "repos-%s.json" % key)


def write_cache(path, index):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Write then rename: Alfred kills the previous run on each keystroke.
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(index, f)
    os.replace(tmp, path)


def load_index(cfg, max_age=CACHE_TTL):
    """Return (index, age_seconds). age is 0 when freshly scanned."""
    path = cache_path(cfg)
    try:
        age = time.time() - os.path.getmtime(path)
        if age < max_age:
            with open(path) as f:
                return json.load(f), age
    except (OSError, ValueError):
        pass  # missing, unreadable or truncated: rescan
    index = build_index(cfg)
    try:
        write_cache(path, index)
    except OSError:
        pass
    return index, 0


def read_branch(repo_path):
    """Branch name from HEAD, short SHA when detached, '' if unreadable. Follows worktree gitdir files."""
    git = os.path.join(repo_path, ".git")
    try:
        if os.path.isfile(git):
            with open(git) as f:
                line = f.read().strip()
            if not line.startswith("gitdir:"):
                return ""
            git = os.path.normpath(os.path.join(repo_path, line[len("gitdir:"):].strip()))
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return head[:8]


def rank(index, query):
    """Repos matching every query word; name matches before path-only matches."""
    words = query.lower().split()
    if not words:
        return sorted(index, key=lambda r: r["name"].lower())
    q = " ".join(words)
    scored = []
    for r in index:
        name = r["name"].lower()
        path = shorten(r["path"]).lower()
        if all(w in name for w in words):
            tier = 0 if name == q else 1 if name.startswith(words[0]) else 2
        elif all(w in path for w in words):
            tier = 3
        else:
            continue
        scored.append((tier, len(name), name, r["path"], r))
    scored.sort(key=lambda s: s[:4])
    return [s[4] for s in scored]


def shorten(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def launcher_for(ide):
    path = os.path.join(LAUNCHER_DIR, ide)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else OPEN


# Toolbox launchers end with: open -na "/Users/x/Applications/PhpStorm.app/Contents/MacOS/phpstorm" $wait --args ...
TOOLBOX_OPEN = re.compile(r'open\s+-na\s+"([^"]+)"')


def app_binary(launcher):
    """IDE binary named in a JetBrains Toolbox launcher script, or None."""
    try:
        with open(launcher, errors="replace") as f:
            m = TOOLBOX_OPEN.search(f.read(65536))
    except OSError:
        return None
    return m.group(1) if m and os.path.isfile(m.group(1)) else None


def open_argv(ide, path):
    """argv that opens path in ide. The Toolbox launcher (3.8.1) wraps any argument containing a
    space in literal quote characters, so the IDE gets '"/a b/repo"' and cannot open it. Calling
    open -na <binary> --args <path> ourselves does what the launcher does, with path as one argv item."""
    launcher = launcher_for(ide)
    if launcher == OPEN:
        return [OPEN, path]
    binary = app_binary(launcher)
    return [OPEN, "-na", binary, "--args", path] if binary else [launcher, path]


def icon_dir():
    return os.path.join(CACHE_DIR, "icons")


ICONKIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iconkit.js")
# Tailwind 700 shades: white text on each passes WCAG AA (>= 4.5:1).
PALETTE = ["#1D4ED8", "#B91C1C", "#0F766E", "#7E22CE", "#B45309",
           "#BE185D", "#4338CA", "#15803D", "#C2410C", "#334155"]


def _words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def monogram(name, stems=()):
    """'zeta-editor' -> 'ZE', 'phpMailer' -> 'PM', 'mailkit' -> 'Ma', '0day' -> '0d'.
    With stems (see branching_stems), 'acmedocs' -> 'AD' rather than 'Ac'."""
    words = _words(name)
    if not words:
        return "?"
    if len(words) > 1:
        return (words[0][0] + words[1][0]).upper()
    w = words[0]
    for stem in stems:  # longest first
        if w.lower().startswith(stem) and len(w) > len(stem):
            return (w[0] + w[len(stem)]).upper()
    return w[0].upper() + w[1:2].lower()


def branching_stems(names):
    """Prefixes (3+ chars) that 3+ repo names share and then diverge after, e.g. 'acme' for
    acmedocs/acmecloud/acmephone. Single-word repos in such a family would all read 'Ac'.
    A stem must also be a word on its own somewhere (acme-engine), so accidental prefixes
    like 'acmep' (acmeparq/acmephone/acmepulse) don't count."""
    lowered = [n.lower() for n in names]
    words = {w.lower() for n in names for w in _words(n)}
    stems = set()
    for w in {ws[0].lower() for ws in map(_words, names) if len(ws) == 1}:
        for k in range(len(w) - 1, 2, -1):
            p = w[:k]
            if p not in words:
                continue
            family = [n for n in lowered if n.startswith(p) and len(n) > k]
            if len(family) >= 3 and len({n[k] for n in family}) >= 2:
                stems.add(p)
                break
    return sorted(stems, key=len, reverse=True)


def color_for(name):
    """Stable per repo, so a repo keeps its colour across runs and machines."""
    return PALETTE[int(hashlib.sha1(name.encode()).hexdigest(), 16) % len(PALETTE)]


_apps = {}


def ide_app(ide):
    """The IDE's .app bundle (for its icon), or None."""
    if ide not in _apps:
        launcher = launcher_for(ide)
        binary = app_binary(launcher) if launcher != OPEN else None
        app = binary[:binary.index(".app") + 4] if binary and ".app" in binary else None
        _apps[ide] = app if app and os.path.isdir(app) else None
    return _apps[ide]


def icon_spec(r):
    """(png path, iconkit job) for a repo: its IDE's icon with a monogram badge."""
    mono, color = r.get("mono") or monogram(r["name"]), color_for(r["name"])
    # Hashed name: macOS volumes are usually case-insensitive, so "Ph" and "PH" icons
    # would otherwise share one file.
    key = hashlib.sha1(("%s|%s|%s" % (r["ide"], mono, color)).encode()).hexdigest()[:16]
    out = os.path.join(icon_dir(), "%s-%s.png" % (r["ide"], key))
    app = ide_app(r["ide"])
    job = {"out": out, "base": app, "badge": {"text": mono, "color": color}} if app else \
        {"out": out, "bg": color, "text": mono}
    return out, job


def ensure_icons(repos):
    """Render missing icons in one batch (~5 ms each). Failures just leave the fallback icon."""
    jobs, seen = [], set()
    for r in repos:
        out, job = icon_spec(r)
        if out not in seen and not os.path.exists(out):
            seen.add(out)
            jobs.append(job)
    if not jobs:
        return
    spec = os.path.join(icon_dir(), "jobs-%d.json" % os.getpid())
    try:
        os.makedirs(icon_dir(), exist_ok=True)
        with open(spec, "w") as f:
            json.dump(jobs, f)
        subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", ICONKIT, spec],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.remove(spec)
    except OSError:
        pass


def icon_for(r):
    out, _ = icon_spec(r)
    if os.path.exists(out):
        return {"path": out}
    app = ide_app(r["ide"])
    return {"type": "fileicon", "path": app} if app else None


def item(r, cfg):
    path, short = r["path"], shorten(r["path"])
    branch = read_branch(path)
    launcher = launcher_for(r["ide"])
    ide_label = r["ide"] if launcher != OPEN else "%s missing → open" % r["ide"]
    cd = "cd %s" % shlex.quote(path)
    icon = icon_for(r)
    return dict({"icon": icon} if icon else {}, **{
        "uid": path,
        "title": r["name"],
        "subtitle": " · ".join(p for p in (short, branch, ide_label) if p),
        "arg": path,
        "autocomplete": r["name"],
        "quicklookurl": path,
        "text": {"copy": path, "largetype": path},
        "variables": {"ide": r["ide"]},
        "mods": {
            "cmd": {"arg": path, "subtitle": "Open in Fork"},
            "alt": {"arg": cd, "subtitle": "Open a terminal at %s" % short},
            "ctrl": {"arg": "%s && %s" % (cd, cfg["claude_cmd"]),
                     "subtitle": "Start %s in %s" % (cfg["claude_cmd"], short)},
            "shift": {"arg": path, "subtitle": "Reveal in Finder"},
        },
    })


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--open":  # ↩ action: repo.py --open <ide> <path>
        subprocess.run(open_argv(sys.argv[2] or settings()["ide_default"], sys.argv[3]), timeout=30)
        return
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = settings()
    try:
        index, age = load_index(cfg)
        hits = rank(index, query)
        if not hits and age > RESCAN_MIN_AGE:  # maybe a fresh clone
            index, age = load_index(cfg, max_age=0)
            hits = rank(index, query)
        shown = hits[:MAX_ITEMS]
        ensure_icons(shown)
        items = [item(r, cfg) for r in shown]
        if not items:
            items = [{
                "title": "No repo matches '%s'" % query if query.strip() else "No git repos found",
                "subtitle": "Searched %s (depth %d)" % (", ".join(shorten(r) for r in cfg["roots"]), cfg["depth"]),
                "valid": False,
            }]
    except Exception as e:  # always hand Alfred valid JSON
        items = [{"title": "Repo search failed", "subtitle": "%s: %s" % (type(e).__name__, e), "valid": False}]
    json.dump({"items": items}, sys.stdout)


if __name__ == "__main__":
    main()
