import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NoCapAsFact(unittest.TestCase):
    FORBIDDEN = re.compile(r"(cap is 10|burst cap is 10|cap of 10|hard cap)", re.I)

    def test_no_file_states_ten_as_a_gateway_fact(self):
        for rel in ("luna_doctor/cli.py", "luna_doctor/_fix_block.py",
                    "luna_doctor/_chart_block.py", "README.md",
                    "skills/luna-doctor/SKILL.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(self.FORBIDDEN.search(text), f"{rel} still states the cap as fact")


if __name__ == "__main__":
    unittest.main()
