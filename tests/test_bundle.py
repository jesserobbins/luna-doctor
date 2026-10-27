"""Tests for luna_doctor/bundle.py - the paste-safe evidence block.

Three properties are what this module exists for, so they are what these tests
pin:

- identity does not survive `redact`, and evidence does. A home path, an email
  address and an API key are removed; a model id, a duration, a count and an ISO
  timestamp come through untouched, because a maintainer cannot date or
  reproduce a report without them. Redaction that also ate the timestamps would
  pass a "no leak" check and still be useless.
- `issue_block` is datable: tool version, both window bounds and the store
  names, so a pasted block can be placed in time and traced to the logs it came
  from. Its verdict line is optional and its absence is clean.
- the block is paste-safe: ASCII, no ANSI, no emoji, every line inside 100
  columns, and no triple backticks to close the fence it is pasted into.

`manifest` is the honesty check on the other side: a reader must always be told
what was left out, so a quiet window cannot be mistaken for a truncated one.
"""

import unittest

from luna_doctor.bundle import (
    MARK_EMAIL,
    MARK_TOKEN,
    MAX_LINE,
    issue_block,
    manifest,
    redact,
    redact_masked,
)

# Shaped like a real LunaRoute key: the lr_ prefix and 64 hex-ish characters.
KEY = "lr_" + "A1b2C3d4" * 8

PROVENANCE = {
    "version": "0.1.1-alpha.2",
    "schema": 3,
    "window_start": "2026-10-24T00:05:14Z",
    "window_end": "2026-10-24T06:05:14Z",
    "stores": ["pi", "claude-code", "codex"],
}

# Deliberately longer than one line, with a model id, a duration, a timestamp
# and a count in it: the block has to wrap this and keep all four intact.
HEADLINE = (
    "Your organization: this machine's peak concurrency was 12 concurrent "
    "requests (this machine), inside the declared width of 20 priority "
    "concurrent requests (declared plan apex), and the account reported in use "
    "20 of 20 priority concurrent requests (account) since 2026-10-24T00:05:14Z, "
    "a median park deadline of 120.2s observed over 41 requests (this machine); "
    "this machine was not the width, the account was."
)
ROWS = [
    ("request count", "41 requests in the last 6 hours (this machine)"),
    ("peak concurrency", "12 concurrent requests (this machine)"),
    ("model", "deepseek-4.1-flash"),
]
VERDICT = ("your-org", "the account was the width, not this machine.")


class RedactStripsIdentity(unittest.TestCase):
    def test_redact_strips_users_paths(self):
        out = redact("store at /Users/jesse/.codex/sessions/2026-10-24")
        self.assertEqual("store at ~/.codex/sessions/2026-10-24", out)
        self.assertNotIn("jesse", out, "the account name survived redaction")

    def test_redact_strips_home_paths(self):
        out = redact("store at /home/ana/.claude/projects and at /home/ana")
        self.assertEqual("store at ~/.claude/projects and at ~", out)
        self.assertNotIn("ana", out)

    def test_redact_strips_email_addresses(self):
        out = redact("reported by jesse.robbins+ld@example.co.uk today")
        self.assertEqual("reported by %s today" % MARK_EMAIL, out)
        self.assertNotIn("@", out)

    def test_redact_strips_a_long_opaque_token(self):
        self.assertEqual(67, len(KEY), "the fixture key must be 67 characters")
        out = redact("authorization: Bearer %s" % KEY)
        self.assertEqual("authorization: Bearer %s" % MARK_TOKEN, out)
        self.assertNotIn(KEY, out)

    def test_redact_uses_the_home_it_is_given(self):
        out = redact("cfg /opt/team/jesse/.codex/config.toml", home="/opt/team/jesse/")
        self.assertEqual("cfg ~/.codex/config.toml", out)

    def test_redact_leaves_a_url_path_alone(self):
        """A URL that merely contains /home/ is a link, not a home directory."""
        text = "see https://example.com/home/changelog for the rollout notes"
        self.assertEqual(text, redact(text))


class RedactKeepsEvidence(unittest.TestCase):
    def test_redact_leaves_model_ids_durations_counts_and_timestamps_alone(self):
        text = ("model deepseek-4.1-flash glm-5.3, median park deadline 120.2s, "
                "41 requests across 3 stores, 41 MB of logs, window "
                "2026-10-24T00:05:14Z .. 2026-10-24T06:05:14Z, day 2026-10-24")
        self.assertEqual(text, redact(text))

    def test_redact_is_idempotent(self):
        once = redact("home /Users/jesse/.codex, key %s, mail a@b.co" % KEY)
        self.assertEqual(once, redact(once))
        self.assertIn(MARK_TOKEN, once)
        self.assertIn(MARK_EMAIL, once)

    def test_redact_handles_several_of_each_in_one_string(self):
        text = ("/Users/one/a /Users/two/b /home/three/c /home/four/d "
                "%s %s one@example.com two@example.org" % (KEY, "lr_" + "9" * 40))
        out = redact(text)
        self.assertEqual("~/a ~/b ~/c ~/d %s %s %s %s"
                         % (MARK_TOKEN, MARK_TOKEN, MARK_EMAIL, MARK_EMAIL), out)

    def test_redact_leaves_no_home_path_email_or_token(self):
        text = ("%s saw /Users/jesse/.pi/logs and /home/ana/.pi/logs, wrote to "
                "jesse@example.com, then re-read /Users/jesse/.pi/logs" % KEY)
        out = redact(text, home="/Users/jesse")
        for leaked in ("/Users/", "/home/", "jesse", "ana", "@", KEY):
            self.assertNotIn(leaked, out, "redaction leaked %r" % leaked)


class IssueBlockIsDatable(unittest.TestCase):
    def test_issue_block_carries_version_window_stores_and_verdict(self):
        block = issue_block(HEADLINE, ROWS, PROVENANCE, VERDICT)
        self.assertIn("0.1.1-alpha.2", block, "the block cannot be dated")
        self.assertIn(PROVENANCE["window_start"], block)
        self.assertIn(PROVENANCE["window_end"], block)
        for store in PROVENANCE["stores"]:
            self.assertIn(store, block, "the block cannot be traced to its logs")
        self.assertIn("verdict: your-org - the account was the width",
                      block)
        self.assertIn(VERDICT[1], block)
        for label, value in ROWS:
            self.assertIn(label, block)
            self.assertIn(value, block)
        self.assertTrue(block.endswith("\n"), "one trailing newline, no more")
        self.assertFalse(block.endswith("\n\n"))

    def test_issue_block_omits_the_verdict_line_when_there_is_none(self):
        block = issue_block(HEADLINE, ROWS, PROVENANCE)
        self.assertNotIn("verdict:", block)
        self.assertIn(HEADLINE.split(",")[0], block, "the headline still renders")
        with_verdict = issue_block(HEADLINE, ROWS, PROVENANCE, VERDICT)
        self.assertEqual(
            [line for line in with_verdict.splitlines()
             if not line.startswith("verdict:")],
            block.splitlines(),
            "a missing verdict costs exactly one line and changes nothing else")

    def test_issue_block_reports_an_absent_provenance_as_unknown(self):
        block = issue_block("headline only", [], {})
        self.assertIn("unknown", block)
        self.assertIn("window: unknown", block)
        self.assertIn("stores: none reported", block)


class IssueBlockIsPasteSafe(unittest.TestCase):
    def test_issue_block_wraps_every_line_to_100_columns(self):
        rows = ROWS + [("store path", "/Users/" + "x" * 140)]
        for block in (issue_block(HEADLINE, rows, PROVENANCE, VERDICT),
                      issue_block(HEADLINE, rows, PROVENANCE)):
            for line in block.splitlines():
                self.assertLessEqual(
                    len(line), MAX_LINE,
                    "a line of %d columns breaks the paste width: %r"
                    % (len(line), line))

    def test_issue_block_has_no_triple_backticks(self):
        rows = ROWS + [("log body", "```json\n{\"outcome\": \"rate_limited\"}\n```")]
        block = issue_block("a fence ``` inside the headline", rows, PROVENANCE)
        self.assertNotIn("```", block, "the block would close its own fence")

    def test_issue_block_is_plain_ascii(self):
        block = issue_block("caf\u00e9 \u2014 12 requests \U0001f600",
                            ROWS, PROVENANCE, VERDICT)
        block.encode("ascii")           # raises if anything non-ASCII survived
        self.assertNotIn("\x1b", block, "no ANSI escapes")
        self.assertNotIn("\u2014", block)
        self.assertNotIn("\U0001f600", block, "no emoji")


class ManifestStatesTheOmission(unittest.TestCase):
    def test_manifest_states_the_omission(self):
        lines = manifest([("pi logs", "3 stores, 41 MB")],
                         [("full log bodies", "verbatim request text")])
        self.assertIn("included: pi logs (3 stores, 41 MB)", lines)
        self.assertIn("omitted: full log bodies (verbatim request text)", lines)
        self.assertEqual(2, len(lines))

    def test_manifest_says_so_when_a_side_is_empty(self):
        lines = manifest([], [])
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].startswith("included: none"), lines[0])
        self.assertTrue(lines[1].startswith("omitted: none"), lines[1])
        self.assertTrue(all("omitted" in line for line in lines[1:]))

    def test_manifest_always_names_an_omission(self):
        for included, omitted in (([("pi logs", "")], []),
                                  ([], [("log bodies", "")])):
            lines = manifest(included, omitted)
            self.assertTrue(any(line.startswith("omitted:") for line in lines),
                            "the omission must always be stated: %r" % lines)
        self.assertIn("included: pi logs", manifest([("pi logs", "")], []))


class NoRetiredOrBillingVocabulary(unittest.TestCase):
    # LunaRoute bills concurrent sessions, so the copy never reaches for
    # another unit, and the retired internal terms stay retired.
    FORBIDDEN = ("probe", "load-test", "load test", "compare", "credit",
                 "spend")

    def _surfaces(self):
        return {
            "redacted text": redact(
                "home /Users/jesse/.codex, key %s, mail a@b.co" % KEY),
            "block with verdict": issue_block(HEADLINE, ROWS, PROVENANCE, VERDICT),
            "block without verdict": issue_block(HEADLINE, ROWS, PROVENANCE),
            "manifest": "\n".join(manifest(
                [("pi logs", "3 stores, 41 MB")], [("full log bodies", "")])),
            "manifest empty": "\n".join(manifest([], [])),
        }

    def test_no_forbidden_word_in_any_emitted_string(self):
        for name, text in self._surfaces().items():
            low = text.lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, low,
                                 "%s contains the forbidden word %r" % (name, word))


class ToolEvidenceSurvives(unittest.TestCase):
    def test_a_named_gateway_constant_is_not_redacted(self):
        text = "refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED on 3 requests"
        self.assertIn("CONCURRENT_REQUEST_LIMIT_EXCEEDED", redact(text))

    def test_a_harness_key_is_not_redacted(self):
        text = "set CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS to 8"
        self.assertIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", redact(text))

    def test_a_lowercased_home_path_is_still_redacted(self):
        """error_signature() lowercases error text before it reaches this block."""
        self.assertNotIn("/users/jesse", redact("at /users/jesse/private/x"))
        self.assertIn("~/private/x", redact("at /users/jesse/private/x"))


class MaskedHomePrefix(unittest.TestCase):
    def test_a_home_with_a_digit_in_it_is_redacted_in_its_masked_form(self):
        out = redact_masked("at /opt/team/jesse#/file here", home="/opt/team/Jesse2")
        self.assertNotIn("/opt/team", out)
        self.assertIn("~/file", out)

    def test_an_uppercase_credential_with_digits_is_redacted(self):
        out = redact("key LR_7F3A9C2D5E8B1A4F6C0D3E7A here")
        self.assertIn(MARK_TOKEN, out)


class MaskedSignature(unittest.TestCase):
    def test_an_address_masked_in_its_domain_is_redacted(self):
        self.assertNotIn("example", redact_masked("contact jesse@example#.com"))

    def test_an_address_masked_in_its_local_part_is_redacted(self):
        self.assertNotIn("example.com", redact_masked("contact jesse#@example.com"))

    def test_a_credential_with_masked_digits_is_redacted(self):
        out = redact_masked("bearer lr_4a8#f2c9d1e0b7a6c5d4e3f2a1b0c9d8e7f6")
        self.assertIn(MARK_TOKEN, out)


if __name__ == "__main__":
    unittest.main()
