# Public repo plan

Decided 2026-09-23. This is the working plan for publishing HomeOS as a public portfolio repo. Update it in place as decisions change.

## Decisions (Megha)

- **Audience:** a portfolio showcase that people can read and run with their own config. It is not a general framework. It stays Mac + iMessage (BlueBubbles) + Outlook/Microsoft To Do, and the README says so up front.
- **License:** PolyForm Noncommercial 1.0.0, plus a README line saying commercial use requires contacting Megha. This makes it "source-available," not OSI open source. Not legal advice.
- **Framing:** "Shipped April to September 2026. Runs daily for a family of four." Operator voice, not course voice.
- **Order of work:** modernize in the private repo first and verify it live on Kavi. Only then export to a new public repo with fresh history.
- **2026-09-24 update: one repo, not two.** Megha wants a single GitHub repo she keeps building in. So private data stays on disk in `~/Documents/HomeOS` (iCloud-synced) but is gitignored, public examples use a fictional household (Alex Rivera, Sam Chen, kid Theo), a leak guard blocks private markers on every commit, and the same repo gets one history reset (old history kept as a local bundle in `~/Documents/HomeOS-private/backups/`) before it flips to public. Step 1 (untrack fully private files, example configs) shipped in 04b6651.

## Showpieces (2 to 4 sentences each in the README)

Product
1. **Capability spec as a loop.** The capability doc is the spec. Tests derive from it, evals grade against it, and labels flow back into it.
2. **Eval loop.** Frozen matrices, multi-sample graded scorecards, anti-Goodhart freeze and iteration bounds, and Investigator/Verifier claim gates.

Builder
3. **Always-on agent setup.** A dedicated Mac, launchd, Graph webhooks through Tailscale Funnel, spend caps, liveness alarms, and what broke (the 43-day silent pause).
4. **Agent orchestration.** Investigator, Verifier and Engineering Manager sub-agents with claim gates. The "I'm the VP, every agent is a team member" frame.
5. **Choosing a harness.** Claude Code for building; the Anthropic Python SDK for the runtime; why not Managed Agents or the Agent SDK for this job.
6. **Cheapest model the task needs.** The per-task cost table, the eval gate for each downgrade, and the $77 to about $15 to $20 per month story (see `docs/cost-story.md`).

Then a **concept map**: about 15 AI product, builder and leadership concepts. Each row links to the file that proves it, for example "blameless incident review" linking to the realtime-kavi changelog entry for 2026-09-23.

## Remaining work to go public safely (rewritten 2026-09-24)

One repo. Private data stays on disk, gitignored. Nothing flips public until every gate below is green. Each step ships to Kavi and passes tests before the next starts.

### Done
- **Step 1 (04b6651).** Stopped tracking household.md, handoffs/, archive/, investigations/, real configs and eval data. Added `config*.example.yaml` with a fictional household (Alex Rivera, Sam Chen, kid Theo). Full history backed up to `~/Documents/HomeOS-private/backups/`.

- **Step 2 (2026-09-24). Split specs and skills.**
  - **Privacy rule (Megha):** Megha and Max may appear by name. The kids' names must not appear anywhere public. Schools, daycare, caregivers, vendors, other people and contact details are private too.
  - **How:** 232 private blocks across 34 spec and skill files. Each block's public text is a fictional stand-in. The real text lives in gitignored `private/overlays/*.json`, and every loader swaps it back at read time (`kavi_runtime/private_overlay.py`). Stand-ins follow `private/fictional-map.md`. If the overlay is missing, Kavi refuses to compose rather than run on fake names. Public clones set `HOMEOS_PUBLIC_EXAMPLES=1`.
  - **Tools:** `scripts/private_overlay.py` handles extract, check, scan and show. The private-term list is at `private/denylist.txt` (89 terms), not `~/Documents/HomeOS-private/`. Label writeback sends any learned example that names a private term to the overlay.
  - **Gate:** every file resolves byte for byte to its pre-split version (`check --against d0a6344`). On staging, all 25 prompt pieces hash the same as production (`scripts/prompt_hashes.py`). 1,289 tests pass. Deployed as 397df13; Verifier PASS on production (all 25 prompt pieces identical to 905cf87, no overlay errors, live replies working).
  - **Left for later steps:** kids' names still appear in 6 test files (step 4) and in `.claude/skills` (step 5). Block markers inside markdown tables and code fences look rough on GitHub; tidy them in step 8.

### Step 3. Household identity and paths into config (about 1 to 2 sessions)
- **What:** about 90 hardcoded sites in runtime code: phone and email sets, the Megha/Max roster, `MJ`/`MM` prefixes, the `megha|max` owner enum, the outbound allowlist, `/Users/kavi` defaults, the Tailscale IP, `com.megha.kavi` launchd labels, and about 680 comment lines naming the family.
- **How:** one `household` block in config (members, short codes, emails, phones, roles). Code reads roles such as "parent_a", never names. Comments become generic.
- **Gate:** full tests, all four matrices, and a Verifier pass on production.

### Step 4. Scrub tests and fixtures (about 1 session)
- **What:** 105 of 152 test files contain real names, and 25 hold real email or list IDs. 11 regression fixtures are real production traces.
- **How:** replace them with the fictional household and fake IDs. Rebuild the regression fixtures from synthetic data that reproduces the same bug.
- **Gate:** same test count passing before and after.

### Step 5. Everything else that's tracked (about 1 session)
- **Files:** `.claude/` skills, commands and agents; every `CLAUDE.md`; `docs/`; `Templates/`; `kavi-runtime/audits/`, `backlog.md` and `README.md`; the root `scripts/`.
- **How:** dev instructions become generic. Personal notes move to gitignored `CLAUDE.local.md` files. `.claude/settings.json` loses the list ID.

### Step 6. Security review before exposure (about 1 session). Must pass.
Public code shows attackers every endpoint. Check these on the live Funnel URL:
- **`/synthetic/*` routes:** they call the LLM with no auth. If Funnel forwards them, anyone could drain the Anthropic budget or fill the logs. They must be tailnet-only or require a token.
- **The iMessage and Graph webhooks:** confirm the BlueBubbles webhook and Graph `clientState` are checked, so nobody can post fake messages or emails that Kavi acts on.
- **Rotate:** the BlueBubbles password (Megha, in the app) and the healthchecks.io ping URL. Both were committed.
- **Run** `/security-review` and the Engineering Manager on the final tree.

### Step 7. Leak guard (about half a session)
- **Pre-commit hook:** blocks any commit that contains a private marker. The marker list itself is private (`private/denylist.txt`, gitignored), so it never gets committed. `scripts/private_overlay.py scan` already checks the specs and skills against it.
- **GitHub Action:** gitleaks for secrets, plus generic patterns: phone numbers, `AQMk` IDs, `/Users/` paths, Tailscale IPs.
- **Gate:** a planted fake marker is blocked locally and in CI.

### Step 8. README, license, setup (about 1 session)
- **README:** the six showpieces and the concept map above, plus "Mac + iMessage + Outlook only".
- **LICENSE:** PolyForm Noncommercial 1.0.0.
- **Setup guide:** uses the example configs and a sample household.md.

### Step 9. Fresh history and publish (about half a session). Megha does the final clicks.
- **Why a new repo instead of force-pushing:** GitHub keeps old commits reachable by their ID even after a force-push. So we delete the GitHub repo and recreate it with the same name. It is still one repo.
- **Commit:** a single "initial public" commit from the cleaned tree.
- **Before flipping public:**
  - Clone it fresh into a temp folder.
  - Run the leak scan over the whole clone.
  - Run the tests against the example config.
  - Check that no GitHub Actions secrets are needed.
- **Local repo:** points at the new remote. Old history stays only in the backup bundle.

### After it's public (ongoing)
- Every commit goes through the leak guard.
- New family examples go into the private overlay, never the public specs.
- New private data paths get added to `.gitignore` in the same commit that creates them.
