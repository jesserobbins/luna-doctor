import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        for rel in ("luna_doctor/cli.py", "luna_doctor/_fix_block.py",
                    "luna_doctor/_chart_block.py", "README.md",
                    "skills/luna-doctor/SKILL.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(self.FORBIDDEN.search(text), f"{rel} still states the cap as fact")


if __name__ == "__main__":
    unittest.main()
