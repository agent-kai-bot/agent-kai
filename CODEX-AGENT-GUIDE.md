# Using OpenAI Codex CLI as a Sub-Agent

A practical guide for AI agents delegating work to `codex exec`.
Written from hard-won experience — every section addresses a real
failure mode encountered during a 7-phase daemon migration.

---

## 1. What codex exec is

`codex exec` runs the OpenAI Codex CLI non-interactively. You give
it a prompt, it reads/writes files, runs shell commands, and exits.
Think of it as a headless coding agent you can script.

```bash
codex exec "your prompt here"
```

It has full access to the filesystem and shell (depending on sandbox
mode), can read your codebase, run tests, make git commits, and
write summary files. The model behind it (currently gpt-5.4) is
very capable with `xhigh` reasoning.

---

## 2. Key flags reference

```bash
codex exec \
  -c model_reasoning_effort=xhigh \          # deep thinking
  --dangerously-bypass-approvals-and-sandbox \ # REQUIRED for git ops
  --skip-git-repo-check \                     # don't fail outside a repo
  --json \                                    # JSONL event stream to stdout
  -o /tmp/codex-final.txt \                   # last message written here
  --add-dir /tmp \                            # extra writable dirs
  -C /path/to/repo \                          # working directory
  - < prompt.md                               # read prompt from stdin
```

### Flags that DO NOT exist on `codex exec`

These are top-level `codex` flags only. Using them on `exec` will
error with `unexpected argument`:

- `-a` / `--ask-for-approval` — **NOT available on exec**
- `-i` / `--image` — exists on exec but rarely needed

### Reasoning effort levels

```bash
-c model_reasoning_effort=low     # fast, shallow
-c model_reasoning_effort=medium  # balanced
-c model_reasoning_effort=high    # thorough
-c model_reasoning_effort=xhigh   # maximum depth, slow, expensive
```

Use `xhigh` for architectural work, refactors, and anything where
getting it wrong has cascading consequences. Use `medium` or `high`
for mechanical tasks like documentation or renaming.

---

## 3. Sandbox modes — the critical gotcha

### `--full-auto` (workspace-write sandbox)

```bash
codex exec --full-auto "..."
```

This is `-a on-request --sandbox workspace-write`. It lets codex
write to the working directory and `/tmp`, **BUT `.git/` IS
READ-ONLY**. This means:

- `git switch -c branch` → **FAILS** (cannot create ref lock)
- `git commit` → **FAILS** (cannot write to `.git/objects`)
- `git add` → **FAILS** (cannot update `.git/index`)
- File edits work fine

**If your task requires git operations (branching, committing),
`--full-auto` will NOT work.** Codex will hit a "Read-only file
system" error when trying to write to `.git/`.

### `--dangerously-bypass-approvals-and-sandbox` (yolo mode)

```bash
codex exec --dangerously-bypass-approvals-and-sandbox "..."
```

Full filesystem access, no approval prompts, no sandbox. This is
**required** for any task that involves git commits, branch
creation, or writing outside the workspace.

Use this when:
- The task requires git operations
- You're running in an already-sandboxed environment
- The task needs to install packages or modify system files

### Rule of thumb

| Task needs git? | Use |
|---|---|
| No (just file edits + tests) | `--full-auto` |
| Yes (commits, branches) | `--dangerously-bypass-approvals-and-sandbox` |

---

## 4. Prompt delivery — stdin for long prompts

Short prompts can be positional args:

```bash
codex exec "Fix the typo in README.md"
```

Long prompts should go through stdin. Write the prompt to a file,
then pipe it:

```bash
codex exec [flags] - < /tmp/my-prompt.md
```

The `-` tells codex to read from stdin. Without it, codex may hang
waiting for interactive input or misparse the prompt.

**Important:** If you don't pass a prompt as an arg AND don't use
`-`, codex will print `Reading additional input from stdin...` and
may block or behave unexpectedly.

---

## 5. Output capture and monitoring

### Three output channels

1. **stdout** — JSONL event stream (with `--json`), or human
   readable text (without). Redirect to a log file:
   ```bash
   codex exec --json ... > /tmp/codex-phase1.log 2>&1
   ```

2. **`-o FILE`** — writes ONLY the last agent message to this file.
   Useful for grabbing the final summary without parsing JSONL.

3. **Summary files** — tell codex in its prompt to write a summary
   to a specific path (e.g. `/tmp/codex-phase1-summary.md`). This
   is the most reliable deliverable.

### Monitoring a running codex process

**Check if alive:**
```bash
ps -p $PID -o pid,etime,stat
```

**Read agent messages from the JSONL log:**
```bash
grep -o '"text":"[^"]*"' /tmp/codex-phase1.log | tail -5
```

**Count events (rough progress indicator):**
```bash
wc -l /tmp/codex-phase1.log
```

**Check for errors:**
```bash
grep -i "error\|rate\|limit\|fail" /tmp/codex-phase1.log | tail -10
```

**Check the last few events:**
```bash
tail -3 /tmp/codex-phase1.log
```

### Codex JSONL event types

Key events in the `--json` stream:

```jsonc
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"agent_message","text":"..."}}
{"type":"item.completed","item":{"type":"command_execution","command":"...","exit_code":0}}
{"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N}}
{"type":"error","message":"Selected model is at capacity..."}
{"type":"turn.failed","error":{"message":"..."}}
```

---

## 6. Auth setup

Codex uses OpenAI auth stored at `~/.codex/auth.json`. Two paths:

### OAuth (ChatGPT subscription)

```bash
codex login
```

Opens a browser for OAuth flow. Tokens stored in `auth.json`. Can
expire or become malformed — if you see `invalid ID token format`,
re-run `codex login`.

### API key

Set `OPENAI_API_KEY` in the environment or in `auth.json`. This
path doesn't need browser auth.

### Diagnosing auth failures

```bash
# Check login status
codex login status

# Check auth.json structure (without exposing secrets)
jq 'keys' ~/.codex/auth.json
jq '.OPENAI_API_KEY | if . == null then "null" else "set" end' ~/.codex/auth.json

# Check last refresh
jq '.last_refresh' ~/.codex/auth.json
```

If `codex login status` errors with `invalid ID token format`,
the fix is always: `codex login` (re-auth via browser).

---

## 7. Common failure modes and fixes

### "Read-only file system" on git operations

**Cause:** Using `--full-auto` which sandboxes `.git/` as read-only.

**Fix:** Switch to `--dangerously-bypass-approvals-and-sandbox`.

### "Selected model is at capacity"

**Cause:** OpenAI rate limit / capacity issue. Exit code 1.

**Fix:** Wait a few minutes, retry. Not a code issue. The work
done before the error is fine — codex just couldn't start a new
turn.

### "invalid ID token format"

**Cause:** Stale or corrupted OAuth token in `~/.codex/auth.json`.

**Fix:** Run `codex login` interactively (requires browser).

### codex hangs on "Reading additional input from stdin..."

**Cause:** No prompt provided and stdin is a TTY.

**Fix:** Either pass the prompt as a positional arg, or use
`- < prompt.md` to pipe it via stdin.

### Exit code 1 with no obvious error

Check the last 3 lines of the JSONL log:
```bash
tail -3 /tmp/codex.log
```

Common causes:
- Model capacity (rate limit)
- Malformed auth token
- Network timeout
- The model decided to stop (unusual but possible)

### `.codex/` directory appears in git status

Codex creates a `.codex/` working directory in the repo root. Add
it to `.gitignore` or tell codex in the prompt not to worry about
it showing up as untracked.

---

## 8. Background execution and chaining

### Single background run

```bash
nohup codex exec [flags] - < prompt.md > /tmp/codex.log 2>&1 &
echo "PID: $!"
```

Monitor with `ps -p $PID` and `tail /tmp/codex.log`.

### Chaining multiple phases

For multi-phase projects, use a supervisor script:

```bash
#!/bin/bash
TEMPLATE=/tmp/codex-phase-template.md

for N in 1 2 3 4 5; do
    echo "$(date) === Phase $N starting ===" >> /tmp/supervisor.log

    # Generate phase-specific prompt from template
    sed "s/{N}/$N/g" "$TEMPLATE" > /tmp/phase${N}-prompt.md

    # Run codex
    codex exec \
        -c model_reasoning_effort=xhigh \
        --dangerously-bypass-approvals-and-sandbox \
        --skip-git-repo-check \
        --json \
        -o /tmp/phase${N}-final.txt \
        - < /tmp/phase${N}-prompt.md \
        > /tmp/phase${N}.log 2>&1

    EXIT=$?

    if [ $EXIT -ne 0 ]; then
        echo "$(date) ABORT: Phase $N exit code $EXIT" >> /tmp/supervisor.log
        exit 1
    fi

    # Verify phase deliverables exist
    if [ ! -f /tmp/phase${N}-summary.md ]; then
        echo "$(date) ABORT: Phase $N summary missing" >> /tmp/supervisor.log
        exit 1
    fi

    echo "$(date) === Phase $N COMPLETE ===" >> /tmp/supervisor.log
done

echo "$(date) ALL PHASES COMPLETE" >> /tmp/supervisor.log
```

Spawn it:
```bash
nohup bash /tmp/supervisor.sh > /tmp/supervisor.stdout 2>&1 &
```

Each phase gets a fresh codex context (no context exhaustion),
phases gate on the previous phase's summary file, and the
supervisor log gives you a one-line-per-phase progress view.

---

## 9. Prompt engineering for codex

### What works well

1. **Point at files to read first.** Codex doesn't know your
   codebase. Tell it which files matter:
   ```
   READ FIRST:
   1. Ui-DAEMON-UP-GRADE.JSON — the plan
   2. tui/terminal.py — the file you're extracting from
   3. agent-config.json — do not modify this
   ```

2. **Numbered constraints.** Codex follows explicit rules well:
   ```
   CONSTRAINTS:
   1. Work on branch kai/daemon-migration. Do NOT push.
   2. One commit per task. Run pytest before each commit.
   3. No Co-Authored-By trailers or AI mentions.
   ```

3. **Stuck = stop rule.** Prevents codex from guessing when it
   should ask:
   ```
   If stuck, write your question to /tmp/blocker.md and EXIT.
   Do not guess. Do not proceed past the blocker.
   ```

4. **Explicit deliverables.** Tell codex exactly what to produce:
   ```
   WHEN DONE:
   - Working tree clean (everything committed)
   - Write /tmp/phase1-summary.md with: tasks done, test
     results, files changed, deviations, commit hashes
   ```

5. **Scope boundaries.** Codex will happily refactor your entire
   codebase if you let it:
   ```
   SCOPE: Phase 1 only. Tasks P1.1 through P1.7.
   Do NOT start Phase 2.
   No scope creep. No fixing unrelated bugs.
   ```

### What doesn't work well

- **Vague prompts.** "Make the code better" → unpredictable results.
- **No file pointers.** Codex will waste tokens exploring instead
  of reading the right files.
- **No stop condition.** Without a stuck=stop rule, codex may
  silently make wrong architectural decisions.
- **Relying on `-a on-request` for unattended runs.** If codex
  asks for approval and nobody is there to answer, it blocks
  forever.

---

## 10. Cost and performance expectations

| Reasoning | Tokens per phase | Wall time (typical) |
|---|---|---|
| `xhigh` | 150k–300k input, 2k–10k output | 15–40 min |
| `high` | 80k–150k input, 2k–8k output | 8–20 min |
| `medium` | 40k–80k input, 1k–5k output | 5–15 min |

These are rough estimates from a 7-phase migration project. Actual
usage depends on codebase size, number of files read, and how many
shell commands codex runs.

With a ChatGPT Pro subscription, codex uses your subscription
allocation (not per-token billing). With an API key, you pay per
token.

---

## 11. Quick-start recipe

Run a one-shot coding task with full autonomy:

```bash
# Write prompt
cat > /tmp/prompt.md << 'EOF'
Read src/main.py and add a --version flag that prints the version
from pyproject.toml. Commit the change with message "Add --version
CLI flag". Run pytest before committing.
EOF

# Fire codex
codex exec \
  -c model_reasoning_effort=high \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --json \
  -o /tmp/result.txt \
  - < /tmp/prompt.md \
  > /tmp/codex.log 2>&1

echo "Exit: $?"
cat /tmp/result.txt
```

---

## 12. Checklist before spawning codex

- [ ] Auth working? (`codex login status`)
- [ ] Prompt written to a file? (not inline for long prompts)
- [ ] Sandbox mode correct? (`--dangerously-bypass...` if git needed)
- [ ] Output paths set? (`--json`, `-o`, `> log.file`)
- [ ] Reasoning effort set? (`-c model_reasoning_effort=...`)
- [ ] Stuck=stop rule in prompt?
- [ ] Deliverable file path in prompt? (summary, blocker)
- [ ] Scope boundaries in prompt? (what to do AND what not to do)
- [ ] Working directory correct? (`-C /path` or run from the right dir)
