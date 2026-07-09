---
name: self-update
description: "Update the installed agent-coordination-skills plugin to the latest version by fetching main and mirroring it over the local install. Triggers: 'update to latest version', 'self-update', 'update the coordination skill', 'pull latest from main', 'upgrade the coordination plugin'."
---

# Self-update

Update this plugin **in place** to the latest `main`. You (the agent) do the one
authenticated network step — fetching `main` — and the offline helper
`scripts/self_update.py` does all filesystem work (back up → mirror → verify). The
helper touches nothing on the network and imports nothing outside the standard
library, so the risky part (overwriting an install) is deterministic and testable.

> Never overwrite the install without a backup and explicit user confirmation. Update
> the **installed plugin directory**, never the user's working repository. The helper
> auto-restores from its backup if the updated install fails to run.

## Procedure

1. **Locate the install (the target).** This skill file lives at
   `<install>/skills/self-update/SKILL.md`, so the install root is three directories
   up from this file. Confirm it looks like a plugin: it must contain
   `.claude-plugin/plugin.json` and `coord/coord.py`.

2. **Read the current version.** Print `version` from
   `<install>/.claude-plugin/plugin.json` so the user sees where they're starting.

3. **Fetch `main` to a temporary directory** (the only network step). The repo is
   public:
   - Preferred: `git clone --depth 1 https://github.com/Yarbrdab000/Github-copilot-project-manager <tmp>`
   - Fallback (no git, or a private mirror): `gh api repos/Yarbrdab000/Github-copilot-project-manager/tarball/main > <tmp>.tar.gz` then extract.
   If the fetch fails (offline, no auth), **stop** and tell the user — do not touch the
   install.

4. **Preview (dry-run).** Run the helper read-only and show the user the plan:
   ```
   python <install>/scripts/self_update.py --source <tmp> --target <install> --dry-run
   ```
   It prints `version: <from> -> <to>` and the exact add/update/remove list. If the
   list is empty, report "already up to date" and stop.

5. **Confirm.** Ask the user to approve applying the update (it will back up first).
   Only proceed on an explicit yes.

6. **Apply.** Re-run the helper without `--dry-run`:
   ```
   python <install>/scripts/self_update.py --source <tmp> --target <install>
   ```
   It writes a timestamped backup, mirrors `main` over the install, then runs
   `coord --help` to verify. **If verify fails it auto-restores the backup and exits
   non-zero** — report that and the backup path; the install is left working.

7. **Report and clean up.** Show `before -> after` version, the add/update/remove
   counts, and the backup location. Advise the user to **restart their Copilot session**
   so the reloaded skills, agents, and manifest take effect. Remove the temp fetch
   directory.

## Boundaries

- This skill updates the **install**, not the repo you're coding in. It performs no
  git writes in any working repository and never merges or pushes.
- All destructive filesystem work goes through `scripts/self_update.py`, which is
  offline and covered by `tests/test_self_update.py`. Do not hand-copy files.
