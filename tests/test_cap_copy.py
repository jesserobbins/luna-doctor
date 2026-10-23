import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NoCapAsFact(unittest.TestCase):
    # The gateway's interactive cap is per account and only a probe can see
    # it, so no scanned file may state 10 as that cap - as a fact, a cap
    # clause, or a limit the reader is told to stay under.
    FORBIDDEN = re.compile(
        r"(cap is 10|burst cap is 10|cap of 10|hard cap"
        r"|at or under 10|cap: 10|10-request cap|limit of 10)", re.I)

    def test_no_file_states_ten_as_a_gateway_fact(self):
        for rel in ("luna_doctor/cli.py", "luna_doctor/_fix_block.py",
                    "luna_doctor/_chart_block.py", "README.md",
                    "skills/luna-doctor/SKILL.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(self.FORBIDDEN.search(text), f"{rel} still states the cap as fact")


if __name__ == "__main__":
    unittest.main()
