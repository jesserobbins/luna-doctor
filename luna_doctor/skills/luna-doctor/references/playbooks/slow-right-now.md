# Slow, or hanging, right now

## When this applies

The user says it is happening now: a session that went quiet, a call that went
on for minutes, a stream that stopped mid-answer. Measure the last few minutes
before you offer any theory, because more evidence is still arriving.

The error string in their hand is a client string. It says which request to
explain, not whose wall that request hit. The report tells you that.

## The loop

1. Measure with `luna-doctor analyze --now --style plain`. `--now` is the last
   15 minutes. `--style plain` prints the undecorated `label: value` rows. Those
   rows survive a pipe, a log file and a narrow window.
2. Read the attribution section first. It renders at the top. The row
   `the wall you hit` carries one verdict word: `you (this machine)`,
   `your org (the account)`, `the gateway`, `not concurrency`, or `unknown`.
   The row under it, `why`, is the headline that led to that word. Quote the
   word as printed, and quote it whole. Quote "the gateway", not "gateway
   trouble". Do not upgrade `unknown` into a diagnosis.
3. Present the findings, then the advice. Give the window, the verdict word,
   and the two or three rows that carry it. Quote the numbers. Do not
   paraphrase them into adjectives. Advice that arrives before the evidence
   reads as a guess, and the user cannot test a guess.

## Widening: once, and out loud

The window is the one dial you have. It only ever turns backward in time: the
last 15 minutes, the last hour, 24 hours, 7 days, then an explicit `--since`.

- Say which window, and say why. "Nothing in the last 15 minutes. Here is the
  hour instead." Every report states its own window. Your message states why
  you showed that window.
- A substituted window uses up its rung. If you widened to the last hour
  yourself, the hour is spent. Do not then offer to widen to the hour. Offer
  the next step, or stop.
- Revision is explicit. The wider window can change the narrower story: a quiet
  15 minutes, an hour with the stall inside it. Say what changed and when it
  changed. The user answered once. The user must not have to read two reports
  side by side to notice that the story moved.
- One decision point. The only question you ask is "go farther back?". Ask
  nothing else mid-analysis. The window is a dial, not an interview.
- A non-interactive caller gets one window and no ladder. From a script, a cron
  job or a CI step, run the window you were given, print it, and stop. Nobody
  is there to answer "go farther back?".

## The other instruments

`explain` answers for one request. Use it when the user names a request or
holds one bad result. Its default selection is the request that went wrong.
`--blocked` gives the newest request that the gateway refused. The pair
`--slowest --last 5` sorts by duration only. A bare id can be a response,
session or run id. It takes `--since` or `--now` like `analyze`. A stall and a
refusal are different events with different fixes. `one-bad-request.md` keeps
them apart.

`watch` prints the live line. Use it while the incident is still moving and the
user wants to know whether it is still happening. It prints one line per state
change, a 15-minute window per sample, 10 seconds apart by default. `Ctrl-C`
prints a three-line summary and exits with the last sample's verdict. The flag
`--count N` stops after N samples, and `--once` prints a single one. It reads
logs and sends nothing anywhere.

`chart` draws the picture. Run `luna-doctor chart --since <the same window>`. It
writes an SVG and a PNG in the working directory. `--no-image` writes the SVG
alone. Use it when the story is shape rather than amount. Bands that pile at
the same x as a cluster of flare ticks close in one picture an argument that
three paragraphs can reopen. When the stack never approaches the dashed rule,
say so plainly. Concurrency is not what this user is hitting. Add `--plan` only
when the user declared one. The rule is labelled with its own source.

## When the window holds nothing

`analyze` exits `2` and prints `No requests found in the window.` with one
adjustment: widen with `--since`, or run `luna-doctor discover`. Exit `2` means
nothing was measured. It is not the same answer as exit `0`. Exit `0` means the
window was measured and held no pressure. Never report the empty window as "no
pressure".

Say the window was empty and widen once. If the wider window is empty too, run
`luna-doctor discover` and report what stores exist. A machine with no store
the tool can read and a machine that was quiet are two answers. Only `discover`
tells them apart.

`discover` also reports a local `agentsview`, when that command is installed. It
is a second view of session history: an index of the session files of several
harnesses, in SQLite. The tool reads it with one `agentsview projects --json`
query. It never starts a daemon, never serves and never syncs, because a
diagnostic run must not change the data it reads. A row that reads
`present · unreadable (...)` means the command is installed and did not answer.
Read it as "no second view this time", never as "no sessions existed".
When the local logs are empty and agentsview reports sessions, the logs were
rotated or moved. Say that, and ask for the store root with `--pi-root`.

## How this playbook ends

The answer has four parts, in this order.

- The window. "The last 15 minutes", or the wider window you substituted and
  the reason you substituted it.
- The verdict word. One of the five, printed as the report prints it, with the
  `why` headline beside it.
- The one change. The single adjustment the report argues for. It is the width
  this machine's traffic must stay within, or the same model's `-background`
  variant. Use the variant for the work that does not need to be quick. One
  change, not a list. The exact configuration key for the user's agent comes
  from `luna-doctor fix`.
- The verification step. Look at these afterwards. Restart the harness so the
  key is read. Then run `analyze --baseline prev` over a window wide enough to
  clear its sample floor. Watch `blocked at admission` come down. See
  `after-the-fix.md` for the restart-read keys and the floor.

A number never travels alone in that answer. A peak is this machine's. A
priority concurrency is the account's or the declared plan's. Both print with
their owner.

When the verdict word is `not concurrency` or `unknown`, the ending is that
sentence plus the step that settles it. Do not invent a change to fill the slot.
