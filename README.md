# Repo Launcher for Alfred

Find any local git repository from Alfred and open it in the right JetBrains IDE, in Fork, in your terminal, or straight into a Claude Code session.

Each result shows the IDE's own icon with a coloured monogram for the project, so a list of twenty PhpStorm projects is still easy to scan. Monograms are aware of naming families: `acmedocs`, `acmecloud` and `acmephone` become AD, AC and AP instead of three identical "Ac".

## Usage

Type `repo` and part of a repository name. Name matches rank above path-only matches.

| Key | Action |
|---|---|
| ↩ | Open in the IDE that fits the project |
| ⌘ ↩ | Open in Fork |
| ⌥ ↩ | Open your terminal in the repository |
| ⌃ ↩ | Open your terminal and start Claude Code there |
| ⇧ ↩ | Reveal in Finder |
| ⌘ C | Copy the path |

The subtitle shows the path, the current branch (or short SHA when detached; worktrees are followed) and the IDE.

The IDE is chosen from files in the project root, first match wins:

| Marker | IDE |
|---|---|
| `*.sln`, `*.csproj`, `*.fsproj` | Rider |
| `composer.json` | PhpStorm |
| `go.mod` | GoLand |
| `Cargo.toml` | RustRover |
| `pyproject.toml`, `setup.py`, `requirements.txt` | PyCharm |
| `package.json`, `deno.json` | WebStorm |
| anything else | the Default IDE setting |

## Configuration

Open the workflow in Alfred Preferences and click Configure Workflow.

| Setting | Default | Meaning |
|---|---|---|
| Search folders | `~/Projects:~/Developer` | Colon-separated folders to search |
| Search depth | `4` | Folder levels below each search folder |
| Default IDE | `phpstorm` | IDE for projects without a marker file |
| Claude Code command | `claude` | What ⌃ ↩ runs |

Repositories are not searched inside other repositories, and `node_modules`, `vendor`, `.venv`, `venv`, `dist`, `build` and dot-folders are skipped. The index is cached for five minutes; a search with no hits rescans at once, so a fresh clone shows up immediately.

## Requirements

- macOS with Alfred 5 and the Powerpack
- `/usr/bin/python3` (installed with the Xcode Command Line Tools)
- JetBrains IDEs with Toolbox shell scripts enabled (Toolbox, Settings, Generate shell scripts), in `/usr/local/bin`. Without a launcher the project opens with the default app.
- Fork for ⌘ ↩. Terminal actions use the app chosen in Alfred's Terminal setting (Terminal, iTerm or a custom script).

The Toolbox launcher scripts wrap any path containing a space in literal quote characters, which the IDE cannot open. The workflow reads the IDE binary from the launcher and starts it directly, so paths with spaces work.

## Icons

Icons are drawn by `iconkit.js`, a small JavaScript for Automation script that uses AppKit, so there is nothing to install. They are rendered once per project into Alfred's workflow cache (about 5 ms each). If rendering fails the plain IDE icon is used.

## Install

Download the latest `.alfredworkflow` from Releases and open it.

To work on it, clone the repository and symlink it into Alfred's workflows folder:

```sh
git clone https://github.com/ndcorder/alfred-repo-launcher.git
ln -s "$PWD/alfred-repo-launcher" "<Alfred preferences>/Alfred.alfredpreferences/workflows/alfred-repo-launcher"
```

Your Configure Workflow values are saved in `prefs.plist` in the workflow folder, which is ignored by git.

## Development

```sh
/usr/bin/python3 -B -m unittest test_repo
```

Tests use temporary folders only and never launch an IDE or osascript.

## Licence

MIT. See [LICENSE](LICENSE).
