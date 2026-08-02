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
- **Responsive, in both directions.** Resizing the pane recalculates column widths
  and truncates titles with an ellipsis; and from 22 rows of screen height up, the
  dialogs stop being cramped — they gain padding and a blank line between groups.
  Below that they fall back to the compact layout. Dialogs are always as tall as
  their content, never taller.
- **Contextual repo mode.** Launch it inside a repo with a GitHub remote and the
  app starts filtered to that repo, with a toggle (`t`, or a click on
  "all"/"this repo") to switch to the full list and back.
- **Quick-pick due dates.** Set a due date with one click — today, tomorrow, +3
  days, next week, +1 month — or type an exact `YYYY-MM-DD`.
- **Recurring tasks.** Mark a task as daily, weekly, biweekly or monthly and
  closing it opens the next occurrence automatically.
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
```

No need to look up internal identifiers: the app resolves the Project's node IDs
with `gh` on first run and caches them in `~/.config/tareas/ids-cache.json`. If you
ever change the Project, delete that file and they get resolved again — that's
also when the app picks up a renamed Project title (see below).

> **Removed in 1.1.0:** the `cuerpo_nuevo` key. New issues used to be created with
> that fixed placeholder body; now the body is whatever you type in the *notes*
> field of the new-task dialog, and empty by default. If the key is still in your
> config file it's simply ignored.

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
| Click the `↻ repeat:` chip | Cycles the recurrence |
| Click outside a dialog | Closes it |

**With the keyboard**

| Key | Where | Action |
|---|---|---|
| `↑` `↓` (or `k` `j`) | list | Move the selection |
| `enter` | list | View the detail |
| `n` | list | New task |
| `d` | list | Change the due date |
| `x` | list | Close the task (asks for confirmation) |
| `r` | list | Reload |
| `t` | list | Toggle between "all" and "this repo" (only inside a repo) |
| `g` / `G` | list | Jump to the first / last task |
| `q` | list | Quit |
| `j` / `k` | detail | Scroll the body |
| `1`–`5` | new task, due date | Quick-pick a due date |
| `enter` | new task | Jump to the next field |
| `ctrl+enter` (or `ctrl+s`) | new task, due date | Create / save, from any field |
| `ctrl+r` | new task | Cycle the recurrence |
| `y` / `n` | confirmation | Confirm / cancel |
| `esc` | any dialog | Close it |

`ctrl+enter` needs a terminal that speaks the [kitty keyboard
protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/) (kitty, WezTerm,
Ghostty, foot…). `ctrl+s` does the same thing everywhere else.

The list reloads itself every 5 minutes; the header shows how long ago it last
updated.

### Contextual repo mode

If you launch `tareas` from inside a repo with a GitHub remote, the app starts
filtered to that repo: the header shows its name instead of your Project's title,
and the list only shows that repo's tasks. A click on "all" (or the `t` key)
toggles to the full list and back; outside a repo, the toggle doesn't appear.

![Repo mode](docs/repo-mode.png)

Creating a task (`n`) in repo mode preselects that repo without opening the
repo picker — it only asks for a title, notes and a date. If you'd rather pick a
different repo, click the fixed repo label to reveal the normal picker.

### Creating a task

The new-task dialog asks for a repo, a title, optional *notes* (they become the
issue body — leave them empty and the issue has no body), an optional due date and
an optional recurrence. `enter` walks the fields one by one; `ctrl+enter` creates
from wherever you are.

| Roomy (110×24) | Compact (80×15) |
|---|---|
| ![New task](docs/new-task.png) | ![New task, small pane](docs/new-task-small.png) |

### Recurring tasks

The `↻ repeat:` chip in the new-task dialog cycles through **none → daily →
weekly → biweekly → monthly** (click it, or press `ctrl+r`). A recurrence needs a
due date to count from: without one the task is still created, but the app tells
you the recurrence wasn't applied.

Recurring tasks show a `↻` in the list and a `↻ repeats <interval>` note in the
detail. When you close one **from the TUI** (`x`, then confirm), the app closes the
issue and immediately creates the next occurrence: same repo, title and notes, with
the due date moved one interval forward. If the task was already overdue, the new
date catches up until it lands in the future — a weekly task closed a month late
comes back next week, not already overdue. Monthly moves by calendar month and
clamps to the end of it (31 January → 28 February), always counting from the
original day, so a task due on the 31st doesn't drift backwards over time.

The recurrence lives inside the issue body as an HTML comment
(`<!-- tareas:repeat=weekly -->`), which GitHub doesn't render, so nothing extra is
needed in your Project and the notes you see are only yours.

Two limitations worth knowing:

- **Only the TUI spawns the next occurrence.** Closing the issue from GitHub, from
  `gh`, or with a `Closes #N` in a pull request just closes it — the series stops
  there.
- **The recurrence is set when the task is created.** The due-date dialog doesn't
  change it; to alter the recurrence of an existing task, edit the
  `<!-- tareas:repeat=… -->` comment in the issue body.

### Setting a due date without typing

The due date dialog has clickable shortcuts — *today*, *tomorrow*, *+3 days*,
*next week*, *+1 month* — and also accepts an exact date as `YYYY-MM-DD`. The
*clear* button removes the due date.

| Detail | Due date |
|---|---|
| ![Detail](docs/detail.png) | ![Due date](docs/due-date.png) |

When nothing is left pending, it says so:

![Nothing pending](docs/empty.png)

## Colors

The app defines a theme with `ansi=True`, so its colors are your terminal's **own
ANSI 0-15 colors**: the background and text are yours, and status colors use the
semantic slots (red for overdue, yellow for due soon, dim for the rest). That's
why the screenshots above look different from each other — it's the same code
over different palettes — and why there's nothing to configure to make it look
right in light or dark.

The one place that needs care is the row under the cursor, which paints text in
color 0 over color 3. Textual's `cursor_foreground_priority` only overrides the
*color* of a cell, never its attributes, so a cell marked `dim` kept fading
against the cursor's background until it was unreadable. `TablaTareas` cancels
`dim` on that row, so it reads as one solid block.

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
`datos.py` (`gh` calls, date math and formatting), and `app.py` (the interface).
Tests are split the same way: `tests/test_repeticion.py` covers the recurrence
math on its own, `tests/test_app.py` drives the interface with textual's Pilot.

> **Note:** the interface currently ships in English; the dates and data you'll
> see when you run it come straight from your own GitHub Project.

## License

MIT — see [LICENSE](LICENSE).
