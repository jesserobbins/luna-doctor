# Install and configure with an agent

Copy the block below into a coding agent that runs on the machine you want to
measure. The agent installs `luna-doctor`, reads the account's capacity, and
applies the concurrency settings that `fix` names. You do not need to install
Python packages by hand.

The block tells the agent two things that matter. It never opens a request of
its own, because a diagnostic must not add to the pressure it explains. And it
never states a concurrency number that it did not read from the account or from
your declaration.

## The block

````text
Install luna-doctor on this machine and use it to configure my agent
concurrency settings.

1. Install it. It is not published on PyPI, so install it from the repository:

   pipx install "luna-doctor @ git+https://github.com/jesserobbins/luna-doctor.git"

   If pipx is missing, install it (brew install pipx) and run pipx ensurepath.
   Open a new shell after that.

2. Check that it runs and that it finds my logs:

   luna-doctor --version
   luna-doctor discover

   If discover reports no store, stop and show me the output. Nothing to read
   means nothing to fix.

3. Read the account's width. LunaRoute bills concurrent sessions, so the width
   is what matters, and it belongs to my account, not to the gateway:

   luna-doctor capacity --online

   That is one authenticated GET and it needs the key that lunaroute login
   already stored. If it fails, ask me for my plan name or my priority
   concurrency number, and pass it as --plan <name or number> on each command
   in steps 4 to 6. Never state a width you did not read.

4. Show me what my traffic did in the last day, and what to change:

   luna-doctor analyze --since 24h
   luna-doctor fix --since 24h

5. Apply the fix blocks. Each block names one file, one key, the current value
   and the new value. Back up each file before you edit it, keep the file valid
   JSON, and tell me every change you made. Do not edit a key that fix did not
   name.

6. Ask me to restart the harness. Then confirm that the change held:

   luna-doctor analyze --baseline prev

   Read me the change: line. Fewer requests blocked at admission, over a window
   with enough requests to clear the sample floor, is the sign that it worked.
   If the report says inconclusive, the window was too small. Say that and wait.

While you work:

- Quote the numbers as printed. Do not round them, re-derive them or add them.
- A measured number is mine. A configured number is a setting in my agent
  config. Never sum the two.
- Never open requests of your own to test a theory. This tool reads what
  already happened.
- If the report shows no pressure in the window, say so and stop.
````

## What the user sees at each step

| step | what it costs | what it can touch |
|---|---|---|
| `pipx install` | one download and build from the repository | a new venv under pipx's home |
| `discover` | nothing | reads only |
| `capacity --online` | one authenticated GET to `api.lunaroute.com` | refreshes the 5-minute disk cache |
| `analyze`, `fix`, `analyze --baseline prev` | nothing | reads the local harness logs |

Every command except `capacity --online` runs offline. No step opens a session,
so the run does not add to the pressure it reports.

## After the install

The wheel installs the command, not the skill. The skill that an agent reads is
in the repository, at `skills/luna-doctor/SKILL.md`, with its playbooks under
`references/`. Point the agent at that file, or copy the directory into the
agent's own skill directory.

With the skill loaded, the agent has three more surfaces: `luna-doctor` explains
a window, `explain` explains one request, and `--for-issue` prints a redacted
block for a support thread. This repository holds the design notes under
`docs/`, and the README holds the full command surface.
