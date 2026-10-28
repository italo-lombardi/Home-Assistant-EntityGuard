# Entity Guard — Pending Backlog

_Not scheduled for execution today. Captured 2026-10-28 after Round 3 (PR #51) shipped._

Current state: `main` at `e9be761`, manifest `0.4.0` (no bump since v0.4.0), no open feature branches, no pending PRs. All Round-3 features (G/H/I/J) merged and live-verified on `serene_booth`.

---

## 1. Tier-2 opportunity-scan items
Not yet scoped or built. Next feature batch after the Tier-1 rounds (per-entity visibility, status reason line, deliverable states, target-missing repair, enforce timeout, cooldown/suppression countdown, verify-after-enforce, diagnostics, skip counters, device triggers). Needs a fresh opportunity scan to enumerate concrete items before planning.

## 2. `alarm_control_panel` support (v2)
Deferred from Feature C (deliverable non-on/off states). Arming/disarming many real panels needs a **code field**, which the current fixed-state picker + service map do not carry. Requires:
- a config-flow field for the arm/disarm code (optional, panel-dependent),
- threading the code through to `alarm_control_panel.alarm_arm_*` / `alarm_disarm` service calls,
- the safety-acknowledgment gate (as cover/lock/climate/valve already have).

## 3. Suppress re-arm hardening (from PR #43)
Pre-existing deferred work, not a Round-3 regression. See memory `suppress_rearm_hardening.md`:
- deferred hardening tests not yet written,
- GAP-3 context-id false positive still open.

---

### Working conventions to carry forward (so the next session doesn't relearn them)
- **Worktrees, not branches**, for concurrent same-file edits across sessions (`.claude/worktrees/feat-X`). Shared checkout + `git checkout -b` does NOT isolate.
- **CI pins `ruff==0.16.1`** (`.github/workflows/validate.yml`). Match it locally; `ruff format` output differs between minor versions and will fail CI otherwise.
- **Never run `ruff format` on `*.json`** (strings.json / translations) — it injects trailing commas and corrupts them.
- **100% coverage gate** (`pytest --cov-fail-under=100`) + JS card tests (`node --test tests/frontend/*.test.mjs`).
- **New user-facing strings** → native-speaker translate + adversarial-skeptic workflow into all 28 non-English locales; verify key + placeholder parity vs `en.json`.
- **No version bump, no tags/releases** unless the maintainer explicitly asks.
- **Review model:** peers cross-review features they did not build + a 5-agent adversarial workflow; verify every finding before acting.
- **Smoke targets:** only the allowed/disposable entities in memory `smoke_test_entities.md`; smokes run inside `serene_booth`.
