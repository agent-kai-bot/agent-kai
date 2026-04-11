# ASO Final Convergence Verdict v3

## Verdict

**CONVERGED**

The last blocking ambiguity is gone.

- Stage 2 now uses `>= 50 per fold` with the explicit `100 if overlap-adjusted` rule.
- Stage 4 now uses `>= 10 trades` for shadow and explicitly says to extend shadow instead of auto-passing.
- The stale `30` and `5` literals are removed from the validation contract.
- The stage contract now points to `check_sample_size(...)` as the single sample-size gate.

That is enough to treat the design as implementation-ready for v1.

## Top 5 Implementation Priorities

1. Build the typed `Strategy IR -> validator -> executor adapter` path, with compile-time rejection of out-of-scope v1 fields.
2. Implement the canonical validation ladder as code: `in-sample -> walk-forward -> lockbox -> shadow -> promotion`, with `check_sample_size(...)` as the only trade-count gate.
3. Implement the frozen v1 executor semantics exactly as specified, including fill timing, fee/slippage/spread application, split boundaries, warm-up handling, and benchmark alignment.
4. Add artifacted evaluation + provenance storage in SQLite: strategy version, lineage, run metadata, dataset hash, executor version, prompt/model version, and immutable stage results.
5. Add tests for the critical edge cases before optimizer automation: sample-size thresholds (`49/50`, `99/100`, `9/10`), warm-up exclusions, boundary force-closes, lockbox usage caps, and shadow extension behavior.
