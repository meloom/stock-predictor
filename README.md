# stock-predictor

A long-only US-equity systematic trading system (IBKR), rebuilt clean from a
prior prototype with the lessons that prototype paid real money to learn.

**Status: migration in progress.** Code is being moved over from the previous
codebase module by module — each piece reviewed, fixed, and tested before it
lands here. Nothing in this repo trades real capital until it has passed
validation in the exact configuration it will run in.

## Ground rules (learned the hard way, enforced here)

1. **No component goes live without out-of-sample validation** in the exact
   configuration it will run in. Purge/embargo gaps in time-series splits are
   implemented in code, not just documented.
2. **Execution safety is non-negotiable**: limit orders only, position sync
   against broker ground truth before every order, per-ticker cooldown after
   losses, concentration cap AND per-position dollar-value verification
   (share-count rounding is never trusted), fill verification, regime cash gate.
3. **Money-touching code has unit tests.** No exceptions.
4. **No secrets, account identifiers, market data, or trade history in git.**
   See `.gitignore`.
5. **Honest metrics only**: a backtest result doesn't exist until it comes from
   a purged, held-out, touched-once test window; hit rates are reported against
   the correct null baseline; overlapping-window significance inflation is
   corrected for.

## Architecture (target)

```
data ingestion → signals → alpha → portfolio construction → execution → reporting
```

Modular layout, one responsibility per module — replacing the prior 1,900-line
monolith. Detail lands here as modules are migrated.
