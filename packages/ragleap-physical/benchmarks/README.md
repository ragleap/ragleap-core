# Repeatability experiment (AI-assisted)

Run `python -m pytest -q benchmarks/test_repeatability.py` and `python benchmarks/repeatability.py --runs 3 --steps 100 --output benchmarks/result.json` in each environment. Preserve each JSON report separately; compare `sha256` arrays only when the XML, inputs, seed metadata, and step count match. Record Python, MuJoCo and platform from each report. Identical hashes demonstrate bitwise agreement for the sampled trajectory only, not a general cross-version or cross-machine guarantee. The seed is metadata because the arm model has no stochastic elements. No network or hardware.

| Environment | Python | MuJoCo | Same-environment exact | Cross-environment exact |
|---|---|---|---|---|
| Colab run 1 | pending | pending | unverified | unverified |
| Colab run 2 (different version, if available) | pending | pending | unverified | unverified |
| Python 3.10 runner | pending | pending | unverified | unverified |
