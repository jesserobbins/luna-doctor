"""The skill package is prose an agent loads cold and then acts on.

Everything SKILL.md says about the CLI has to be true of the installed CLI,
because the agent reading it will type those commands: a flag the skill
documents and the parser does not have is a dead end in the middle of a
diagnosis. The package is also loaded by reading SKILL.md alone, so a reference
file it never links is a file no agent ever opens, a playbook with no ending
leaves the diagnosis hanging, and frontmatter too long to read or a skill too
long to load is a package that does not get used at all.

Nothing here runs a live command: the CLI is asked for `--help` and nothing
else, so the guard is offline and costs one process per command.
"""

import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
SKILL_DIR = os.path.join(ROOT, "skills", "luna-doctor")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES_DIR = os.path.join(SKILL_DIR, "references")
PLAYBOOKS_DIR = os.path.join(REFERENCES_DIR, "playbooks")

# Every subcommand the skill's command index speaks for. The bare run (no
# subcommand) is the tenth target and is spelled "".
COMMANDS = ("discover", "explain", "watch", "analyze", "fix", "chart",
            "capacity")

# argparse's own help pair: present everywhere, documented nowhere.
IGNORED_FLAGS = ("-h", "--help")

# A long flag as a reader types it. The lookbehind keeps the second half of a
# `--online|--offline` cell from being read as one word.
FLAG = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]*)")
# The tool's own name, not the directory `~/.luna-doctor/` or the image in cwd.
MENTION = re.compile(r"(?<![\w./-])luna-doctor\b")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+\S")
ENDS = re.compile(r"\bends?\b", re.I)


def markdown_files(directory):
    """Every markdown file under `directory`, in a stable order."""
    found = []
    for base, _dirs, names in os.walk(directory):
        found.extend(os.path.join(base, n) for n in sorted(names) if n.endswith(".md"))
    return sorted(found)


def relative(path):
    return os.path.relpath(path, ROOT)


def help_text(command):
    """(returncode, stdout, stderr) of `luna-doctor [command] --help`."""
    argv = [sys.executable, CLI] + ([command] if command else []) + ["--help"]
    env = dict(os.environ)
    env["NO_COLOR"] = "1"          # one plain text, whoever runs the suite
    return subprocess.run(argv, capture_output=True, text=True, timeout=180,
                          cwd=ROOT, env=env)


def documented_invocations(path):
    """(lineno, command, flag) for every invocation the file documents.

    A documented invocation is `luna-doctor <command> ... --flag`, or the same
    with no command (the bare run). The tool's output lines
    (`luna-doctor 0.2.0-alpha.3 (log schema 4)`) and prose
    (`luna-doctor does not edit files`) are neither, and are skipped: the word
    after the tool's name is how the two are told apart.
    """
    found = []
    with open(path, encoding="utf-8") as fh:
        for no, line in enumerate(fh, 1):
            for m in MENTION.finditer(line):
                # A backticked span ends at the backtick, not at a space, so
                # the backticks become spaces before the words are split.
                rest = line[m.end():].replace("`", " ")
                words = rest.split()
                if not words:
                    continue
                if words[0] in COMMANDS:
                    command = words[0]
                elif words[0].startswith("-"):
                    command = ""
                else:
                    continue
                for flag in FLAG.findall(rest):
                    found.append((no, command, flag))
    return found


class SkillPackage(unittest.TestCase):
    def test_flags_exist_in_the_cli(self):
        """No file may document a flag its own command's help does not carry."""
        help_by_command = {}
        for command in ("",) + COMMANDS:
            proc = help_text(command)
            spoken = f"luna-doctor {command}".strip()
            self.assertEqual(proc.returncode, 0,
                             f"`{spoken} --help` exited {proc.returncode}: {proc.stderr.strip()}")
            help_by_command[command] = set(FLAG.findall(proc.stdout)) - set(IGNORED_FLAGS)
            self.assertTrue(help_by_command[command],
                            f"`{spoken} --help` advertised no flags: nothing was parsed")

        seen = 0
        for path in markdown_files(SKILL_DIR):
            for no, command, flag in documented_invocations(path):
                if flag in IGNORED_FLAGS:
                    continue
                seen += 1
                spoken = f"luna-doctor {command}".strip()
                self.assertIn(flag, help_by_command[command],
                              f"{relative(path)}:{no} documents `{spoken} {flag}`, "
                              f"which `{spoken} --help` does not offer")
        self.assertTrue(seen, f"no documented invocation found under {SKILL_DIR}: "
                              "the scan proves nothing")

    def test_every_reference_is_linked_and_every_link_resolves(self):
        """Routing runs one way and completely: no dangling link, no orphan."""
        with open(SKILL_MD, encoding="utf-8") as fh:
            text = fh.read()
        linked = set()
        for target in LINK.findall(text):
            if "references/" not in target:
                continue
            path = os.path.normpath(os.path.join(SKILL_DIR, target.split("#")[0]))
            self.assertTrue(os.path.isfile(path),
                            f"SKILL.md links {target}, which is not a file")
            linked.add(path)

        self.assertTrue(linked, "SKILL.md links no reference file: the routing table is gone")
        shipped = markdown_files(REFERENCES_DIR)
        self.assertTrue(shipped, f"no markdown found under {REFERENCES_DIR}: "
                                 "the scan proves nothing")
        orphans = sorted(set(shipped) - linked)
        self.assertEqual(orphans, [],
                         "unlinked reference files, which no agent opens: "
                         + ", ".join(relative(p) for p in orphans))

    def test_frontmatter_and_budget(self):
        """The skill loads or it does not: name, one-line description, budget."""
        with open(SKILL_MD, encoding="utf-8") as fh:
            text = fh.read()
        lines = text.splitlines()
        self.assertEqual(lines[0].strip(), "---",
                         "SKILL.md must open with a YAML frontmatter block")
        self.assertIn("---", lines[1:], "SKILL.md's frontmatter block is never closed")
        end = lines.index("---", 1)
        # One `key: value` per line, so a multi-line description cannot be
        # assembled here: its value would be the block marker, not "Use when".
        fields = {}
        for line in lines[1:end]:
            if ":" in line and not line.startswith((" ", "\t")):
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()

        self.assertEqual(fields.get("name"), os.path.basename(SKILL_DIR),
                         "the skill's name must equal its directory name")
        description = fields.get("description", "")
        self.assertTrue(description.startswith("Use when"),
                        f"the description must start with 'Use when', not {description[:40]!r}")
        self.assertLess(len(description), 1024,
                        f"the description is {len(description)} chars, past the 1024 budget")

        self.assertLessEqual(len(lines), 150,
                             f"SKILL.md is {len(lines)} lines, past the 150 budget")
        words = len(text.split())
        self.assertLess(words, 1500, f"SKILL.md is {words} words, past the 1500 budget")

    def test_playbooks_have_a_shape(self):
        """A playbook that does not say how it ends is a dangling diagnosis."""
        playbooks = markdown_files(PLAYBOOKS_DIR)
        self.assertTrue(playbooks, f"no playbook found under {PLAYBOOKS_DIR}: "
                                   "the scan proves nothing")
        for path in playbooks:
            rel = relative(path)
            with open(path, encoding="utf-8") as fh:
                body = [(no, line) for no, line in enumerate(fh.read().splitlines(), 1)
                        if line.strip()]
            self.assertTrue(body, f"{rel} is empty")
            self.assertIsNotNone(HEADING.match(body[0][1]),
                                 f"{rel} does not open with a heading")
            headings = [(no, line) for no, line in body if HEADING.match(line)]
            last_no, last_heading = headings[-1]
            self.assertIsNotNone(ENDS.search(last_heading),
                                 f"{rel} does not close on a section saying how the "
                                 f"playbook ends (its last heading is {last_heading!r})")
            self.assertGreater(body[-1][0], last_no,
                               f"{rel} ends on the heading {last_heading!r} with "
                               "nothing written under it")
    def test_every_relative_link_resolves_in_every_file(self):
        """A reference file that links the wrong path sends the agent nowhere.

        SKILL.md's own links are checked above; the files it opens link to each
        other, and a link written from the wrong directory resolves to a path
        that does not exist while looking correct in the text.
        """
        scanned = 0
        for path in markdown_files(SKILL_DIR):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for target in LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                scanned += 1
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), target.split("#")[0]))
                self.assertTrue(
                    os.path.isfile(resolved),
                    f"{relative(path)} links {target!r}, which resolves to "
                    f"{relative(resolved)} and is not a file")
        self.assertGreater(scanned, 0, "no relative link found: the scan proves nothing")

    def test_no_invented_command_name(self):
        """A command the parser does not have is a dead end mid-diagnosis.

        `probe` was the load test's old name and is retired: naming it in the
        skill hands the agent a command it cannot type. Any single-token
        `luna-doctor <word>` in the prose is a command reference, so the word
        has to be a subcommand the CLI actually serves.
        """
        command_mention = re.compile(r"`luna-doctor ([a-z][a-z0-9-]*)`")
        for path in markdown_files(SKILL_DIR):
            rel = relative(path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("probe", text,
                             f"{rel} names `probe`, which is the load test's retired "
                             "name and not a command")
            for word in command_mention.findall(text):
                self.assertIn(word, COMMANDS,
                              f"{rel} names `luna-doctor {word}`, which is not a "
                              "subcommand the CLI serves")


if __name__ == "__main__":
    unittest.main()
