"""luna-doctor: explain what your local agent traffic does to your
LunaRoute concurrency, and what to adjust.

`cli` holds everything shared: the dataclasses, log readers, the analyser and
the measured constants. `_fix_block` and `_chart_block` are the two commands
kept in their own files so their snippets can be reviewed and verified on their
own; `cli` executes them in its own namespace so they share its globals.
"""
