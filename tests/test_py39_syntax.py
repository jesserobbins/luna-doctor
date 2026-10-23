"""Guard: the package must parse on Python 3.9, the floor of requires-python.

0.1.1-alpha.2 shipped scene.py emitters whose f-strings only Python 3.12+
can parse (PEP 701), so every command died at import on an older
interpreter with SyntaxError. Three guards keep that from regressing, in
order of how much they model rather than measure:

1. `ast.parse(..., feature_version=(3, 9))` over every .py file -- catches
   grammar the ast module versions explicitly (match, except*, ...).
2. A tokenizer sweep -- catches PEP 701 f-strings. Guard 1 misses those on
   a 3.12+ host: feature_version does not revert the f-string tokenizer, so
   the unfixed scene.py parses "as 3.9" on a new Python. The tokenizer
   still exposes the pieces PEP 701 added (FSTRING_* tokens), so a line
   break or same-quote reuse inside a single-quoted f-string is visible
   wherever the host parser would otherwise stay silent. On hosts older
   than 3.12 this sweep is a no-op: those interpreters reject the code
   themselves.
3. When a real Python 3.9-3.11 interpreter is installed, compile the
   package with it -- the only ground-truth check, skipped when absent.

Guards 1 and 2 each have a meta-test proving the guard would actually
fire rather than asserting nothing; guard 3 is ground truth and needs
none. Run this file on any host Python and it still guards.
"""

import ast
import io
import os
import shutil
import subprocess
import tokenize
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("luna_doctor", "scripts")

# the released bug, verbatim in shape: a single-quoted f-string whose
# expression spans lines (scene.py before the fix)
PEP701_BUG_SNIPPET = (
    "add(f'<rect {_attrs((\n"
    "    (\"x\", _num(sh.x)), (\"y\", _num(sh.y)),\n"
    "))}/>')\n"
)


def py_files():
    for name in SOURCE_DIRS:
        root = REPO / name
        for path in sorted(root.rglob("*.py")):
            yield path


def pep701_violations(source):
    """(line, message) for f-string syntax only Python 3.12+ can parse.

    PEP 701 let f-string expressions span lines and reuse the enclosing
    quote; before it, both are SyntaxError. Track the f-strings the
    tokenizer delimits with FSTRING_* tokens and flag:

    - a line break while a non-triple-quoted f-string is open;
    - a string literal whose quote matches the enclosing f-string's
      (only fatal for single-quoted f-strings: a lone " inside f\"\"\"...\"
      is and always was legal).

    Hosts without FSTRING_* tokens predate PEP 701 and reject this code
    at parse time, so there is nothing to check.
    """
    if not hasattr(tokenize, "FSTRING_START"):
        return []
    out, open_fstrings = [], []  # (quote char, is triple-quoted)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_START:
            open_fstrings.append(
                (tok.string[-1], tok.string[-3:] in ('"""', "'''"))
            )
        elif tok.type == tokenize.FSTRING_END:
            if open_fstrings:
                open_fstrings.pop()
        elif tok.type in (tokenize.NL, tokenize.NEWLINE):
            if any(not triple for _, triple in open_fstrings):
                out.append(
                    (tok.start[0],
                     "line break inside a single-quoted f-string "
                     "(PEP 701; needs Python 3.12+)")
                )
        elif tok.type == tokenize.STRING and open_fstrings:
            quote, triple = open_fstrings[-1]
            if not triple and tok.string.lstrip("rRbBuUfF")[-1] == quote:
                out.append(
                    (tok.start[0],
                     "string literal reusing the enclosing f-string's quote "
                     "(PEP 701; needs Python 3.12+)")
                )
    return out


class Py39SyntaxGuard(unittest.TestCase):
    def test_every_module_parses_under_the_3_9_grammar(self):
        offenders = []
        for path in py_files():
            try:
                ast.parse(path.read_text(), filename=str(path),
                          feature_version=(3, 9))
            except SyntaxError as e:
                offenders.append(f"{path}:{e.lineno}: {e.msg}")
        self.assertEqual([], offenders,
                         "post-3.9 grammar in a package that claims >=3.9")

    def test_3_9_grammar_guard_rejects_known_bad_syntax(self):
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1: pass\n",
                      feature_version=(3, 9))

    def test_no_fstring_needs_pep_701(self):
        offenders = []
        for path in py_files():
            source = path.read_text()
            try:
                violations = pep701_violations(source)
            except tokenize.TokenError as e:
                violations = [(e.args[1][0], f"tokenize gave up: {e.args[0]}")]
            for line, message in violations:
                offenders.append(f"{path}:{line}: {message}")
        self.assertEqual([], offenders,
                         "PEP 701 f-string syntax in a package that claims >=3.9")

    def test_pep_701_guard_rejects_the_released_bug_shape(self):
        if hasattr(tokenize, "FSTRING_START"):
            self.assertTrue(pep701_violations(PEP701_BUG_SNIPPET),
                            "guard missed the exact shape that shipped in 0.1.1-alpha.2")
        else:  # pre-3.12 host: the parser itself is the guard
            with self.assertRaises(SyntaxError):
                ast.parse(PEP701_BUG_SNIPPET)

    def test_a_real_3_9_to_3_11_interpreter_compiles_the_package(self):
        interpreter = find_old_interpreter()
        if interpreter is None:
            self.skipTest("no Python 3.9-3.11 interpreter found; "
                          "syntax guards above still ran")
        result = subprocess.run(
            [interpreter, "-m", "compileall", "-q", *SOURCE_DIRS],
            cwd=REPO, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode,
                         f"{interpreter} -m compileall failed:\n{result.stdout}{result.stderr}")


def find_old_interpreter():
    """The oldest real Python 3.9-3.11 interpreter available, else None.

    The oldest interpreter proves the most: it is closest to the floor.
    /usr/bin/python3 is checked by version, not trusted by name, because it
    is whatever the OS shipped.
    """
    candidates = ["python3.9", "python3.10", "python3.11", "/usr/bin/python3"]
    oldest = None
    for candidate in candidates:
        path = shutil.which(candidate) or (
            candidate if os.path.isfile(candidate) else None
        )
        if path is None:
            continue
        version = subprocess.run([path, "--version"], capture_output=True,
                                 text=True)
        parts = version.stdout.split()
        if len(parts) != 2 or parts[0] != "Python":
            continue
        try:
            major, minor = (int(n) for n in parts[1].split(".")[:2])
        except ValueError:
            continue
        if major == 3 and 9 <= minor <= 11:
            if oldest is None or minor < oldest[0]:
                oldest = (minor, path)
    return oldest[1] if oldest else None


if __name__ == "__main__":
    unittest.main()
