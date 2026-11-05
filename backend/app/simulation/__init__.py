"""Scenario simulation what-if intelligence layer.

THIS IS SCENARIO ANALYSIS, NOT A PERFECT PHYSICAL DIGITAL TWIN. Nothing in
this package claims physical realism, invents a sensor relationship, or
asserts that a hypothetical scenario represents a guaranteed future — see
each module's docstring for the specific, narrow claim it does make.

Sub-modules:
  perturbations  Pure, deterministic, non-destructive value transforms —
                 the six scenario types. Never mutates the input Series.
  validation     Rejects unsafe/malformed scenario configs BEFORE any
                 simulation runs. Never silently clamps a value.
  pipeline       Runs the SAME analytical functions app.anomaly/app.condition
                 use on real data, unmodified, against a simulated Series.
  comparison     Baseline vs. scenario diffing and magnitude-sweep
                 sensitivity analysis. No fabricated combined risk score.
"""
