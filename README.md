# Taskly — To-Do List with accounts and team workspaces

A small to-do app with email/password accounts, private per-user tasks, and
shared team boards with assignments, deadlines, threaded updates, live sync,
and data-driven completion forecasts.

- `index.html` — the whole front end (HTML, CSS, JS)
- `server.py` — a zero-dependency Python backend (stdlib + SQLite)
- `task_intel.json` — the precomputed forecast model served by the API
- `tools/build_intel.py` — a dev-only script that rebuilds the model from the
  source spreadsheet (not needed to run the app)

No build step, no npm packages, no third-party Python packages.

## Run it

Requires Python 3.8 or newer.

```sh
python server.py
```

Then open <http://127.0.0.1:8000>.

The server creates `taskly.db` next to `server.py` on first start and serves
the app from the same origin, so cookies and the API work without any CORS
setup.

To use a different port:

```sh
# PowerShell
$env:PORT=8080; python server.py

# macOS / Linux
PORT=8080 python server.py
```

## Accounts

- Create an account with an email address and a password of at least
  8 characters.
- Sign in with the same email and password.
- Sessions last 30 days and are kept in an HttpOnly `SameSite=Lax` cookie,
  so reloading the page keeps you signed in.
- Every personal task, subtask, and deadline belongs to exactly one user. Two
  accounts never see each other's **My Tasks**; only team boards are shared,
  and only with that team's members.
- Sign out with the button in the top-right corner.

### How passwords are stored

- PBKDF2-HMAC-SHA256 with 210,000 iterations and a random 16-byte salt per
  user (`hashlib.pbkdf2_hmac`).
- Only the hash is stored; the plain password never touches the database.
- Session tokens are random 32-byte values; the database stores only their
  SHA-256 hash.
- Login attempts are throttled: 10 failures per IP address per 5 minutes.

### Good to know

This is a self-hosted local app, not a hardened public service:

- Traffic is plain HTTP. Put it behind HTTPS before exposing it to a network.
- There is no email verification, password reset, or account deletion flow.
- `taskly.db` contains password hashes and task data — keep it private.
  The server refuses to serve `.db`, `.py`, `.json`, and other source files,
  but the files are still readable on disk.
- Deleting `taskly.db` while the server is stopped resets all accounts and
  tasks.

## Features

- Add tasks with an optional deadline (date picker in the composer) and a
  priority: **Urgent**, **High**, **Medium**, or **Low** (default Medium)
- Tasks grouped by deadline: **Overdue**, **Today**, **Tomorrow**,
  **Next 7 days**, **Later**, and **No deadline**
- Deadline column shows a chip per task; click it to change or clear the date
- Progress column shows a percentage bar per task and a completed/total
  fraction when the task has subtasks
- Break any task into smaller tasks with the list-plus button; subtasks sit
  indented under their parent, inside the deadline group
- Checking a parent completes all its subtasks; checking every subtask
  completes the parent. Partial parents show an indeterminate marker.
- Filters: All / Active / Done, with live counts
- Overall progress bar counts leaf items (subtasks count individually)
- Collapse/expand subtasks with the chevron
- Priority, on-time probability, and automatically assigned **category** pills
  on every row; click a priority or probability pill to open a forecast dialog
  with the priority, shift reason, expected duration, and deadline advice
- A **Short lead** flag when the deadline allows fewer days than the model's
  average completion time; the on-time probability is lowered to match
- A **Workload assessment** panel compares open tasks with nearby deadlines
  against realistic parallel capacity and raises a heavy-load alert
- A **Focus order** panel appears when several open tasks share a deadline,
  ordering them Urgent → High → Medium → Low, then by the lowest on-time odds
- A short instructions panel explains forecasts, categories, short-lead alerts,
  workload alerts, and focus order
- Light and dark themes (follows your OS setting)
- Keyboard: `/` focuses the input, `Enter` edits the focused task,
  `Enter`/`Escape` save/cancel while editing, `Escape` closes a subtask input
  or the forecast dialog
- Responsive layout with reduced-motion support

## Team workspaces

The app has two views, switched with the tabs under the header:

- **My Tasks** — the private workspace described above. Nothing here is ever
  shared.
- **Team Tasks** — shared boards for teams you own or belong to.

### Teams and roles

- Anyone can create a team; the creator becomes the **Owner**.
- The owner can invite people, remove members, cancel pending invites, and
  delete any task on the board.
- **Members** can open the team, add and edit tasks, assign work, comment,
  and leave the team. Members can delete only tasks they created.
- Owners cannot leave their own team or be removed from it.

### Inviting people

Invites are matched against registered accounts by email address. The email
must already belong to a Taskly account — there is no email delivery and no
Google sign-in, so "your Google email" means the address you registered with.
Inviting an unknown address returns `404` and a message asking the person to
sign up first.

An invite appears as a pending member chip for the owner and as an
**Invitation** card in the invitee's Team Tasks sidebar. The invitee can
accept or decline; accepting makes them an active member and their tasks
become assignable.

### Shared tasks

- Add a task with a deadline, an optional assignee (any active member), and a
  priority.
- Every task has an assignee chip, creator, deadline, priority, category,
  on-time probability, short-lead flag, and comment count, and is grouped by
  the same deadline buckets as My Tasks (Overdue, Today, Tomorrow, Next 7 days,
  Later, No deadline).
- Filters: All / Mine / Open / Done, with live counts.
- A team **Workload assessment** looks for open tasks with deadlines in the
  same 3-day window and compares expected effort with two tasks of capacity per
  active member.
- Open a task to see a details drawer: status toggle, assignee, priority,
  shift reason, deadline, rename, delete, a completion forecast, a short-lead
  alert, a per-task progress log, and threaded updates.
- The same **Focus order** panel appears above the board when several open
  team tasks share a deadline.

### Updates and progress logs

- Updates are threaded: reply to any comment to nest a response.
- Every change (create, complete, reopen, rename, deadline, assign, unassign,
  delete, member join/leave/invite/remove, comments) is written to a
  human-readable progress log, both for the team and per task.
- The team feed keeps the most recent 40 entries; each task thread keeps up
  to 100 comments.

### Real-time sync

The page subscribes to `GET /api/events`, a Server-Sent Events stream. The
server pushes a `team.changed` event to every active member of the affected
team (and to the invitee when they are invited). The client refreshes the
team list, the open board, and the open task thread automatically, and the
**Live** badge in the team header shows the connection state. A heartbeat
comment is sent every 20 seconds to keep the stream open.

## Completion intelligence

Every task can carry a **priority** and an optional **shift reason**. The app
uses those fields, plus the lead time between creation and deadline, to
estimate the chance a task finishes on time and whether its deadline should
move.

### Where the numbers come from

`task_intel.json` is built once from a 5,000-task dataset covering 2025
(`tools/build_intel.py`). It stores only aggregate averages:

- overall delay rate and average actual duration
- five lead-time buckets: **3–4**, **5–7**, **8–10**, **11–14**, and **15–21**
  days
- delay rate and average actual duration per bucket, per priority, and per
  bucket × priority cell
- delay rate, average shift, and extra days when delayed for each of the five
  shift reasons, plus reason × priority cells

The app never sends task data to a model service. The authenticated
`GET /api/intel` endpoint returns this aggregate file, and all scoring happens
in the browser.

### Lead time

Lead days are calendar days from the task's creation date to its deadline.
Tasks without a deadline show **Set deadline** and receive no probability.

### On-time probability

The estimate starts with the bucket × priority delay rate. Small cells are
shrunk toward the bucket/priority average, using `n / (n + 40)`, so a cell
with only a handful of rows cannot dominate.

If a shift reason is selected, the base rate is blended 45% toward that
reason's delay rate (weighted by `n / (n + 20)`), and the reason × priority
cell is shrunk toward the reason's overall rate.

```text
delayed = base_rate * (1 - reason_weight) + reason_rate * reason_weight
on_time = 1 - delayed
```

The row pill and the forecast dialog show the rounded percentage.

### Expected actual days and deadline advice

Expected duration is the bucket × priority average actual duration, plus a
risk premium when a reason is selected:

```text
expected = base_duration + avg_extra_when_delayed * reason_delay_rate
```

The advice compares expected duration with allocated lead days:

- expected exceeds lead by 0.5 days or more → **add** the rounded difference
- expected is 0.5 days or more below lead → **trim** the rounded difference
- otherwise → **keep** the deadline

When the lead is outside the observed 3–21 day range, the nearest bucket is
used and the dialog says so explicitly.

### Short-lead alert and penalty

A task has a **short lead** when the allocated lead is less than the expected
actual duration by more than half a day. The UI shows a **Short lead** pill in
the row, focus order, forecast dialog, and team drawer. A task that is due today
and was created today has no lead time and is flagged the same way.

The base on-time probability is adjusted with a completion-time curve. Let
`L` be the allocated lead and `E` the expected actual days. The multiplier is
normalised so a lead equal to `E` leaves the base probability unchanged:

```text
factor = (1 - exp(-L / E)) / (1 - exp(-1))     when L < E
on_time = base_on_time * factor
```

The dialog reports both the original and adjusted odds. An overdue task has
`L <= 0`, so its on-time probability is near zero.

### Automatic categories

Every task gets one of eight categories from keywords in its title:

- Data Engineering & AI
- DevOps & Infrastructure
- Finance & Compliance
- Marketing & Content
- Operations
- Product Design
- Quality Assurance
- Software Engineering

The classifier scores weighted keywords (specific phrases beat generic words)
and uses **Operations** as the fallback. The server recomputes the category
from the title on every save and team-task update, so renaming a task
reclassifies it automatically. Categories appear as pills in rows and details
views.

### Workload assessment

The workload panel clusters open tasks whose deadlines fall inside any 3-day
window and picks the busiest cluster. It sums each task's expected actual days
as **effort** and compares that with **capacity**:

```text
capacity = 3 days * parallel task capacity
personal parallel capacity = 2 tasks
team parallel capacity     = 2 tasks per active member (minimum 2)
```

- **Heavy load** — task count is at least capacity + 2, or effort is at least
  1.5× capacity
- **Watch** — task count is at least capacity + 1, or effort is at least 0.8×
  capacity
- **Balanced** — otherwise

The panel shows the task count, expected effort, capacity, and load multiple.
On a team board it also names the assignee holding the most tasks in the
cluster.

### Shift reasons

The model supports five reasons:

- Scope Expansion
- Client Feedback Delay
- Technical Roadblock
- Dependency Block
- Resource Reallocation

Each is delayed roughly 44–52% of the time and runs about 1.9–2.0 extra days
past the revised deadline when it does slip. Selecting a reason both lowers
the on-time odds and raises expected duration.

### Same-day focus order

When two or more open tasks share a deadline, a **Focus order** panel lists
them in completion order:

1. **Urgent**, then **High**, then **Medium**, then **Low**
2. lowest on-time probability first within the same priority
3. oldest task first when probability ties

The panel shows the top four colliding dates; open a row to jump to its
forecast (My Tasks) or its details drawer (Team Tasks).

### Rebuilding the model

Only needed if the source spreadsheet changes:

```sh
pip install openpyxl
python tools/build_intel.py "C:\path\to\To-Do List DataSet.xlsx"
```

The server reads `task_intel.json` at startup; no Python packages are needed
at runtime. If the file is missing, the API returns `503` and the UI shows
forecast data as unavailable.

## Saving

Edits are sent to the server shortly after you make them (debounced ~350 ms).
The footer shows the save state:

- **Saving…** — a request is in flight
- **Saved** — the server confirmed the write
- **Not saved** — something went wrong; a toast explains why

If your session expires while you are editing, the app returns to the sign-in
screen and asks you to sign in again.

## Bringing over old local tasks

Earlier versions of Taskly stored tasks in `localStorage` under
`taskly.tasks.v1`. If that data exists on this device and the account you sign
into has no tasks yet, a banner offers to import it. Importing copies those
tasks into your account; dismissing the banner remembers the choice for that
account only. Nothing is imported automatically.

## HTTP API

All endpoints return JSON. Authenticated endpoints use the session cookie.

| Method | Path            | Body                    | Notes                              |
| ------ | --------------- | ----------------------- | ---------------------------------- |
| POST   | `/api/register` | `{email, password}`     | Creates an account and signs in    |
| POST   | `/api/login`    | `{email, password}`     | Signs in                           |
| POST   | `/api/logout`   | —                       | Deletes the session                |
| GET    | `/api/me`       | —                       | Current user, or 401               |
| GET    | `/api/tasks`    | —                       | Current user's tasks               |
| PUT    | `/api/tasks`    | `{tasks: [...]}`        | Replaces the current user's tasks  |
| GET    | `/api/intel`    | —                       | Aggregate 2025 forecast model      |
| GET    | `/api/teams`    | —                       | Your teams plus pending invitations |
| POST   | `/api/teams`    | `{name}`                | Creates a team; you become owner    |
| GET    | `/api/teams/{id}` | —                     | Team, members, tasks, and event feed |
| POST   | `/api/teams/{id}/invite` | `{email}`      | Owner only; email must have an account |
| POST   | `/api/teams/{id}/invitations/accept` | — | Accepts your pending invite |
| POST   | `/api/teams/{id}/invitations/decline` | — | Declines your pending invite |
| POST   | `/api/teams/{id}/leave` | —               | Members only                       |
| DELETE | `/api/teams/{id}/members/{userId}` | — | Owner removes a member or invite |
| POST   | `/api/teams/{id}/tasks` | `{text, due, priority, reason, assignee_email}` | Adds a shared task |
| PUT    | `/api/teams/{id}/tasks/{taskId}` | partial fields | Status, text, due, priority, reason, assignee |
| DELETE | `/api/teams/{id}/tasks/{taskId}` | —    | Owner or task creator              |
| GET    | `/api/teams/{id}/tasks/{taskId}/thread` | — | Task, events, and comments |
| POST   | `/api/teams/{id}/tasks/{taskId}/comments` | `{body, parent_id}` | Threaded update |
| GET    | `/api/events`   | —                       | Server-Sent Events stream          |

A task looks like:

```json
{
  "id": "t...",
  "text": "Ship the release",
  "done": false,
  "due": "2026-11-12",
  "priority": "High",
  "reason": "Scope Expansion",
  "category": "Software Engineering",
  "createdAt": 1794300000000,
  "subtasks": [
    { "id": "s...", "text": "Write notes", "done": true }
  ]
}
```

`priority` is one of `Urgent`, `High`, `Medium`, or `Low`. `reason` is `null`
or one of the five shift reasons. `category` is derived from the title on the
server and is always one of the eight categories above; clients do not need to
send it. Invalid priority or reason values are rejected with `400`.

Limits: 500 tasks, 200 subtasks per task, 500 characters per item, 2 MB per
request.

A team task looks like:

```json
{
  "id": "g4cd73d99788b8f05",
  "text": "Prepare the launch deck",
  "done": false,
  "due": "2026-11-14",
  "priority": "Urgent",
  "reason": "Dependency Block",
  "category": "Marketing & Content",
  "assignee_email": "teammate@example.com",
  "created_by_email": "owner@example.com",
  "created_at": 1794366797,
  "updated_at": 1794366797,
  "comment_count": 2
}
```

An event on the live stream looks like:

```text
data: {"type":"team.changed","team_id":4,"task_id":"g4cd73d99788b8f05","kind":"task_updated","actor":"teammate@example.com"}
```

Team limits: team name 1–80 characters, task text 1–500, comment 1–2000,
40 recent events per team, 100 comments per task thread.
