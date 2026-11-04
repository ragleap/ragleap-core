"""evaluate: compare quality and input tokens with and without an Optimizer, on your own cases."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ragleap_security import Scope

from .model import InvalidRequestError
from .optimizer import Optimizer


@dataclass(frozen=True)
class EvalTable:
    n: int
    n_errors: int
    min_cases: int
    tokens_before: int
    tokens_after: int
    mean_baseline: float
    mean_optimized: float
    delta: float
    delta_low: float
    delta_high: float
    regressions: Tuple[int, ...]
    lossy_cases: int
    error_kinds: Tuple[str, ...] = ()

    @property
    def enough(self) -> bool:
        return self.n >= self.min_cases

    def summary(self) -> str:
        lines = [f"cases: {self.n} scored, {self.n_errors} failed" + (f" ({', '.join(sorted(set(self.error_kinds)))})" if self.error_kinds else "")]
        if not self.enough:
            lines.append(f"Too few cases for a savings claim (have {self.n}, need {self.min_cases}); no figures are given.")
        else:
            saved = 100.0 * (1 - self.tokens_after / self.tokens_before) if self.tokens_before else 0.0
            lines.append(f"input tokens: {self.tokens_before} -> {self.tokens_after} ({saved:.1f}% fewer)")
            lines.append(f"quality: baseline {self.mean_baseline:.3f}, optimized {self.mean_optimized:.3f}, "
                         f"change {self.delta:+.3f} (95% interval {self.delta_low:+.3f} to {self.delta_high:+.3f})")
        lines.append(f"cases where a lossy step changed the request: {self.lossy_cases}")
        lines.append("cases where the optimized answer scored lower: "
                     + (", ".join(str(i) for i in self.regressions) if self.regressions else "none"))
        lines.append("Input tokens only (answers are not counted); counts are estimates unless you gave a real counter.")
        return "\n".join(lines)


def _score(judge: Callable[..., Any], case: Any, response: Any) -> float:
    s = judge(case, response)
    if isinstance(s, bool):
        return 1.0 if s else 0.0
    if not isinstance(s, (int, float)) or not 0 <= s <= 1:
        raise InvalidRequestError("judge must return a bool or a number from 0 to 1")
    return float(s)


def evaluate(cases: Sequence[Dict[str, Any]], run: Callable[[Dict[str, Any]], Any], optimizer: Optimizer,
             judge: Callable[[Any, Any], Any], *, scope: Optional[Scope] = None, min_cases: int = 30,
             bootstrap: int = 1000, seed: int = 0) -> EvalTable:
    """Each case is a dict with a "request" and anything your judge needs (for example "expected").
    For every case: run the original request and the optimized one through `run`, score both answers with
    `judge(case, response)` (a bool or a number from 0 to 1), and count input tokens. A case where `run`,
    the optimizer or the judge raises counts as failed and is left out of the quality figures.
    The 95% interval is a seeded bootstrap of the paired quality differences."""
    if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:
        raise InvalidRequestError("min_cases must be a positive int")
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:
        raise InvalidRequestError("bootstrap must be a positive int")
    base: List[float] = []
    opt: List[float] = []
    kinds: List[str] = []
    tb = ta = lossy = 0
    regress: List[int] = []
    for i, case in enumerate(cases):
        try:
            result = optimizer.run(case["request"], scope)
            sb = _score(judge, case, run(case["request"]))
            so = _score(judge, case, run(result.request))
        except Exception as e:
            kinds.append(type(e).__name__)
            continue
        base.append(sb)
        opt.append(so)
        tb += result.report.tokens_before
        ta += result.report.tokens_after
        lossy += 1 if result.report.lossy else 0
        if so < sb:
            regress.append(i)
    n = len(base)
    diffs = [o - b for o, b in zip(opt, base)]
    mb = sum(base) / n if n else 0.0
    mo = sum(opt) / n if n else 0.0
    if n:
        rng = random.Random(seed)
        means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(bootstrap))
        low, high = means[int(0.025 * (bootstrap - 1))], means[int(0.975 * (bootstrap - 1))]
    else:
        low = high = 0.0
    return EvalTable(n, len(kinds), min_cases, tb, ta, mb, mo, mo - mb, low, high, tuple(regress), lossy, tuple(kinds))
