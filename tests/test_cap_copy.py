import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, "skills", "luna-doctor")


def skill_markdown():
    """Every markdown file the skill package ships, in a stable order.

    The skill is prose an agent loads cold, so it is copy a user ends up
    reading; narrowing the scan back to SKILL.md would leave the reference
    files - the ones that carry the numbers - unscanned.
    """
    found = []
    for base, _dirs, names in os.walk(SKILL_DIR):
        found.extend(os.path.join(base, n) for n in sorted(names) if n.endswith(".md"))
    return sorted(found)


class NoCapAsFact(unittest.TestCase):
    # The gateway's interactive cap is per account and only the account itself
    # (or a load test) can see it, so no scanned file may state one as that cap
    # - as a fact, a cap clause, or a limit the reader is told to stay under.
    # The width is deliberately not pinned to one number: the earlier pass was
    # too narrow to catch "at or under 10" after it had already shipped "8",
    # and advice that generalises the mistake would generalise past the guard.
    FORBIDDEN = re.compile(
        r"(cap is \d+|burst cap is \d+|cap of \d+|hard cap"
        r"|at or under \d+|cap: \d+|\d+-request cap|limit of \d+"
        r"|keep concurrent requests at or under)", re.I)

    def test_no_file_states_ten_as_a_gateway_fact(self):
        paths = [os.path.join(ROOT, rel) for rel in
                 ("luna_doctor/cli.py", "luna_doctor/_fix_block.py",
                  "luna_doctor/_chart_block.py", "README.md")]
        skill = skill_markdown()
        self.assertTrue(skill, f"no markdown found under {SKILL_DIR}: the scan proves nothing")
        paths.extend(skill)
        for path in paths:
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(self.FORBIDDEN.search(text), f"{rel} still states the cap as fact")


if __name__ == "__main__":
    unittest.main()
