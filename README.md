# tareas

A TUI for tracking client tasks on a **GitHub Project (v2)**, built to stay open all
day in a small pane without getting in the way.

![The task list](docs/list.png)

A task is a GitHub issue inside a Project. This app is the fast view of that
Project: what's pending, what's due next, and what can be closed — without opening
a browser.

## Why it exists

GitHub's boards are comfortable full-screen and awful in a terminal pane. This is
the opposite:

- **Fits in a small pane.** The reference case is 80×15. One line per task, one
  header line, one line of shortcuts, nothing else.
- **Mouse-first.** Every action has a clickable target: rows, header buttons,
  footer shortcuts, and every button inside a dialog. The keyboard is a shortcut,
  not a requirement.
- **Responsive.** Resizing the pane recalculates column widths and truncates
  titles with an ellipsis; nothing overflows or gets cut off.
- **Contextual repo mode.** Launch it inside a repo with a GitHub remote and the
  app starts filtered to that repo, with a toggle (`t`, or a click on
  "all"/"this repo") to switch to the full list and back.
- **Quick-pick due dates.** Set a due date with one click — today, tomorrow, +3
  days, next week, +1 month — or type an exact `YYYY-MM-DD`.
- **Auto-refresh.** The list reloads itself every 5 minutes; the header shows how
  long ago it last synced.
- **Uses your terminal's colors.** No hardcoded palette — it inherits yours, so if
  your terminal switches between light and dark, the app switches with it.

| Small pane (80×15) | Light terminal |
|---|---|
| ![Small pane](docs/small-pane.png) | ![Light mode](docs/light.png) |

## Requirements

- **Python 3.11 or higher** (uses `tomllib`).
- **[GitHub CLI](https://cli.github.com), authenticated** (`gh auth login`). All
  reading and writing goes through `gh`; the app doesn't store credentials.
- A GitHub Project (v2) with a *Date*-type field for the due date.

## Installation

```bash
git clone https://github.com/Pit-CL/tareas.git ~/Projects/tareas
~/Projects/tareas/bin/tareas
```

The `bin/tareas` launcher takes care of the rest: the first run sets up an
isolated environment in `~/.venvs/tareas` with the textual version the app needs,
and from then on it just starts it. It also restarts the app if it crashes, so the
pane it lives in never ends up dead.

To have it on your `PATH`:

```bash
ln -sf ~/Projects/tareas/bin/tareas ~/.local/bin/tareas
```

It can also be installed as a package, if you'd rather manage the environment
yourself:

```bash
pipx install git+https://github.com/Pit-CL/tareas.git   # gives you the tareas-tui command
```

## Configuration

Copy [`config.example.toml`](config.example.toml) to
`~/.config/tareas/config.toml` and adjust the values:

```toml
owner = "my-user"             # owner of the Project (user or organization)
project = 1                   # the number at the end of the Project's URL
campo_fecha = "Due date"      # name of the Date-type field
estado_hecho = "Done"         # Status option that counts as done
cuerpo_nuevo = "Created from the tareas TUI."
```

No need to look up internal identifiers: the app resolves the Project's node IDs
with `gh` on first run and caches them in `~/.config/tareas/ids-cache.json`. If you
ever change the Project, delete that file and they get resolved again — that's
also when the app picks up a renamed Project title (see below).

## Usage

The main view is just the list, sorted by due date. Everything else is a dialog
that opens on top and closes with `esc`, a click outside, or its own button.

The header title shows the **real name of your GitHub Project** (fetched through
`gh`, not a fixed label) while you're looking at the full list; in repo mode it
shows that repo's `owner/name` instead.

**With the mouse**

| Gesture | What it does |
|---|---|
| Click a row | Selects it |
| Click the already-selected row, or double-click | Opens the detail |
| Wheel | Scrolls the list and the detail body |
| Click `+ new` / `⟳` (header) | Creates a task / reloads |
| Click a footer shortcut | Runs that action |
| Click outside a dialog | Closes it |

**With the keyboard**

| Key | Action |
|---|---|
| `↑` `↓` (or `k` `j`) | Move the selection |
| `enter` | View the detail |
| `n` | New task |
| `d` | Change the due date |
| `x` | Close the task (asks for confirmation) |
| `r` | Reload |
| `t` | Toggle between "all" and "this repo" (only shown inside a repo) |
| `q` | Quit |

The list reloads itself every 5 minutes; the header shows how long ago it last
updated.

### Contextual repo mode

If you launch `tareas` from inside a repo with a GitHub remote, the app starts
filtered to that repo: the header shows its name instead of your Project's title,
and the list only shows that repo's tasks. A click on "all" (or the `t` key)
toggles to the full list and back; outside a repo, the toggle doesn't appear.

![Repo mode](docs/repo-mode.png)

Creating a task (`n`) in repo mode preselects that repo without opening the
repo picker — it only asks for a title and a date. If you'd rather pick a
different repo, click the fixed repo label to reveal the normal picker.

### Setting a due date without typing

The due date dialog has clickable shortcuts — *today*, *tomorrow*, *+3 days*,
*next week*, *+1 month* — and also accepts an exact date as `YYYY-MM-DD`. The
*clear* button removes the due date.

| Detail | Due date | New task |
|---|---|---|
| ![Detail](docs/detail.png) | ![Due date](docs/due-date.png) | ![New task](docs/new-task.png) |

When nothing is left pending, it says so:

![Nothing pending](docs/empty.png)

## Colors

The app defines a theme with `ansi=True`, so its colors are your terminal's **own
ANSI 0-15 colors**: the background and text are yours, and status colors use the
semantic slots (red for overdue, yellow for due soon, dim for the rest). That's
why the screenshots above look different from each other — it's the same code
over different palettes — and why there's nothing to configure to make it look
right in light or dark.

## Running in a persistent pane

`bin/tareas` restarts the app on its own if it crashes (only exit codes `0`, `2`,
`130`, and `143` are treated as final; anything else triggers a restart after a
short pause), which makes it a good fit for a pane that's meant to stay alive —
tmux, another terminal multiplexer, or any pane manager that keeps a long-running
command around. Point that pane's command at `bin/tareas` (or the `tareas` symlink
on your `PATH`) and it keeps coming back on its own.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
TAREAS_DEMO=1 .venv/bin/python -m tareas_tui     # sample data, no GitHub calls
.venv/bin/pytest                                 # smoke test with textual's Pilot
```

`TAREAS_DEMO=1` starts the app with sample tasks and without calling `gh`. That's
what's used for this README's screenshots. `BackendDemo(repo_actual="owner/repo")`
also simulates contextual repo mode without touching GitHub.

The code is three modules: `config.py` (configuration and ID resolution),
`datos.py` (`gh` calls and formatting), and `app.py` (the interface).

> **Note:** the interface currently ships in English; the dates and data you'll
> see when you run it come straight from your own GitHub Project.

## License

MIT — see [LICENSE](LICENSE).
