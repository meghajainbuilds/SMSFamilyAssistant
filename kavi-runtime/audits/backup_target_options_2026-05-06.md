# Off-Mac backup target options — 2026-05-06

## What's broken or at risk

Item #1 ships a nightly local snapshot of state files (`imessage-state.json`,
`learned_facts.jsonl`, `pending_facts.jsonl`, `corrections.jsonl`,
`runs.jsonl`) into `/Users/kavi/kavi-runtime/snapshots/<UTC-date>/` with
14-day retention. **That's still single-Mac.** A theft, drive failure, or
ransomware on Kavi's Mac wipes the snapshot dir along with the live data.
Local snapshot is the primary defense; this audit picks the second copy.

PM-visible failure mode: 6 weeks of corrections + learned facts gone after
a single Mac dies, no rollback path.

## Three options

### (a) Tailscale-reachable Mac at Megha's house

A second Mac (any Mac mini, MBA, even an unused one) on the same Tailnet
runs `rsync` over SSH from Kavi's Mac once daily at 03:30 PT (right after
the 03:00 snapshot finishes).

- **Cost:** $0/month if a spare Mac exists. Otherwise, ~$600 one-time for
  a refurbished Mac mini.
- **Setup:** install Tailscale on the second Mac, generate an SSH key on
  Kavi, authorize on Megha's Mac, add a launchd job that runs `rsync -av`
  to a folder on Megha's Mac.
- **Recovery time:** seconds (rsync back).
- **Risk:** both Macs live in Seattle; a fire / break-in could hit both.
  Geographic redundancy not solved.

### (b) Backblaze B2

Backblaze B2 is the cheapest mainstream object store. CLI client `b2`
runs on Kavi's Mac.

- **Cost:** $5/TB-month. HomeOS state is ~50MB today, growing slow → less
  than $0.50/month for years.
- **Setup:** create a B2 account, generate an application key, install
  `b2` via brew, schedule a daily `b2 sync` from `/snapshots/` to a B2
  bucket. ~30 minutes setup.
- **Recovery time:** minutes (download bucket).
- **Risk:** depends on Backblaze surviving as a company; SOC 2 / ISO
  certified. Geographic redundancy: yes (B2 lives in 3+ regions).
- **Privacy:** state files contain household identity + financial domain
  metadata. Encrypt-at-rest is default; consider client-side encryption
  with `age` or `gpg` before upload as additional belt.

### (c) GCS / S3 (Google Cloud Storage or Amazon S3)

Same shape as B2, slightly more setup overhead, slightly more expensive.

- **Cost:** ~$0.02/GB-month + egress. For HomeOS state (~50MB),
  ~$0.001/month + egress on download. Effectively zero, but the free
  tiers cover 5GB on both.
- **Setup:** install `gcloud` or `awscli`, create a bucket, IAM role.
  ~45 minutes.
- **Recovery time:** minutes.
- **Risk:** lower than B2 on company viability (Google / Amazon scale),
  higher complexity (more IAM surface to misconfigure).

## Recommendation

**Option (a) if a spare Mac exists at Megha's house, otherwise (b) Backblaze B2.**

Reasoning:
- (a) is operationally simpler — `rsync over SSH on Tailscale` is a one-line
  cron job, with no third-party account or billing relationship.
- (b) is the right pick if no spare Mac is available. $0.50/month is real
  but trivial; Backblaze is the closest-to-free cloud object store with
  decent reliability. Add client-side encryption (`age` is one binary)
  before adopting.
- (c) is overkill for 50MB of household state. Skip unless Megha has a
  free GCS/S3 credit lying around.

**Do NOT create cloud accounts in this audit pass.** Decision needed from
Megha before any setup commences.

Open question: does Megha already have a spare Mac at home that could host
the rsync target? That answer flips this from (b) to (a) at zero cost.
