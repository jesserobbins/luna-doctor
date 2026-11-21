# Reading the chart

`luna-doctor chart` writes one SVG and one PNG of the same scene. The bare
`luna-doctor` run draws the last hour the same way.

## Two stacks, one clock

One plot area, one time axis, and two stacks with a scale each: one per
scheduling class. The priority stack grows from the page's bottom axis. The
background stack sits in the region above it, on its own baseline, with a clear
gap between the two. Each stack's height at any moment is that class's concurrent
inference requests in flight, rebuilt from these logs. Each band is the average
over its column, not the column's maximum. So the height is honest load and never
the sum of peaks that did not co-occur. Other machines on the account are
invisible here, so the height is a lower bound.

Two stacks exist because the classes are two populations. Priority scheduling is
the work someone is waiting on, and the account's priority concurrency is the
number it runs against. Background scheduling is batch work, admitted as capacity
allowed, and it runs at its own slower level against its own number. One stack
added them into a height that no limit governs, so a busy batch window read as a
busier priority one. The two stacks share the clock, so a burst in one class
lines up with what the other class was doing at that moment. They are never
drawn as two pictures side by side, because that breaks the alignment. They are
never summed, and neither region borrows the other's scale.

## What a band is

A band is one model's traffic from one agent in one scheduling class, with one
colour everywhere. Blues are priority scheduling, the class that someone is
actively waiting on. Violets are background scheduling, which uses the
`-background` model-id form. A violet band name ends `(bg)`. The folded
`other (N models)` band keeps its own fixed tone, slate for priority and pale
violet for background. So a reader reads that band by its label, not by its hue.
Red is used only for flare ticks.

Priority bands are in the lower stack and background bands in the upper one, so a
class never shares a stack with the other. Within its own stack the order is peak
thickness, then volume, then name. The agent joins the name (`glm-5.3 · pi`)
when more than one agent has traffic. Up to four priority bands and three
background bands are named. The rest fold into one `other` band per agent per
class.

## The marks

The grey outline is the busiest moment in each column, for its own stack: the
envelope of that class's total. Per-column averages cannot show a burst, so the
envelope carries the peaks that the areas lose. Where the envelope rides above
the areas, that column peaked above its average. An open circle marks each
stack's exact peak and, in its hover title, the class and the time of that peak:
`peak 14 priority concurrent inference requests · 09:41 UTC`.

Each region is captioned at its top right, in its own ink: `priority scheduling ·
concurrent requests`, and `background scheduling · concurrent requests · account
limit 12` when the account reports the background number. The two sets of numbers
in the left gutter are two scales, and the captions say which is which.

A red flare tick hangs from the top edge of the region its own class owns, at
the moment that a request died. The tick reads `rejected at admission`, `rejected:
concurrency limit`, or `overflowed to background scheduling`. That last one sits
at the start of the request, and it hangs in the background region because that is
the class it joined. Its hover title names the class, the agent, the model and how
long the request waited. Ticks clustered at one x mark the moment worth explaining.

The capacity rule is the account's priority concurrency, drawn only when the
number is established, in the priority region against the priority stack. When
the account reports a background concurrency, that number gets its own rule in the
background region against the stack it governs, and a background number of zero
rides in the region caption instead of on the baseline. The mark carries the
provenance. A number the account
reported is a thick solid line, in the strongest ink of the picture. A number
declared with `--plan` is a thin dashed line. A fetched line reads `priority
concurrency 10 (account, plan teams)`,
with ` · stale cache` appended when the cache is old. A declared line reads
`priority concurrency (--plan max = 4)` when the account had no number. The
label names the source as well, so a fetched number can never be mistaken for a
declared one. The account is the first authority. When a `--plan` contradicts
it, the plan is set aside and the footer says so. The number belongs to one
account, not to the gateway. With nothing established, no rule is drawn, and
the footer says that `luna-doctor capacity` fetches the account's number.

## Labels and the footer

Every band is named at its right-hand edge, in its own ink, at the height where
it left the stack. A hairline leader joins the name when the band ended early.
There is no legend box.

Two footer rows, and no more. The first gives the rule and where its number came
from, or why none is established. The second gives the one reading that matters:
whether the priority peak stayed within the priority concurrency or exceeded it,
and how many requests were rejected when any were. A row wraps when it runs past
the canvas, so a long plan name never loses its line.

The header dates the picture at the right of the title, as `generated <date>
<time> UTC`. A picture of a past window is only honest with its own date on it.

Everything else this band used to carry left the picture. `luna-doctor analyze`
reports each model's admission deadline with its count, in the `admission wall`
row. The same report gives the account's day counters beside the local traffic,
and its saturation bands say whether latency rises at the ceiling. The heatmap's
reading guide is gone, because the marks carry the shape. For the story of one
request, run `luna-doctor explain`. The rule that a request waiting for
admission counts as open until it is served or rejected still holds. The marks
show it.

The chart draws no universal admission deadline, because that deadline is per
model and `analyze` reports each measured one with its count, in the
`admission wall` row. It draws no advisory concurrency figure either, because
advice depends on the account and belongs to `analyze` and `fix`, not to a
ruler here.

The latency field is not one of those two, and it did not leave. Every window
draws it, with its completion bars, above the stacks. The window width sets the
column, so a one-hour picture and a seven-day picture show the same field at
different resolutions. It traces a p50 and a p90 for each scheduling class, in
that class's own ink, so the batch class's slower level shows beside the realtime
level and never averages into it. For one request's own wait, run `luna-doctor
explain`.

## How this playbook ends

The picture shows which bands piled up, for whom, in which class, and whether the
priority stack crossed the account's priority concurrency as requests were
rejected.

Read the `reading:` row before you interpret anything. When the stack never comes
near the capacity rule and no flare ticks hang over the burst, concurrency is not
the problem. Say it plainly. Then look for the wait in `analyze` and `explain`
instead.
