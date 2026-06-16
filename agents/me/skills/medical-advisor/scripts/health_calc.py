#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_calc.py — deterministic educational health calculators.

Pure Python 3 standard library ONLY (argparse, math, json, sys). No third-party deps.

ОГОВОРКА / EDUCATIONAL CAVEAT
=============================
Every value this tool prints is **general educational information** — NOT a personal
diagnosis and NOT a prescription. The treating physician (and a pharmacist for drug
interactions) makes the final call on diagnosis, drug choice, and exact dose given the
person's full clinical picture (contraindications, interactions, current meds, labs,
comorbidities, allergies, kidney/liver function, frailty, pregnancy/lactation).

Age-appropriate by design:
  * Children/teens (2–19 y) are assessed by **BMI-for-age percentile / z-score**,
    NEVER adult kg/m² cutoffs (25 / 30). Plot on the official growth chart.
  * Adults: standard WHO BMI cut-points.
  * Older adults: a slightly higher BMI can be protective; targets are individualized.

CORRECTNESS OVER COMPLETENESS. The per-age LMS constants for the full WHO BMI-for-age
z-score table (30 age/sex cells, 5–19 y) could NOT be verified cell-by-cell against the
official WHO source, so this tool DELIBERATELY does not embed them and does not compute a
z-score/percentile from unverified constants. Emitting a child's BMI percentile from a
fabricated or approximate LMS triple is unsafe — a wrong median or spread can push a
genuinely overweight child into the "healthy" band right at the +1/+2 SD cut-offs. Instead
the tool degrades gracefully: it reports the raw BMI + age + sex, classifies adult-style
cut-offs as NOT applicable to minors, and directs you to plot the value on the official WHO
BMI-for-age chart (or use a validated WHO Anthro / AnthroPlus tool). The only LMS numbers
present are the THREE worked examples published verbatim in the WHO computation note, used
purely to document the method and validated in self-test against WHO's own answers.

Sources (current as of June 2026):
  * WHO adult BMI classification (WHO, current).
  * WHO Growth Reference 2007, BMI-for-age 5–19 y, LMS method
    (de Onis et al., Bull World Health Organ 2007;85:660-667). z = ((BMI/M)**L - 1)/(L*S);
    per-age L/M/S come from the official WHO tables (NOT reproduced here — see above).
    Three verbatim worked examples from WHO "Computation of centiles and z-scores"
    (boy 9y BMI19 -> z 1.47; boy 11y BMI30 -> z 3.24; boy 16y BMI14 -> z -3.96) are
    embedded only to validate the formula.
  * Mifflin–St Jeor (1990) for BMR; standard activity multipliers for TDEE.
  * PROT-AGE (Bauer et al., JAMDA 2013) and ESPEN for protein targets.
  * Waist-to-height ratio ≥ 0.5 = elevated central-adiposity risk (NICE CG189, current).
  * CKD-EPI creatinine 2021 race-free equation (Inker et al., NEJM 2021; adopted by
    KDIGO 2024 CKD guideline).
  * ADAG / A1c–eAG relationship: eAG(mg/dL) = 28.7 × A1c − 46.7 (Nathan et al.,
    Diabetes Care 2008; used by ADA Standards of Care in Diabetes 2026).
  * Glucose SI conversion: mmol/L × 18.0156 ≈ mg/dL.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# ---------------------------------------------------------------------------
# Caveat strings
# ---------------------------------------------------------------------------

CAVEAT_SHORT = (
    "Образовательно, не диагноз / Educational, not a diagnosis. "
    "Confirm any clinical decision with the treating clinician."
)

CAVEAT_PEDS = (
    "Образовательно, не диагноз. This reports a child's RAW BMI only — it does NOT "
    "assign a weight category. Children/teens are assessed by BMI-for-age "
    "PERCENTILE/z-score on the official WHO/CDC growth chart (never adult cut-offs), "
    "alongside growth trajectory and puberty stage. Confirm with the pediatric "
    "clinician; never restrict or calorie-shame a minor."
)

CAVEAT_EGFR = (
    "Образовательно, не диагноз. eGFR is an estimate; a single value does not stage "
    "CKD (KDIGO needs >=2 values >=90 days apart + albuminuria) and the equation is not "
    "validated in pregnancy, acute kidney injury, extremes of muscle mass, or "
    "amputees. Confirm with the treating clinician."
)


# ---------------------------------------------------------------------------
# WHO 2007 BMI-for-age LMS — METHOD + VERIFIED WORKED EXAMPLES ONLY.
#
# We intentionally DO NOT embed the full 5–19 y per-age LMS table. The official
# WHO constants could not be verified cell-by-cell from an authoritative source,
# and an approximate/fabricated LMS triple yields a wrong child percentile right
# at the overweight/obesity cut-offs (a safety hazard). bmi_for_age() therefore
# reports raw BMI + age/sex and defers the percentile to the official WHO chart.
#
# The constants below are the THREE worked examples published *verbatim* in the
# WHO note "Computation of centiles and z-scores for height-for-age,
# weight-for-age and BMI-for-age" (WHO Growth Reference 5–19 y). They are used
# ONLY to validate the LMS z-score formula in the self-test, never to score a
# real input. Each tuple is (age_years, sex, BMI, L, M, S, expected_z).
#   z = ((BMI/M)**L - 1) / (L*S)
# ---------------------------------------------------------------------------

_WHO_LMS_WORKED_EXAMPLES = (
    # WHO computation note, Figure 1 — values quoted exactly from the source.
    (9,  "male", 19.0, -1.6318, 16.0490, 0.10038, 1.47),   # Child 3 (pure LMS)
    (11, "male", 30.0, -1.7862, 16.9392, 0.11070, 3.24),   # Child 1 (LMS, pre-cap)
    (16, "male", 14.0, -1.3529, 20.4951, 0.12579, -3.96),  # Child 2 (LMS, pre-cap)
)


def _lms_zscore(bmi_value: float, L: float, M: float, S: float) -> float:
    """WHO LMS z-score: z = ((BMI/M)**L - 1)/(L*S); log form when L≈0."""
    if abs(L) < 1e-9:
        return math.log(bmi_value / M) / S
    return ((bmi_value / M) ** L - 1.0) / (L * S)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _require_positive(name: str, value: float) -> None:
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number (got {value!r})")


def _round(x: float, ndigits: int = 2):
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. BMI + adult classification (WHO)
# ---------------------------------------------------------------------------

def bmi(weight_kg: float, height_m: float) -> dict:
    """Body mass index = weight(kg) / height(m)^2, with WHO adult classification.

    Adult classification applies only to ages ~18+; for children use bmi_for_age().
    """
    _require_positive("weight_kg", weight_kg)
    _require_positive("height_m", height_m)
    if height_m > 3.0:
        raise ValueError(
            f"height_m={height_m} looks like centimetres — pass metres (e.g. 1.75)"
        )

    value = weight_kg / (height_m ** 2)

    # WHO adult cut-points (kg/m^2).
    if value < 16.0:
        cls = "underweight (severe thinness, WHO)"
    elif value < 17.0:
        cls = "underweight (moderate thinness, WHO)"
    elif value < 18.5:
        cls = "underweight (mild thinness, WHO)"
    elif value < 25.0:
        cls = "normal weight (WHO)"
    elif value < 30.0:
        cls = "overweight / pre-obesity (WHO)"
    elif value < 35.0:
        cls = "obesity class I (WHO)"
    elif value < 40.0:
        cls = "obesity class II (WHO)"
    else:
        cls = "obesity class III (WHO)"

    return {
        "metric": "bmi",
        "bmi": _round(value, 1),
        "classification": cls,
        "note": (
            "Adult WHO cut-points (kg/m^2). Asian-population risk rises earlier "
            "(overweight >=23, obesity >=27.5). NOT valid for children/teens — "
            "use bmi-for-age. BMI does not distinguish muscle from fat."
        ),
        "caveat": CAVEAT_SHORT,
    }


# ---------------------------------------------------------------------------
# 2. BMI-for-age (children/teens 5–19 y) — WHO LMS z-score + percentile
# ---------------------------------------------------------------------------

def bmi_for_age(
    weight_kg: float,
    height_m: float,
    age_years: float,
    sex: str,
) -> dict:
    """BMI-for-age for children/teens — SAFE FALLBACK ONLY (no fabricated percentile).

    Children and teens (roughly 2–19 y) are assessed by BMI-for-age PERCENTILE /
    z-score against the WHO 2007 reference, NEVER by the adult kg/m² cut-offs
    (25 / 30). Computing that percentile needs the official per-age WHO LMS
    constants. Those constants are NOT embedded here (they could not be verified
    cell-by-cell, and an approximate triple misclassifies a child near the
    overweight/obesity cut-off — a safety hazard). So this function deliberately
    returns the raw BMI + age + sex and tells you to plot it on the official WHO
    BMI-for-age chart or run WHO AnthroPlus. It does NOT invent a z-score.

    The WHO 5–19 y z-score bands (for reading the chart) are:
        < -3 SD severe thinness | -3..-2 thinness | -2..+1 healthy
        > +1..+2 overweight     | > +2 obesity
    """
    _require_positive("weight_kg", weight_kg)
    _require_positive("height_m", height_m)
    if height_m > 3.0:
        raise ValueError(
            f"height_m={height_m} looks like centimetres — pass metres (e.g. 1.40)"
        )
    if age_years is None or not math.isfinite(age_years) or age_years < 0:
        raise ValueError(f"age_years must be a non-negative number (got {age_years!r})")

    sex_norm = (sex or "").strip().lower()
    if sex_norm in ("m", "male", "boy", "boys"):
        sex_norm = "male"
    elif sex_norm in ("f", "female", "girl", "girls"):
        sex_norm = "female"
    else:
        raise ValueError("sex must be one of: male/female (m/f)")

    raw_bmi = weight_kg / (height_m ** 2)

    # Which reference applies, so the user picks the right official chart.
    if age_years < 5.0:
        chart = (
            "Use the WHO Child Growth Standards (0-5 y) BMI-for-age / weight-for-length."
        )
    elif age_years <= 19.0:
        chart = (
            "Plot on the official WHO 2007 BMI-for-age chart (5-19 y) "
            "or run WHO AnthroPlus to get the exact z-score/percentile."
        )
    else:
        chart = "Age 19+ — use the adult BMI cut-offs (see the 'bmi' command)."

    return {
        "metric": "bmi_for_age",
        "bmi": _round(raw_bmi, 1),
        "age_years": _round(age_years, 2),
        "sex": sex_norm,
        "z_score": None,
        "percentile": None,
        "classification": "see_official_chart",
        "method": "raw BMI only — percentile deferred to official WHO chart",
        "how_to_read": chart,
        "note": (
            "Adult BMI cut-offs (25/30 kg/m^2) DO NOT apply to a minor. A child's "
            "weight status is read as a BMI-for-age percentile/z-score against the WHO "
            "reference, in the context of growth trajectory and puberty stage. NO "
            "calorie-shaming of a minor: any concern goes to the pediatric clinician / "
            "dietitian, who screens for disordered eating before any plan."
        ),
        "caveat": CAVEAT_PEDS,
    }


# ---------------------------------------------------------------------------
# 3. BMR (Mifflin–St Jeor) + TDEE
# ---------------------------------------------------------------------------

_ACTIVITY_FACTORS = {
    "sedentary": 1.2,        # little/no exercise
    "light": 1.375,          # 1-3 d/wk
    "moderate": 1.55,        # 3-5 d/wk
    "active": 1.725,         # 6-7 d/wk
    "very_active": 1.9,      # hard daily / physical job
}


def bmr(weight_kg: float, height_cm: float, age_years: float, sex: str) -> dict:
    """Basal metabolic rate via Mifflin-St Jeor (1990).

    BMR = 10*kg + 6.25*cm - 5*age + s   (s = +5 male, -161 female), kcal/day.
    """
    _require_positive("weight_kg", weight_kg)
    _require_positive("height_cm", height_cm)
    if age_years is None or not math.isfinite(age_years) or age_years < 0:
        raise ValueError(f"age_years must be a non-negative number (got {age_years!r})")
    if height_cm < 3.0:
        raise ValueError(
            f"height_cm={height_cm} looks like metres — pass centimetres (e.g. 175)"
        )

    sex_norm = (sex or "").strip().lower()
    if sex_norm in ("m", "male", "boy"):
        sex_norm, s_const = "male", 5.0
    elif sex_norm in ("f", "female", "girl"):
        sex_norm, s_const = "female", -161.0
    else:
        raise ValueError("sex must be one of: male/female (m/f)")

    value = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years + s_const

    return {
        "metric": "bmr",
        "bmr_kcal_day": _round(value, 0),
        "equation": "Mifflin-St Jeor (1990)",
        "sex": sex_norm,
        "note": (
            "Mifflin-St Jeor is validated for adults; in children/teens energy needs "
            "follow growth + activity (e.g. NASEM 2023 DRI), so treat pediatric output "
            "as rough only."
        ),
        "caveat": CAVEAT_SHORT,
    }


def tdee(
    weight_kg: float,
    height_cm: float,
    age_years: float,
    sex: str,
    activity: str,
) -> dict:
    """Total daily energy expenditure = BMR * activity factor."""
    act = (activity or "").strip().lower()
    if act not in _ACTIVITY_FACTORS:
        raise ValueError(
            "activity must be one of: " + ", ".join(sorted(_ACTIVITY_FACTORS))
        )

    base = bmr(weight_kg, height_cm, age_years, sex)
    factor = _ACTIVITY_FACTORS[act]
    value = base["bmr_kcal_day"] * factor

    return {
        "metric": "tdee",
        "bmr_kcal_day": base["bmr_kcal_day"],
        "activity_level": act,
        "activity_factor": factor,
        "tdee_kcal_day": _round(value, 0),
        "note": (
            "Estimated maintenance energy. Activity multipliers are population averages "
            "(+/-10-15%). Calorie goals for minors must avoid restriction/shaming and "
            "go through the pediatric clinician/dietitian."
        ),
        "caveat": CAVEAT_SHORT,
    }


# ---------------------------------------------------------------------------
# 4. Protein target (g/kg/day) by population
# ---------------------------------------------------------------------------

_PROTEIN_TARGETS = {
    # population -> (g_per_kg_low, g_per_kg_high, label/source)
    "general":      (0.8, 0.8, "RDA sedentary adult (0.8 g/kg/d)"),
    "active":       (1.2, 2.0, "Active/athlete (ACSM/ISSN 1.2-2.0 g/kg/d)"),
    "older_adult":  (1.0, 1.2, "Older adult (PROT-AGE / ESPEN 1.0-1.2 g/kg/d; "
                               "1.2-1.5 if acute/chronic illness)"),
}


def protein_target(weight_kg: float, population: str) -> dict:
    """Daily protein target range by population, in grams/day."""
    _require_positive("weight_kg", weight_kg)
    pop = (population or "").strip().lower()
    if pop not in _PROTEIN_TARGETS:
        raise ValueError(
            "population must be one of: " + ", ".join(sorted(_PROTEIN_TARGETS))
        )

    lo, hi, label = _PROTEIN_TARGETS[pop]
    return {
        "metric": "protein_target",
        "population": pop,
        "g_per_kg_day": [lo, hi] if lo != hi else [lo],
        "grams_per_day": [_round(lo * weight_kg, 0), _round(hi * weight_kg, 0)]
        if lo != hi else [_round(lo * weight_kg, 0)],
        "basis": label,
        "note": (
            "CKD caution: in advanced CKD NOT on dialysis, protein is often RESTRICTED "
            "(KDIGO 2024: ~0.55-0.6 g/kg/d, or 0.28-0.43 with keto-analogues) — these "
            "general ranges do NOT apply; defer to the nephrology team. Dialysis raises "
            "needs again (~1.0-1.2). Pediatric needs are weight/age-specific (DRI), not "
            "these adult ranges."
        ),
        "caveat": CAVEAT_SHORT,
    }


# ---------------------------------------------------------------------------
# 5. Waist-to-height ratio
# ---------------------------------------------------------------------------

def waist_to_height_ratio(waist_cm: float, height_cm: float) -> dict:
    """Waist-to-height ratio; >= 0.5 flags elevated central-adiposity risk (NICE)."""
    _require_positive("waist_cm", waist_cm)
    _require_positive("height_cm", height_cm)

    ratio = waist_cm / height_cm
    if ratio < 0.4:
        cls = "below healthy range — consider underweight/low central fat"
    elif ratio < 0.5:
        cls = "healthy (< 0.5)"
    elif ratio < 0.6:
        cls = "increased central-adiposity risk (0.5-0.6)"
    else:
        cls = "high central-adiposity risk (>= 0.6)"

    return {
        "metric": "waist_to_height_ratio",
        "ratio": _round(ratio, 3),
        "classification": cls,
        "threshold": ">= 0.5 = elevated risk (NICE, 'keep waist < half your height')",
        "note": (
            "Simple central-adiposity screen across ages incl. children >=5 y "
            "(the 0.5 rule applies broadly). Measure waist at the midpoint between "
            "lowest rib and iliac crest."
        ),
        "caveat": CAVEAT_SHORT,
    }


# ---------------------------------------------------------------------------
# 6. eGFR — CKD-EPI creatinine 2021 (race-free)
# ---------------------------------------------------------------------------

def egfr(creatinine_mg_dl: float, age_years: float, sex: str) -> dict:
    """Estimated GFR via CKD-EPI creatinine 2021 race-free equation.

    eGFR = 142 * min(Scr/k,1)^a * max(Scr/k,1)^-1.200 * 0.9938^age * (1.012 if female)
      female: k=0.7, a=-0.241 ; male: k=0.9, a=-0.302   (Scr in mg/dL)
    Units: mL/min/1.73 m^2.
    """
    _require_positive("creatinine_mg_dl", creatinine_mg_dl)
    if age_years is None or not math.isfinite(age_years) or age_years <= 0:
        raise ValueError(f"age_years must be a positive number (got {age_years!r})")

    sex_norm = (sex or "").strip().lower()
    if sex_norm in ("f", "female"):
        sex_norm, kappa, alpha, female_factor = "female", 0.7, -0.241, 1.012
    elif sex_norm in ("m", "male"):
        sex_norm, kappa, alpha, female_factor = "male", 0.9, -0.302, 1.0
    else:
        raise ValueError("sex must be one of: male/female (m/f)")

    scr_k = creatinine_mg_dl / kappa
    value = (
        142.0
        * (min(scr_k, 1.0) ** alpha)
        * (max(scr_k, 1.0) ** -1.200)
        * (0.9938 ** age_years)
        * female_factor
    )

    if value >= 90:
        stage = "G1 (normal/high) — needs kidney-damage marker (albuminuria) to be CKD"
    elif value >= 60:
        stage = "G2 (mildly decreased) — needs damage marker to be CKD"
    elif value >= 45:
        stage = "G3a (mild-moderate decrease)"
    elif value >= 30:
        stage = "G3b (moderate-severe decrease)"
    elif value >= 15:
        stage = "G4 (severe decrease)"
    else:
        stage = "G5 (kidney failure)"

    return {
        "metric": "egfr",
        "egfr_ml_min_1_73m2": _round(value, 1),
        "kdigo_gfr_category": stage,
        "equation": "CKD-EPI creatinine 2021 (race-free)",
        "sex": sex_norm,
        "note": (
            "CKD-EPI 2021 is the KDIGO-2024-endorsed adult equation; it is NOT for "
            "children (use a pediatric equation, e.g. CKiD U25/Schwartz). Pair with "
            "urine ACR for staging."
        ),
        "caveat": CAVEAT_EGFR,
    }


# ---------------------------------------------------------------------------
# 7. A1c <-> eAG  +  glucose unit conversion
# ---------------------------------------------------------------------------

def a1c_to_eag(a1c_percent: float) -> dict:
    """Estimated average glucose from HbA1c. eAG(mg/dL) = 28.7*A1c - 46.7 (ADAG)."""
    if a1c_percent is None or not math.isfinite(a1c_percent) or a1c_percent <= 0:
        raise ValueError(f"a1c_percent must be a positive number (got {a1c_percent!r})")

    eag_mgdl = 28.7 * a1c_percent - 46.7
    if eag_mgdl <= 0:
        raise ValueError("computed eAG is non-physiologic; check the A1c input")

    return {
        "metric": "a1c_to_eag",
        "a1c_percent": _round(a1c_percent, 2),
        "eag_mg_dl": _round(eag_mgdl, 0),
        "eag_mmol_l": _round(eag_mgdl / 18.0156, 1),
        "formula": "eAG(mg/dL) = 28.7 x A1c - 46.7 (ADAG; ADA 2026)",
        "note": (
            "A1c is unreliable with anemia, hemoglobinopathy, recent transfusion, "
            "pregnancy, CKD, or high RBC turnover — confirm with glucose-based testing. "
            "Diagnosis needs 2 abnormal results (ADA 2026)."
        ),
        "caveat": CAVEAT_SHORT,
    }


def eag_to_a1c(eag_mg_dl: float) -> dict:
    """HbA1c from estimated average glucose. A1c = (eAG + 46.7) / 28.7 (ADAG)."""
    if eag_mg_dl is None or not math.isfinite(eag_mg_dl) or eag_mg_dl <= 0:
        raise ValueError(f"eag_mg_dl must be a positive number (got {eag_mg_dl!r})")

    a1c = (eag_mg_dl + 46.7) / 28.7
    return {
        "metric": "eag_to_a1c",
        "eag_mg_dl": _round(eag_mg_dl, 0),
        "eag_mmol_l": _round(eag_mg_dl / 18.0156, 1),
        "a1c_percent": _round(a1c, 2),
        "formula": "A1c = (eAG[mg/dL] + 46.7) / 28.7 (ADAG; ADA 2026)",
        "note": (
            "Inverse of the ADAG relationship; same A1c reliability caveats apply."
        ),
        "caveat": CAVEAT_SHORT,
    }


def glucose_convert(value: float, unit: str) -> dict:
    """Convert glucose between mmol/L and mg/dL. mmol/L x 18.0156 = mg/dL."""
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"value must be a positive number (got {value!r})")
    u = (unit or "").strip().lower()
    factor = 18.0156
    if u in ("mmol", "mmol/l", "mmoll"):
        out_mgdl = value * factor
        return {
            "metric": "glucose_convert",
            "input": {"value": _round(value, 2), "unit": "mmol/L"},
            "mg_dl": _round(out_mgdl, 0),
            "mmol_l": _round(value, 2),
            "factor": "mmol/L x 18.0156 = mg/dL",
            "caveat": CAVEAT_SHORT,
        }
    elif u in ("mg", "mg/dl", "mgdl"):
        out_mmol = value / factor
        return {
            "metric": "glucose_convert",
            "input": {"value": _round(value, 0), "unit": "mg/dL"},
            "mmol_l": _round(out_mmol, 1),
            "mg_dl": _round(value, 0),
            "factor": "mg/dL / 18.0156 = mmol/L",
            "caveat": CAVEAT_SHORT,
        }
    raise ValueError("unit must be one of: mmol (mmol/L) or mg (mg/dL)")


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------

def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    # Human-readable: print key fields then the caveat last.
    caveat = result.get("caveat")
    note = result.get("note")
    for k, v in result.items():
        if k in ("caveat", "note"):
            continue
        print(f"{k}: {v}")
    if note:
        print(f"note: {note}")
    if caveat:
        print(f"\n⚠️  {caveat}")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _approx(a, b, tol=0.5) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def self_test() -> int:
    """Run built-in examples for every function; print a clean pass/fail report."""
    checks = []  # (name, ok, detail)

    # 1. BMI: 70 kg / 1.75 m = 22.86 -> normal
    r = bmi(70, 1.75)
    checks.append(("bmi 70kg/1.75m=22.9 normal",
                   _approx(r["bmi"], 22.9, 0.1) and "normal" in r["classification"],
                   r["bmi"]))

    # 2. BMI obesity I: 100 kg / 1.75 m = 32.65
    r = bmi(100, 1.75)
    checks.append(("bmi 100kg/1.75m=32.7 obesity I",
                   _approx(r["bmi"], 32.7, 0.1) and "class I" in r["classification"],
                   r["bmi"]))

    # 3. bmi_for_age: SAFE FALLBACK — never fabricates a percentile, defers to the
    #    official WHO chart, and reports the raw BMI (20 / 1.4^2 ≈ 10.2). z stays None.
    r = bmi_for_age(weight_kg=20, height_m=1.40, age_years=10, sex="male")
    checks.append(("bmi_for_age returns raw BMI + None z (no fabrication)",
                   r["z_score"] is None and r["percentile"] is None
                   and r["classification"] == "see_official_chart"
                   and _approx(r["bmi"], 10.2, 0.1),
                   (r["bmi"], r["z_score"])))

    # 4. LMS FORMULA validated against WHO's OWN published worked examples
    #    (boy 9y BMI19 -> z 1.47; 11y BMI30 -> 3.24; 16y BMI14 -> -3.96).
    #    This checks the math, using only WHO-quoted constants — no embedded table.
    lms_ok = True
    lms_detail = []
    for age_y, _sex, bmi_v, L, M, S, z_exp in _WHO_LMS_WORKED_EXAMPLES:
        z_got = _lms_zscore(bmi_v, L, M, S)
        lms_detail.append(f"{age_y}y:{round(z_got, 2)}/{z_exp}")
        if not _approx(z_got, z_exp, 0.01):
            lms_ok = False
    checks.append(("WHO LMS formula reproduces 3 published worked z-scores",
                   lms_ok, " ".join(lms_detail)))

    # 5. BMR Mifflin male: 80kg,180cm,30y = 10*80+6.25*180-5*30+5 = 1780
    r = bmr(80, 180, 30, "male")
    checks.append(("bmr male 80/180/30 = 1780",
                   _approx(r["bmr_kcal_day"], 1780, 1), r["bmr_kcal_day"]))

    # 6. BMR Mifflin female: 60kg,165cm,30y = 600+1031.25-150-161 = 1320.25
    r = bmr(60, 165, 30, "female")
    checks.append(("bmr female 60/165/30 = 1320",
                   _approx(r["bmr_kcal_day"], 1320, 1), r["bmr_kcal_day"]))

    # 7. TDEE moderate: 1780 * 1.55 = 2759
    r = tdee(80, 180, 30, "male", "moderate")
    checks.append(("tdee male moderate = 1780*1.55=2759",
                   _approx(r["tdee_kcal_day"], 2759, 1), r["tdee_kcal_day"]))

    # 8. protein general: 0.8*70 = 56
    r = protein_target(70, "general")
    checks.append(("protein general 70kg = 56 g/d",
                   _approx(r["grams_per_day"][0], 56, 0.5), r["grams_per_day"]))

    # 9. protein older_adult: 1.0-1.2 * 70 = 70-84
    r = protein_target(70, "older_adult")
    checks.append(("protein older_adult 70kg = 70-84 g/d",
                   _approx(r["grams_per_day"][0], 70, 0.5)
                   and _approx(r["grams_per_day"][1], 84, 0.5),
                   r["grams_per_day"]))

    # 10. waist/height: 90/180 = 0.5 -> elevated
    r = waist_to_height_ratio(90, 180)
    checks.append(("wthr 90/180=0.5 elevated",
                   _approx(r["ratio"], 0.5, 0.001) and "increased" in r["classification"],
                   r["ratio"]))

    # 11. eGFR female Scr 0.9, age 55 -> ~76 (published CKD-EPI 2021 reference value)
    r = egfr(0.9, 55, "female")
    checks.append(("egfr female Scr0.9 age55 ~76",
                   _approx(r["egfr_ml_min_1_73m2"], 76, 2),
                   r["egfr_ml_min_1_73m2"]))

    # 12. eGFR male Scr 1.0, age 40 -> ~99 (published CKD-EPI 2021 reference value)
    r = egfr(1.0, 40, "male")
    checks.append(("egfr male Scr1.0 age40 ~99",
                   _approx(r["egfr_ml_min_1_73m2"], 99, 2),
                   r["egfr_ml_min_1_73m2"]))

    # 13. A1c 7.0 -> eAG 154 mg/dL
    r = a1c_to_eag(7.0)
    checks.append(("a1c 7.0 -> eAG 154 mg/dL",
                   _approx(r["eag_mg_dl"], 154, 1), r["eag_mg_dl"]))

    # 14. eAG 154 -> A1c ~7.0
    r = eag_to_a1c(154)
    checks.append(("eAG 154 -> a1c ~7.0",
                   _approx(r["a1c_percent"], 7.0, 0.05), r["a1c_percent"]))

    # 15. glucose 5.5 mmol/L -> ~99 mg/dL
    r = glucose_convert(5.5, "mmol")
    checks.append(("glucose 5.5 mmol/L -> 99 mg/dL",
                   _approx(r["mg_dl"], 99, 1), r["mg_dl"]))

    # 16. glucose 180 mg/dL -> ~10.0 mmol/L
    r = glucose_convert(180, "mg")
    checks.append(("glucose 180 mg/dL -> 10.0 mmol/L",
                   _approx(r["mmol_l"], 10.0, 0.1), r["mmol_l"]))

    # 17. defensive: divide-by-zero height
    try:
        bmi(70, 0)
        ok17 = False
    except ValueError:
        ok17 = True
    checks.append(("bmi(70,0) raises ValueError", ok17, "raised"))

    # 18. defensive: bad sex
    try:
        bmr(80, 180, 30, "x")
        ok18 = False
    except ValueError:
        ok18 = True
    checks.append(("bmr bad sex raises ValueError", ok18, "raised"))

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print("=" * 64)
    print("health_calc.py self-test")
    print("=" * 64)
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name}  -> {detail}")
    print("-" * 64)
    print(f"{passed}/{total} checks passed")
    print("\n" + CAVEAT_SHORT)
    return 0 if passed == total else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="health_calc.py",
        description="Deterministic EDUCATIONAL health calculators (not a diagnosis).",
        epilog=CAVEAT_SHORT,
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--self-test", action="store_true",
                   help="run built-in examples for every function (smoke test)")

    # Parent so --json is accepted AFTER the subcommand too (e.g. `egfr ... --json`),
    # not only before it. store_true defaults False, so the global default is kept
    # unless the subcommand-level flag is given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="json_sub",
                        help="emit JSON instead of text")

    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("bmi", parents=[common],
                        help="adult BMI + WHO classification")
    sp.add_argument("--weight-kg", type=float, required=True)
    sp.add_argument("--height-m", type=float, required=True)

    sp = sub.add_parser("bmi-for-age", parents=[common],
                        help="child/teen BMI-for-age (raw BMI; defers percentile to WHO chart)")
    sp.add_argument("--weight-kg", type=float, required=True)
    sp.add_argument("--height-m", type=float, required=True)
    sp.add_argument("--age-years", type=float, required=True)
    sp.add_argument("--sex", required=True, choices=["male", "female", "m", "f"])

    sp = sub.add_parser("bmr", parents=[common],
                        help="basal metabolic rate (Mifflin-St Jeor)")
    sp.add_argument("--weight-kg", type=float, required=True)
    sp.add_argument("--height-cm", type=float, required=True)
    sp.add_argument("--age-years", type=float, required=True)
    sp.add_argument("--sex", required=True, choices=["male", "female", "m", "f"])

    sp = sub.add_parser("tdee", parents=[common],
                        help="total daily energy expenditure")
    sp.add_argument("--weight-kg", type=float, required=True)
    sp.add_argument("--height-cm", type=float, required=True)
    sp.add_argument("--age-years", type=float, required=True)
    sp.add_argument("--sex", required=True, choices=["male", "female", "m", "f"])
    sp.add_argument("--activity", required=True,
                    choices=sorted(_ACTIVITY_FACTORS))

    sp = sub.add_parser("protein", parents=[common],
                        help="daily protein target by population")
    sp.add_argument("--weight-kg", type=float, required=True)
    sp.add_argument("--population", required=True,
                    choices=sorted(_PROTEIN_TARGETS))

    sp = sub.add_parser("wthr", parents=[common], help="waist-to-height ratio")
    sp.add_argument("--waist-cm", type=float, required=True)
    sp.add_argument("--height-cm", type=float, required=True)

    sp = sub.add_parser("egfr", parents=[common],
                        help="eGFR CKD-EPI creatinine 2021 (race-free)")
    sp.add_argument("--creatinine-mg-dl", type=float, required=True)
    sp.add_argument("--age-years", type=float, required=True)
    sp.add_argument("--sex", required=True, choices=["male", "female", "m", "f"])

    sp = sub.add_parser("a1c-to-eag", parents=[common],
                        help="HbA1c -> estimated average glucose")
    sp.add_argument("--a1c-percent", type=float, required=True)

    sp = sub.add_parser("eag-to-a1c", parents=[common],
                        help="estimated average glucose -> HbA1c")
    sp.add_argument("--eag-mg-dl", type=float, required=True)

    sp = sub.add_parser("glucose", parents=[common],
                        help="convert glucose mmol/L <-> mg/dL")
    sp.add_argument("--value", type=float, required=True)
    sp.add_argument("--unit", required=True, choices=["mmol", "mg"])

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.cmd:
        parser.print_help()
        return 0

    try:
        if args.cmd == "bmi":
            result = bmi(args.weight_kg, args.height_m)
        elif args.cmd == "bmi-for-age":
            result = bmi_for_age(args.weight_kg, args.height_m,
                                 args.age_years, args.sex)
        elif args.cmd == "bmr":
            result = bmr(args.weight_kg, args.height_cm, args.age_years, args.sex)
        elif args.cmd == "tdee":
            result = tdee(args.weight_kg, args.height_cm, args.age_years,
                          args.sex, args.activity)
        elif args.cmd == "protein":
            result = protein_target(args.weight_kg, args.population)
        elif args.cmd == "wthr":
            result = waist_to_height_ratio(args.waist_cm, args.height_cm)
        elif args.cmd == "egfr":
            result = egfr(args.creatinine_mg_dl, args.age_years, args.sex)
        elif args.cmd == "a1c-to-eag":
            result = a1c_to_eag(args.a1c_percent)
        elif args.cmd == "eag-to-a1c":
            result = eag_to_a1c(args.eag_mg_dl)
        elif args.cmd == "glucose":
            result = glucose_convert(args.value, args.unit)
        else:  # pragma: no cover
            parser.print_help()
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # --json is accepted before OR after the subcommand; honor either position.
    as_json = bool(getattr(args, "json", False) or getattr(args, "json_sub", False))
    _emit(result, as_json=as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
