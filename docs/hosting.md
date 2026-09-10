# Where to run it

Herald is most useful when it is always on: it ingests every thirty minutes,
briefs you at 06:30, and interrupts you the hour it finds something closing.
A copy that only runs when you open a laptop is a different, smaller thing.

This is what is actually free, checked in September 2026, and what each option
costs you in ways that are not money.

## What Herald needs from a machine

| | |
|---|---|
| **RAM** | Anthropic gives Claude Code a floor of **4 GB**. Herald itself is small; the model CLI is what wants the memory. |
| **CPU** | Anything. It is idle almost all the time and bursts for a couple of minutes a day. |
| **Disk** | A few hundred MB to start. Mail metadata and message history grow slowly; the ledger here is ~80 MB after a fortnight of heavy use. |
| **Architecture** | x64 or ARM64 both work — Claude Code ships native binaries for both, and no longer needs Node at runtime. |
| **OS** | Ubuntu 20.04+, Debian 10+, Alpine 3.19+, or macOS 13+. Windows only through WSL2. |
| **Network** | Outbound HTTPS. **No inbound ports**, ever: Telegram is long-polled, and the setup wizard binds to localhost. |
| **Uptime** | See the login warning below — this is the constraint people do not expect. |

### The login constraint nobody expects

Claude Code's stored login refreshes itself every time it runs, and the refresh
window is roughly **a day**, not months. `herald doctor` reports it:

```
login refreshable for another 13h (renewed on every run)
```

An always-on Herald renews it constantly and never notices. **A machine that is
off or asleep for much longer than a day comes back logged out**, and everything
model-driven — the digest, the scout pass, answering you — stops until somebody
runs `claude` and `/login` again. Collectors keep working, because they never
touch a model.

That single fact is the strongest argument for a machine that stays on, and the
reason "I'll just leave it on my laptop when I remember" is the option that
disappoints.

## The free options, ranked honestly

### 1. Hardware you already own — the real answer

An old laptop, a Mac mini, a small desktop, or a Raspberry Pi 4/5 with 4 GB or
more. This is free in the sense that matters, and it beats every cloud free tier
on every axis except electricity:

- nothing to be reclaimed, throttled, or halved by a provider
- no credit card, no identity verification, no region lottery
- your data is on hardware you can physically hold
- on a **Mac**, you also get the bundled iMessage extension, which reads the
  Messages database and can tell you who is actually waiting on a reply. No
  cloud host can do that

A Pi 5 costs a few dollars a year to run. An old laptop with the lid closed and
"sleep when lid is closed" turned off is the cheapest always-on server most
people already have. Herald installs the same way on all of them.

The one thing to get right: **stop it sleeping.** On Linux,
`sudo systemctl mask sleep.target suspend.target hibernate.target`. On macOS,
System Settings → Displays → Advanced → "Prevent automatic sleeping when the
display is off", or `caffeinate -s`. On a laptop, also disable lid-close sleep.

### 2. Oracle Cloud Always Free — the only cloud free tier that fits

The only genuinely free VM with enough memory: **2 Ampere ARM cores and 12 GB
of RAM**, permanently, plus two tiny AMD instances. ARM is fine — Claude Code
has native `linux-arm64` binaries.

The catches are real and you should know all four before choosing it:

- **The allowance was halved in June 2026**, from 4 cores and 24 GB to 2 and 12,
  with no announcement — the documentation was edited and people found out when
  their instances were resized. What is free today is not a promise about next
  year.
- **Idle instances get reclaimed.** Under 10% CPU *and* under 10% network over
  seven days and Oracle may stop yours. Herald is idle by design, so this is a
  live risk rather than a theoretical one. The standard mitigation is a cron job
  that does something measurable; a nightly `herald collect --force` plus a
  regular SSH session is usually enough, and an instance that is stopped can
  simply be started again.
- **ARM capacity is frequently exhausted** in popular regions, and your home
  region is fixed at signup. Expect to retry, possibly for days.
- **A credit card is required**, and the account is easy to trip into paid
  resources if you provision anything beyond the free shapes.

### 3. Google Cloud's free e2-micro — free forever, and too small

One `e2-micro` (shared vCPU, **1 GB RAM**), 30 GB disk, permanently free, but
only in `us-west1`, `us-central1` or `us-east1`, with 1 GB/month of egress.

1 GB is a quarter of Claude Code's documented minimum. Collectors and the
Telegram bridge will run; a model invocation on a 1 GB shared-core box with swap
is where it falls over, which means the digest — the thing you installed it for.
Fine for experimenting, not for the thing you rely on.

### 4. What does not work, and why

- **AWS**: the 12-month free tier ended for accounts created after 15 July 2025;
  new accounts get credits that expire. A home for something meant to run for
  years should not have a countdown.
- **Azure**: 12 months of a B1S, then it stops being free.
- **Render, Railway, and most "free app hosting"**: the instance spins down
  after minutes of inactivity and the filesystem is ephemeral. Herald's memory
  *is* a filesystem, and a Herald that sleeps between requests cannot brief you
  at 06:30. Wrong shape, not merely small.
- **Fly.io**: no free tier since October 2024. An always-on machine is a couple
  of dollars a month, which makes it a cheap paid option rather than a free one.
- **GitHub Codespaces, Replit, Colab**: session-scoped by design.

If you would rather pay a little than manage hardware, a €4/month VPS from any
of the usual providers clears the 4 GB bar without any of Oracle's caveats, and
that is a perfectly reasonable answer to this question.

## Setting up on a machine you reach over SSH

Three things differ from a machine you are sitting at, and the wizard handles
all of them:

**Logging in to Claude Code.** Run `claude`, press `c` to copy the login URL,
open it in a browser anywhere, and paste the code back at the prompt. The docs
call this out for SSH sessions specifically; it is not a workaround.

Do *not* reach for `claude setup-token` and `CLAUDE_CODE_OAUTH_TOKEN` for this.
It works for model calls, but a token from it cannot establish Remote Control
sessions — which is exactly how Herald's brain serves the Claude app and
claude.ai/code. You would lose a surface without being told why.

**The Google consent screen.** `herald setup --web` prints an `ssh -N -L …`
line; run it on the computer you are sitting at, then open the printed link.
The OAuth redirect comes back to your own browser through the tunnel. The
terminal wizard prints the same tunnel command.

**Keeping the services running when you log out.** On Linux, once:

```bash
sudo loginctl enable-linger $USER
```

Without it, systemd stops your user's units when your SSH session ends, which
looks exactly like Herald having crashed.

## One machine, one subscription, one person

Herald runs Claude Code on **your** subscription, as you. That is what makes it
legitimate, and it means the obvious economy is not available: a Herald hosted
for several people out of one account is a shared subscription, which the terms
do not allow. Everyone who wants one needs their own machine and their own
subscription.

It also means the machine holds your Claude login, your Google token, and your
whole ledger. On a cloud host, that is your data on somebody else's disk — which
is a good reason to prefer the old laptop in the cupboard, and a better reason to
keep the ledger repository private if you give it one.

## Sources

- [Claude Code system requirements](https://code.claude.com/docs/en/setup) — 4 GB RAM, x64/ARM64, supported OSes
- [Claude Code authentication](https://code.claude.com/docs/en/authentication) — SSH login flow, credential storage, `setup-token` limits
- [Oracle Cloud Infrastructure Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm) — Always Free resources and idle reclamation
- [Google Cloud Free Tier](https://cloud.google.com/free) — e2-micro allowance and regions
- [Free VPS tiers compared, 2026](https://www.mrplanb.com/datacenter/free-vps-tiers) — Oracle's June 2026 halving, AWS and Azure limits, Render's spin-down
- [Fly.io pricing after the free tier](https://www.saaspricepulse.com/blog/flyio-free-tier-2026)
