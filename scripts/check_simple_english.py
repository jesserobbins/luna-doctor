"""Find prose that breaks the Simple English rules, and say where.

The rules come from ASD-STE100, the controlled language used for maintenance
manuals. This checker holds the rules that a machine can see. It cannot judge
sentence length or a noun chain, so a clean run is a floor, not a pass.

Exempt by construction: fenced code blocks, inline code spans, link targets,
frontmatter, and anything inside a table cell's code span. Identifiers, flags,
paths, product names and quoted errors are never the subject of these rules.
"""

import pathlib
import re
import sys

# Each check is a name, a compiled pattern, and the fix a writer makes.
CHECKS = (
    ("em-dash", re.compile("\u2014"),
     "write two sentences, or name the relation"),
    ("semicolon", re.compile(r";"),
     "write two sentences"),
    ("contraction", re.compile(
        r"\b(don't|doesn't|can't|won't|isn't|aren't|wasn't|weren't|you're|it's|"
        r"that's|there's|here's|we're|they're|I'm|let's|didn't|couldn't|"
        r"shouldn't|wouldn't|hasn't|haven't|hadn't)\b", re.I),
     "write the two words out"),
    ("modal", re.compile(r"\b(should|would|may|might|could)\b", re.I),
     "use can, will or must"),
    ("present-perfect", re.compile(
        r"\b(has|have|had)\s+(been|completed|moved|changed|added|fixed|removed|"
        r"grown|become)\b", re.I),
     "use the simple past"),
    ("ing-after-comma", re.compile(r",\s+\w+ing\b"),
     "write a new sentence"),
    ("filler", re.compile(
        r"\b(simply|seamlessly|robust|powerful|comprehensive|leverage|crucial|"
        r"in order to|it is worth noting|very|really|quite)\b", re.I),
     "delete it, or state the fact"),
    ("bold", re.compile(r"\*\*"),
     "use a heading, a list, or plain words"),
)

FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\([^)]*\)")
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def prose(text):
    """The text a reader reads, with code, link targets and frontmatter removed."""
    text = FRONTMATTER.sub("", text)
    text = FENCE.sub("", text)
    text = CODE.sub("", text)
    text = LINK.sub("]", text)
    return text


def findings(path):
    """Yield (line, rule, fix, match) for one file, in reading order."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    body = prose(text)
    for rule, pattern, fix in CHECKS:
        for match in pattern.finditer(body):
            line = text[:text.find(match.group(0))].count("\n") + 1 if match.group(0) in text else 0
            yield line, rule, fix, match.group(0)


def count(path):
    """Count the findings in one file, by rule."""
    totals = {}
    for _line, rule, _fix, _match in findings(path):
        totals[rule] = totals.get(rule, 0) + 1
    return totals


def main(argv):
    paths = []
    for arg in argv:
        target = pathlib.Path(arg)
        paths.extend(sorted(target.rglob("*.md")) if target.is_dir() else [target])
    broken = 0
    for path in paths:
        totals = count(path)
        total = sum(totals.values())
        broken += total
        if total:
            print(f"{str(path):58s} {total:4d}  {totals}")
    print(f"\n{len(paths)} file(s), {broken} finding(s)")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
