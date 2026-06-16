#!/usr/bin/env python3
"""Driver-based scenario & sensitivity modeling — pure stdlib, reproducible.

A small, auditable engine for the my-claude-bot fleet:
  - a driver-based model:  profit = (price - unit_cogs) * volume - fixed
  - three_cases():         base / bull / bear via coherent driver shifts
  - one_way_sensitivity(): tornado-ranked drivers (low/high swing on each)
  - monte_carlo():         triangular draws per driver -> P10/P50/P90

Design goals: correct, deterministic (FIXED seed), zero third-party deps.
Range beats a point estimate; assumptions are explicit; never sell
Monte-Carlo precision as certainty.

Run (cwd = agents/me/memory):
    python3 ../skills/scenario-modeling/scripts/scenario.py --self-test
"""
from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable

SEED = 20260616  # fixed for reproducibility — same report every run


# --------------------------------------------------------------------------- #
# Model: drivers -> outcome
# --------------------------------------------------------------------------- #
@dataclass
class Driver:
    """One model input with a base value and a plausible low/high range.

    low/high are the *coherent* downside/upside for this driver in isolation
    (the same numbers feed bull/bear cases, one-way sensitivity, and the
    Monte-Carlo triangular distribution — single source of truth).
    """
    name: str
    base: float
    low: float
    high: float
    # direction: +1 means "higher value is good for the outcome", -1 means bad.
    # Used so bull/bear pick the *favourable*/*unfavourable* end coherently.
    direction: int = 1
    unit: str = ""

    def bull_value(self) -> float:
        return self.high if self.direction > 0 else self.low

    def bear_value(self) -> float:
        return self.low if self.direction > 0 else self.high


def profit_model(price: float, volume: float, unit_cogs: float,
                 fixed: float) -> float:
    """profit = (price - unit_cogs) * volume - fixed.  Pure, no side effects."""
    return (price - unit_cogs) * volume - fixed


@dataclass
class Model:
    """Bundles named drivers with the function that turns them into a number."""
    drivers: dict[str, Driver]
    fn: Callable[..., float]

    def evaluate(self, values: dict[str, float]) -> float:
        return self.fn(**values)

    def base_values(self) -> dict[str, float]:
        return {n: d.base for n, d in self.drivers.items()}


# --------------------------------------------------------------------------- #
# 1) Three cases: base / bull / bear (coherent driver shifts)
# --------------------------------------------------------------------------- #
def three_cases(model: Model) -> dict[str, float]:
    base = model.evaluate(model.base_values())
    bull_vals = {n: d.bull_value() for n, d in model.drivers.items()}
    bear_vals = {n: d.bear_value() for n, d in model.drivers.items()}
    return {
        "bear": model.evaluate(bear_vals),
        "base": base,
        "bull": model.evaluate(bull_vals),
    }


# --------------------------------------------------------------------------- #
# 2) One-way sensitivity (tornado): swing each driver low<->high alone
# --------------------------------------------------------------------------- #
@dataclass
class TornadoRow:
    driver: str
    low_outcome: float   # outcome with this driver at its low, others at base
    high_outcome: float  # outcome with this driver at its high, others at base
    swing: float = field(init=False)  # |high_outcome - low_outcome|

    def __post_init__(self) -> None:
        self.swing = abs(self.high_outcome - self.low_outcome)


def one_way_sensitivity(model: Model) -> list[TornadoRow]:
    """Vary ONE driver at a time across its [low, high] band, others at base.

    Returns rows sorted by swing descending — the tornado ranking. The top
    driver is the one the outcome is most sensitive to: that's where to spend
    diligence, negotiate, or place a hedge/trigger.
    """
    base_vals = model.base_values()
    rows: list[TornadoRow] = []
    for name, d in model.drivers.items():
        lo_vals = dict(base_vals); lo_vals[name] = d.low
        hi_vals = dict(base_vals); hi_vals[name] = d.high
        rows.append(TornadoRow(
            driver=name,
            low_outcome=model.evaluate(lo_vals),
            high_outcome=model.evaluate(hi_vals),
        ))
    rows.sort(key=lambda r: r.swing, reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# 3) Monte Carlo: triangular draws per driver -> distribution of outcome
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0,1]). Matches numpy 'linear'."""
    if not sorted_vals:
        raise ValueError("empty sample")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


@dataclass
class MonteCarloResult:
    n: int
    p10: float
    p50: float
    p90: float
    mean: float
    stdev: float
    prob_negative: float  # share of trials with outcome < 0 (loss probability)


def monte_carlo(model: Model, trials: int = 10_000,
                seed: int = SEED) -> MonteCarloResult:
    """Draw each driver from a triangular(low, base, high) per trial.

    triangular(low, high, mode=base): the mode is the base case, the band
    edges are the same low/high used everywhere else. Independent draws per
    driver (no correlation modeled — see Границы in SKILL.md).
    """
    rng = random.Random(seed)  # local RNG; global state untouched, reproducible
    outcomes: list[float] = []
    for _ in range(trials):
        vals = {}
        for name, d in model.drivers.items():
            lo, hi = sorted((d.low, d.high))
            mode = min(max(d.base, lo), hi)
            vals[name] = rng.triangular(lo, hi, mode)
        outcomes.append(model.evaluate(vals))
    outcomes.sort()
    neg = sum(1 for o in outcomes if o < 0) / len(outcomes)
    return MonteCarloResult(
        n=trials,
        p10=_percentile(outcomes, 0.10),
        p50=_percentile(outcomes, 0.50),
        p90=_percentile(outcomes, 0.90),
        mean=statistics.fmean(outcomes),
        stdev=statistics.pstdev(outcomes),
        prob_negative=neg,
    )


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _money(x: float) -> str:
    return f"{x:,.0f}"


def format_report(model: Model, *, trials: int = 10_000) -> str:
    cases = three_cases(model)
    tornado = one_way_sensitivity(model)
    mc = monte_carlo(model, trials=trials)
    base = cases["base"]

    out: list[str] = []
    out.append("=" * 64)
    out.append("DRIVER-BASED SCENARIO REPORT")
    out.append("=" * 64)

    out.append("\nDrivers & assumptions:")
    out.append(f"  {'driver':<12}{'base':>12}{'low':>12}{'high':>12}  dir")
    for n, d in model.drivers.items():
        arrow = "+" if d.direction > 0 else "-"
        out.append(f"  {n:<12}{_money(d.base):>12}{_money(d.low):>12}"
                   f"{_money(d.high):>12}  {arrow}")

    out.append("\nThree cases (coherent driver shifts):")
    out.append(f"  {'case':<8}{'outcome':>14}{'vs base':>12}")
    for case in ("bear", "base", "bull"):
        val = cases[case]
        if case == "base":
            delta = "—"
        elif base:
            delta = f"{(val - base) / abs(base) * 100:+.0f}%"
        else:
            delta = "n/a"
        out.append(f"  {case:<8}{_money(val):>14}{delta:>12}")

    out.append("\nTornado — one-way sensitivity (swing = |high - low|):")
    out.append(f"  {'driver':<12}{'low_out':>13}{'high_out':>13}{'swing':>13}")
    for r in tornado:
        out.append(f"  {r.driver:<12}{_money(r.low_outcome):>13}"
                   f"{_money(r.high_outcome):>13}{_money(r.swing):>13}")
    top = tornado[0].driver
    out.append(f"  -> outcome is MOST sensitive to: {top}")

    out.append(f"\nMonte Carlo ({mc.n:,} trials, triangular, seed={SEED}):")
    out.append(f"  P10 = {_money(mc.p10)}")
    out.append(f"  P50 = {_money(mc.p50)}   (median)")
    out.append(f"  P90 = {_money(mc.p90)}")
    out.append(f"  mean = {_money(mc.mean)}   stdev = {_money(mc.stdev)}")
    out.append(f"  P(outcome < 0) = {mc.prob_negative * 100:.1f}%")
    out.append("=" * 64)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Built-in example + self-test
# --------------------------------------------------------------------------- #
def example_model() -> Model:
    """A SaaS-ish unit-economics example (annual, abstract money units)."""
    drivers = {
        # price per unit: base 1000, can swing 850..1150
        "price": Driver("price", base=1000, low=850, high=1150, direction=1),
        # volume (units sold): base 12000, 9000..16000
        "volume": Driver("volume", base=12000, low=9000, high=16000, direction=1),
        # unit_cogs: base 600, 520..700 -> higher cogs is BAD (direction=-1)
        "unit_cogs": Driver("unit_cogs", base=600, low=520, high=700, direction=-1),
        # fixed cost: base 3_000_000, 2_600_000..3_600_000 -> higher is BAD
        "fixed": Driver("fixed", base=3_000_000, low=2_600_000,
                        high=3_600_000, direction=-1),
    }
    return Model(drivers=drivers, fn=profit_model)


def self_test() -> int:
    """Deterministic checks on the built-in example. Returns process exit code."""
    model = example_model()
    failures: list[str] = []

    # --- exact arithmetic on the base case ---
    cases = three_cases(model)
    base = cases["base"]
    # (1000-600)*12000 - 3_000_000 = 4_800_000 - 3_000_000 = 1_800_000
    if base != 1_800_000:
        failures.append(f"base profit expected 1_800_000, got {base}")

    # bull: price 1150, vol 16000, cogs 520, fixed 2_600_000
    expected_bull = (1150 - 520) * 16000 - 2_600_000  # = 7_480_000
    if cases["bull"] != expected_bull:
        failures.append(f"bull expected {expected_bull}, got {cases['bull']}")
    # bear: price 850, vol 9000, cogs 700, fixed 3_600_000
    expected_bear = (850 - 700) * 9000 - 3_600_000  # = -2_250_000
    if cases["bear"] != expected_bear:
        failures.append(f"bear expected {expected_bear}, got {cases['bear']}")
    if not (cases["bear"] < cases["base"] < cases["bull"]):
        failures.append("ordering bear < base < bull violated")

    # --- tornado ranking is sorted descending and swings are non-negative ---
    tornado = one_way_sensitivity(model)
    swings = [r.swing for r in tornado]
    if swings != sorted(swings, reverse=True):
        failures.append("tornado not sorted by swing desc")
    if any(s < 0 for s in swings):
        failures.append("negative swing encountered")
    # manual swing check (vary one driver low<->high, others at base)
    manual = {
        "price": abs((1150 - 600) * 12000 - (850 - 600) * 12000),       # 3_600_000
        "volume": abs((1000 - 600) * 16000 - (1000 - 600) * 9000),      # 2_800_000
        "unit_cogs": abs((1000 - 700) * 12000 - (1000 - 520) * 12000),  # 2_160_000
        "fixed": abs(3_600_000 - 2_600_000),                            # 1_000_000
    }
    expected_top = max(manual, key=manual.get)
    if tornado[0].driver != expected_top:
        failures.append(
            f"top tornado driver expected {expected_top}, got {tornado[0].driver}")
    for r in tornado:
        if round(r.swing) != manual[r.driver]:
            failures.append(
                f"swing for {r.driver} expected {manual[r.driver]}, got {r.swing}")

    # --- Monte Carlo: reproducible across two runs with same seed ---
    mc1 = monte_carlo(model, trials=5000, seed=SEED)
    mc2 = monte_carlo(model, trials=5000, seed=SEED)
    if (mc1.p10, mc1.p50, mc1.p90) != (mc2.p10, mc2.p50, mc2.p90):
        failures.append("Monte Carlo not reproducible with fixed seed")
    if not (mc1.p10 < mc1.p50 < mc1.p90):
        failures.append("percentile ordering P10<P50<P90 violated")
    # P50 should sit inside the deterministic bear..bull band
    if not (cases["bear"] < mc1.p50 < cases["bull"]):
        failures.append("MC median outside bear..bull band")

    print(format_report(model))
    print()
    if failures:
        print("SELF-TEST: FAIL")
        for f in failures:
            print(f"  x {f}")
        return 1
    print("SELF-TEST: PASS  (all invariants hold, Monte Carlo reproducible)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Driver-based scenario & sensitivity modeling (stdlib).")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in business example and verify invariants")
    p.add_argument("--trials", type=int, default=10_000,
                   help="Monte Carlo trials for the report (default 10000)")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    # default: print the example report (no test assertions)
    print(format_report(example_model(), trials=args.trials))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
