"""The prose a reader sees must stay in Simple English.

The rules come from ASD-STE100, the controlled language used for maintenance
manuals: short sentences, active voice, simple tenses, one word one meaning. The
checker that finds the machine-visible violations is
`scripts/check_simple_english.py`, and this test runs it over the surfaces a
person reads: the README, every skill file, and the newest CHANGELOG section.

Two exemptions, both deliberate.

Frozen CHANGELOG history is not rewritten. A release note records what was said
at that release, so this test reads the newest section only.

Code, flags, paths, quoted tool output and link targets are exempt by
construction inside the checker, not here.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "scripts", "check_simple_english.py")

_spec = importlib.util.spec_from_file_location("check_simple_english", CHECKER)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

README = os.path.join(ROOT, "README.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
SKILL_DIR = os.path.join(ROOT, "skills", "luna-doctor")


def newest_changelog_section():
    """The text under the newest released heading, without the older sections."""
    with open(CHANGELOG, encoding="utf-8") as fh:
        text = fh.read()
    start = text.find("\n## [", text.find("## [Unreleased]") + 1)
    if start < 0:
        return ""
    rest = text[start + 1:]
    nxt = rest.find("\n## [", 1)
    return rest if nxt < 0 else rest[:nxt]


def violations(text):
    """(rule, match) pairs for one block of prose."""
    body = checker.prose(text)
    out = []
    for rule, pattern, _fix in checker.CHECKS:
        out.extend((rule, m.group(0)) for m in pattern.finditer(body))
    return out


def markdown_under(path):
    found = []
    for base, _dirs, names in os.walk(path):
        found.extend(os.path.join(base, n) for n in sorted(names) if n.endswith(".md"))
    return sorted(found)


class SimpleEnglish(unittest.TestCase):
    def test_readme_is_simple_english(self):
        with open(README, encoding="utf-8") as fh:
            found = violations(fh.read())
        self.assertEqual(
            found, [],
            "README.md breaks Simple English: "
            + ", ".join(f"{rule} {match!r}" for rule, match in found[:8]))

    def test_every_skill_file_is_simple_english(self):
        files = markdown_under(SKILL_DIR)
        self.assertTrue(files, f"no markdown under {SKILL_DIR}: the scan proves nothing")
        broken = {}
        for path in files:
            with open(path, encoding="utf-8") as fh:
                found = violations(fh.read())
            if found:
                broken[os.path.relpath(path, ROOT)] = found[:5]
        self.assertEqual(
            broken, {},
            "skill files break Simple English: " + repr(broken))

    def test_newest_changelog_section_is_simple_english(self):
        section = newest_changelog_section()
        self.assertTrue(section.strip(), "no released section found in CHANGELOG.md")
        found = violations(section)
        self.assertEqual(
            found, [],
            "the newest CHANGELOG section breaks Simple English: "
            + ", ".join(f"{rule} {match!r}" for rule, match in found[:8]))


if __name__ == "__main__":
    unittest.main()
