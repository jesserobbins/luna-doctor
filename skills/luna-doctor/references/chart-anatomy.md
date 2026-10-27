# Reading the chart

`luna-doctor chart` writes one SVG and one PNG of the same scene. The bare
`luna-doctor` run draws the last hour the same way.

## One stack

One plot area, one time axis, one vertical scale, one stack. The stack's height
at any moment is the concurrent inference requests in flight, rebuilt from these
logs. The account's priority concurrency is read against that height. Each band
is the average over its column, not the column's maximum. So the height is honest
load and never the sum of peaks that did not co-occur. Other machines on the
account are invisible here, so the height is a lower bound.

One stack exists because the total in flight is one number. A frame per agent
made each frame thinner and left the total visible in only one of them. So
attribution moved into the band names.

## What a band is

A band is one model's traffic from one agent in one scheduling class, with one
colour everywhere. Blues are priority scheduling, the class that someone is
actively waiting on. Violets are background scheduling, which uses the
`-background` model-id form. A violet band name ends `(bg)`. The folded
`other (N models)` band keeps its own fixed tone, slate for priority and pale
violet for background. So a reader reads that band by its label, not by its hue.
Red is used only for flare ticks.

Every priority band sits below every background band. Within a class the order is
peak thickness, then volume, then name. The agent joins the name (`glm-5.3 · pi`)
when more than one agent has traffic. Up to four priority bands and three
background bands are named. The rest fold into one `other` band per agent per
class.

## The marks

The grey outline is the busiest moment in each column: the envelope of the
stack's total. Per-column averages cannot show a burst, so the envelope carries
the peaks that the areas lose. Where the envelope rides above the areas, that
column peaked above its average. An open circle marks the exact peak and, in its
hover title, the time of that peak: `peak 14 concurrent inference requests · 09:41 UTC`.

A red flare tick hangs from the top edge at the moment that a request died. The
tick reads `rejected at admission`, `rejected: concurrency limit`, or
`overflowed to background scheduling`. That last one sits at the start of the
request. Its hover title names the class, the agent, the model and how long the
request waited. Ticks clustered at one x mark the moment worth explaining.

The dashed rule is the account's priority concurrency, drawn only when the number
is established. Its label names the source, so a fetched number can never be
mistaken for a declared one. A fetched line reads `priority concurrency 10 (account, plan teams)`,
with ` · stale cache` appended when the cache is old. A declared line reads
`priority concurrency (--plan max = 4)` when the account had no number. The
account is the first authority. When a `--plan` contradicts it, the plan is set
aside and the footer says so. The number belongs to one account, not to the
gateway. With nothing established, no rule is drawn. The footer says that the
number is specific to the account, and it points at `luna-doctor capacity` and at
`chart --plan <plan name or number>`.

## Labels and the footer

Every band is named at its right-hand edge, in its own ink, at the height where
it left the stack. A hairline leader joins the name when the band ended early.
There is no legend box.

Four footer rows wrap to the canvas, which grows downward rather than cutting
provenance. The first row gives which rule is drawn and where its number came
from, or why none is. The `reading:` row gives whether the peak stayed within the
priority concurrency or exceeded it. It also gives how many requests waited for
admission and were rejected. The `measured:` row gives each model's observed
admission deadline with its sample count, or `unknown (n=K)`. It also gives the
client idle budget and the silent stall count. The `bands:` row gives the column
width, the grey outline, the version, the window, and which logs were read. It
also gives the rule that a request waiting for admission counts as open until
served or rejected.

The chart does not draw three marks. One is a universal admission deadline,
because that deadline is per model and the footer reports each measured one with
its count. The second is an advisory concurrency figure, because advice depends
on the account and belongs to `analyze` and `fix`, not to a ruler here. The third
is a latency picture, because latency lives in `analyze`.

## How this playbook ends

The picture shows which bands piled up, for whom, in which class, and whether the
total crossed the account's priority concurrency as requests were rejected.

Read the `reading:` row before you interpret anything. When the stack never comes
near the dashed rule and no flare ticks hang over the burst, concurrency is not
the problem. Say it plainly. Then look for the wait in `analyze` and `explain`
instead.
