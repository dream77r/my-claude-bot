#!/usr/bin/env python3
"""Unit-economics calculator — pure stdlib, no third-party deps.

Запуск из памяти агента (cwd = agents/me/memory/):

    python3 ../skills/unit-economics/scripts/calc.py --self-test
    python3 ../skills/unit-economics/scripts/calc.py saas --arpu 1200 --gross-margin 0.80 \
            --churn 0.03 --sm-spend 900000 --new-customers 300
    python3 ../skills/unit-economics/scripts/calc.py store --aov 350 --tx-per-day 150 \
            --days 30 --cogs-pct 0.30 --labor 470000 --rent 280000 --other-fixed 180000 \
            --investment 5500000

Принцип: считаем здесь, в коде. LLM ошибается в длинных арифметических цепочках —
скрипт не ошибается. GIGO: качество вывода = качество входных допущений.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict


# --------------------------------------------------------------------------- #
# Защитная арифметика
# --------------------------------------------------------------------------- #
def _safe_div(numerator: float, denominator: float, default: float = float("inf")) -> float:
    """Делит, но не падает на нуле. По умолчанию возвращает +inf (метрика «не определена / бесконечна»)."""
    if denominator == 0:
        return default
    return numerator / denominator


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _money(x: float, cur: str = "₽") -> str:
    if x == float("inf"):
        return "∞"
    return f"{x:,.0f} {cur}".replace(",", " ")


# --------------------------------------------------------------------------- #
# Базовые метрики
# --------------------------------------------------------------------------- #
def cac(sm_spend: float, new_customers: float) -> float:
    """Customer Acquisition Cost = совокупные затраты Sales & Marketing / число новых клиентов.

    blended  → в sm_spend весь S&M (вкл. органику-как-косвенные расходы) и все новые клиенты.
    paid     → в sm_spend только платный трафик, в new_customers — только привлечённые платно.
    """
    return _safe_div(sm_spend, new_customers)


def ltv_from_churn(arpu: float, gross_margin: float, churn: float) -> float:
    """LTV (gross-margin-adjusted), классическая SaaS-формула.

        Срок жизни клиента (периодов) = 1 / churn
        LTV = ARPU * gross_margin% * (1 / churn)

    ARPU — средняя выручка с клиента ЗА ПЕРИОД (мес/год — churn должен быть в той же базе).
    """
    lifetime = _safe_div(1.0, churn)
    return arpu * gross_margin * lifetime


def ltv_contribution(contribution_per_period: float, churn: float) -> float:
    """LTV на базе contribution margin (а не валовой) — точнее, когда есть переменные не-COGS расходы.

        LTV = contribution_per_period / churn   (= contribution * 1/churn)
    """
    return _safe_div(contribution_per_period, churn)


def ltv_cac_ratio(ltv: float, cac_value: float) -> float:
    """LTV:CAC. Целевой ориентир >= 3.0. < 1.0 — каждый клиент убыточен."""
    return _safe_div(ltv, cac_value)


def cac_payback_months(cac_value: float, arpu: float, gross_margin: float) -> float:
    """CAC payback (мес) = CAC / (ARPU_месячный * gross_margin%).

    Сколько месяцев валовой прибыли с клиента нужно, чтобы отбить стоимость привлечения.
    ARPU здесь — МЕСЯЧНЫЙ. Если данные годовые, делим ARPU на 12 заранее.
    """
    monthly_gross_profit = arpu * gross_margin
    return _safe_div(cac_value, monthly_gross_profit)


def contribution_margin(price: float, variable_cost: float) -> dict:
    """Contribution margin на единицу = цена − переменные затраты.

    Возвращает абсолют и процент. Это «вклад» единицы в покрытие постоянных затрат.
    """
    cm = price - variable_cost
    return {
        "contribution_per_unit": cm,
        "contribution_margin_pct": _safe_div(cm, price, default=0.0),
    }


def cohort_retention(cohort_sizes: list[int]) -> dict:
    """Когортное удержание из ряда «активны в периоде 0,1,2,...».

    retention[t] = active[t] / active[0]
    churn[t]     = 1 − active[t]/active[t-1]   (период-к-периоду)
    Возвращает помесячные retention/churn и среднемесячный churn.
    """
    if not cohort_sizes or cohort_sizes[0] == 0:
        raise ValueError("cohort_sizes должен начинаться с ненулевой базы (период 0)")
    base = cohort_sizes[0]
    retention = [round(_safe_div(n, base, 0.0), 4) for n in cohort_sizes]
    period_churn = []
    for prev, cur in zip(cohort_sizes, cohort_sizes[1:]):
        period_churn.append(round(1.0 - _safe_div(cur, prev, 0.0), 4))
    avg_churn = round(statistics.fmean(period_churn), 4) if period_churn else 0.0
    return {
        "retention_curve": retention,
        "period_churn": period_churn,
        "avg_period_churn": avg_churn,
        "implied_lifetime_periods": round(_safe_div(1.0, avg_churn), 2) if avg_churn else float("inf"),
    }


# --------------------------------------------------------------------------- #
# Юнит-экономика ТОЧКИ (кофейня / ретейл F&B)
# --------------------------------------------------------------------------- #
@dataclass
class StoreEconomics:
    aov: float
    tx_per_day: float
    days: float
    revenue_month: float
    cogs: float
    cogs_pct: float
    labor: float
    labor_pct: float
    rent: float
    other_fixed: float
    fixed_total: float
    contribution: float           # выручка − COGS (− переменный труд, если выделен)
    contribution_pct: float
    operating_profit: float       # contribution − постоянные (труд+аренда+прочее)
    operating_margin: float
    breakeven_revenue_month: float
    breakeven_tx_per_day: float
    payback_months: float | None = None
    roi_annual: float | None = None


def store_unit_economics(
    aov: float,
    tx_per_day: float,
    cogs_pct: float,
    labor: float,
    rent: float,
    other_fixed: float = 0.0,
    days: float = 30.0,
    variable_labor_pct: float = 0.0,
    investment: float | None = None,
) -> StoreEconomics:
    """Полная юнит-экономика одной точки за месяц.

    aov               — средний чек (average order value), ₽.
    tx_per_day        — транзакций (чеков) в день.
    cogs_pct          — себестоимость как доля выручки (food cost), 0..1.
    labor             — ФОТ за месяц (постоянная часть), ₽.
    rent              — аренда за месяц, ₽.
    other_fixed       — прочие постоянные (коммуналка, маркетинг, эквайринг-фикс…), ₽.
    days              — рабочих дней в месяце.
    variable_labor_pct— переменная часть труда как доля выручки (если есть), 0..1.
                        Идёт в contribution, НЕ в постоянные.
    investment        — капзатраты на открытие точки, ₽ (для payback/ROI). Опционально.

    Contribution здесь = выручка − COGS − переменный труд.
    Постоянные = labor (постоянный ФОТ) + rent + other_fixed.
    Break-even по выручке = постоянные / contribution_margin%.
    """
    revenue_month = aov * tx_per_day * days
    cogs = revenue_month * cogs_pct
    variable_labor = revenue_month * variable_labor_pct
    contribution = revenue_month - cogs - variable_labor
    contribution_pct = _safe_div(contribution, revenue_month, 0.0)

    fixed_total = labor + rent + other_fixed
    operating_profit = contribution - fixed_total
    operating_margin = _safe_div(operating_profit, revenue_month, 0.0)

    # Break-even: какая выручка обнуляет операционную прибыль при той же contribution%
    breakeven_revenue_month = _safe_div(fixed_total, contribution_pct, float("inf"))
    breakeven_tx_per_day = _safe_div(breakeven_revenue_month, aov * days, float("inf"))

    payback_months = None
    roi_annual = None
    if investment is not None and investment > 0:
        payback_months = _safe_div(investment, operating_profit) if operating_profit > 0 else float("inf")
        roi_annual = _safe_div(operating_profit * 12.0, investment, 0.0)

    return StoreEconomics(
        aov=aov,
        tx_per_day=tx_per_day,
        days=days,
        revenue_month=revenue_month,
        cogs=cogs,
        cogs_pct=cogs_pct,
        labor=labor,
        labor_pct=_safe_div(labor, revenue_month, 0.0),
        rent=rent,
        other_fixed=other_fixed,
        fixed_total=fixed_total,
        contribution=contribution,
        contribution_pct=contribution_pct,
        operating_profit=operating_profit,
        operating_margin=operating_margin,
        breakeven_revenue_month=breakeven_revenue_month,
        breakeven_tx_per_day=breakeven_tx_per_day,
        payback_months=payback_months,
        roi_annual=roi_annual,
    )


# --------------------------------------------------------------------------- #
# SaaS / subscription bundle
# --------------------------------------------------------------------------- #
def saas_economics(
    arpu: float,
    gross_margin: float,
    churn: float,
    sm_spend: float,
    new_customers: float,
    net_new_arr: float | None = None,
) -> dict:
    """Сводка SaaS-экономики + опционально magic number, если задан net_new_arr.

    magic number = net new ARR (квартал, annualized) / S&M прошлого квартала.
    """
    c = cac(sm_spend, new_customers)
    ltv = ltv_from_churn(arpu, gross_margin, churn)
    ratio = ltv_cac_ratio(ltv, c)
    payback = cac_payback_months(c, arpu, gross_margin)
    out = {
        "cac": round(c, 2),
        "ltv": round(ltv, 2),
        "ltv_cac_ratio": round(ratio, 2),
        "cac_payback_months": round(payback, 2),
        "implied_lifetime_months": round(_safe_div(1.0, churn), 1),
    }
    if net_new_arr is not None:
        out["magic_number"] = round(_safe_div(net_new_arr, sm_spend, 0.0), 2)
    return out


def rule_of_40(growth_pct: float, profit_margin_pct: float) -> dict:
    """Rule of 40: growth% + profit_margin% >= 40 = здоровый баланс рост/прибыльность.

    Обе доли — в процентах (напр. 25 для 25%, не 0.25).
    """
    total = growth_pct + profit_margin_pct
    return {"rule_of_40_score": round(total, 1), "passes": total >= 40.0}


# --------------------------------------------------------------------------- #
# Отчёт / интерпретация
# --------------------------------------------------------------------------- #
def interpret_saas(m: dict) -> list[str]:
    notes = []
    r = m["ltv_cac_ratio"]
    if r == float("inf"):
        notes.append("LTV:CAC = ∞ — CAC=0, проверь ввод (бесплатное привлечение редко реально).")
    elif r < 1:
        notes.append(f"LTV:CAC = {r} (<1) — КРАСНО: каждый клиент убыточен, юнит-экономика не сходится.")
    elif r < 3:
        notes.append(f"LTV:CAC = {r} (<3) — ЖЁЛТО: ниже целевого 3:1, маркетинг недоокупается.")
    elif r > 5:
        notes.append(f"LTV:CAC = {r} (>5) — недоинвестируешь в рост; можно агрессивнее лить в attribution.")
    else:
        notes.append(f"LTV:CAC = {r} — здоровый коридор 3–5.")
    p = m["cac_payback_months"]
    if p == float("inf"):
        notes.append("CAC payback = ∞ — валовая прибыль с клиента = 0.")
    elif p > 18:
        notes.append(f"CAC payback = {p} мес (>18) — слишком долго, кассовый разрыв в росте.")
    elif p > 12:
        notes.append(f"CAC payback = {p} мес — выше SaaS-нормы (<12), терпимо для high-ACV.")
    else:
        notes.append(f"CAC payback = {p} мес — в норме (<12).")
    return notes


def interpret_store(s: StoreEconomics) -> list[str]:
    notes = []
    if s.cogs_pct > 0.35:
        notes.append(f"COGS {_pct(s.cogs_pct)} (>35%) — food cost высокий, норма F&B 28–35%.")
    else:
        notes.append(f"COGS {_pct(s.cogs_pct)} — в норме (28–35%).")
    if s.labor_pct > 0.30:
        notes.append(f"Labor {_pct(s.labor_pct)} (>30%) — переукомплектован или низкий трафик.")
    else:
        notes.append(f"Labor {_pct(s.labor_pct)} — в норме (25–30%).")
    if s.operating_profit <= 0:
        notes.append(f"Операционка {_money(s.operating_profit)} — точка УБЫТОЧНА; break-even = {s.breakeven_tx_per_day:.0f} чеков/день (сейчас {s.tx_per_day:.0f}).")
    else:
        notes.append(f"Операционка {_money(s.operating_profit)} ({_pct(s.operating_margin)}); запас прочности: {s.tx_per_day:.0f} vs break-even {s.breakeven_tx_per_day:.0f} чеков/день.")
    if s.payback_months is not None:
        if s.payback_months == float("inf"):
            notes.append("Окупаемость инвестиций: ∞ (точка убыточна) — открывать/держать нельзя без плана разворота.")
        else:
            verdict = "норма 12–24" if s.payback_months <= 24 else "ДОЛГО (>24 мес)"
            notes.append(f"Окупаемость инвестиций ≈ {s.payback_months:.1f} мес ({verdict}); ROIC {_pct(s.roi_annual)}/год.")
    return notes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_store(s: StoreEconomics) -> None:
    print("=== ЮНИТ-ЭКОНОМИКА ТОЧКИ (месяц) ===")
    print(f"  Средний чек (AOV)        {_money(s.aov)}")
    print(f"  Транзакций/день          {s.tx_per_day:.0f}  ×  {s.days:.0f} дней")
    print(f"  Выручка/мес              {_money(s.revenue_month)}")
    print(f"  COGS                     {_money(s.cogs)}  ({_pct(s.cogs_pct)})")
    print(f"  Labor (ФОТ)              {_money(s.labor)}  ({_pct(s.labor_pct)})")
    print(f"  Аренда                   {_money(s.rent)}")
    print(f"  Прочие постоянные        {_money(s.other_fixed)}")
    print(f"  Постоянные всего         {_money(s.fixed_total)}")
    print(f"  Contribution             {_money(s.contribution)}  ({_pct(s.contribution_pct)})")
    print(f"  Операционная прибыль     {_money(s.operating_profit)}  ({_pct(s.operating_margin)})")
    print(f"  Break-even               {_money(s.breakeven_revenue_month)}/мес = {s.breakeven_tx_per_day:.0f} чеков/день")
    if s.payback_months is not None:
        pm = "∞" if s.payback_months == float("inf") else f"{s.payback_months:.1f} мес"
        print(f"  Окупаемость инвестиций   {pm}   ROIC {_pct(s.roi_annual)}/год")
    print()
    for n in interpret_store(s):
        print(f"  • {n}")


def _print_saas(m: dict) -> None:
    print("=== SaaS / SUBSCRIPTION ===")
    print(f"  CAC                      {_money(m['cac'])}")
    print(f"  LTV (gross-adj)          {_money(m['ltv'])}")
    print(f"  LTV:CAC                  {m['ltv_cac_ratio']}")
    _pb = "∞" if m['cac_payback_months'] == float("inf") else m['cac_payback_months']
    print(f"  CAC payback              {_pb} мес")
    print(f"  Срок жизни (impl.)       {m['implied_lifetime_months']} мес")
    if "magic_number" in m:
        print(f"  Magic number             {m['magic_number']}")
    print()
    for n in interpret_saas(m):
        print(f"  • {n}")


def self_test() -> None:
    """Сквозной прогон на реальном кофейном кейсе. Двойная роль: пример + smoke-test."""
    print("################  SELF-TEST: кофейня Lintu, флагман  ################\n")

    s = store_unit_economics(
        aov=350.0,           # средний чек 350 ₽
        tx_per_day=150.0,    # 150 чеков/день
        cogs_pct=0.30,       # food cost 30%
        labor=470000.0,      # ФОТ 470к/мес (постоянный)
        rent=280000.0,       # аренда 280к/мес
        other_fixed=180000.0,# коммуналка+маркетинг+эквайринг 180к
        days=30.0,
        investment=5_500_000.0,  # открытие точки 5.5 млн
    )
    _print_store(s)
    print()

    # SaaS-кусок: гипотетическая подписка BrewOS на 1200 ₽/мес
    m = saas_economics(
        arpu=1200.0, gross_margin=0.80, churn=0.03,
        sm_spend=900000.0, new_customers=300.0,
        net_new_arr=3_600_000.0,
    )
    _print_saas(m)
    print()

    # Когорта
    coh = cohort_retention([1000, 820, 700, 620, 560, 510])
    print("=== КОГОРТА (ежемесячная) ===")
    print(f"  Retention curve  {coh['retention_curve']}")
    print(f"  Avg churn/мес    {_pct(coh['avg_period_churn'])}")
    print(f"  Срок жизни       {coh['implied_lifetime_periods']} мес")
    print()

    r40 = rule_of_40(growth_pct=35.0, profit_margin_pct=10.0)
    print(f"=== Rule of 40 === score={r40['rule_of_40_score']} passes={r40['passes']}")

    # --- ассерты как smoke-test (числа фиксированы) ---
    assert abs(s.revenue_month - 1_575_000) < 1, s.revenue_month
    assert abs(s.cogs - 472_500) < 1, s.cogs
    assert abs(s.contribution - 1_102_500) < 1, s.contribution
    assert abs(s.operating_profit - 172_500) < 1, s.operating_profit
    assert abs(s.breakeven_tx_per_day - 126.53) < 0.05, s.breakeven_tx_per_day
    assert abs(s.payback_months - 31.88) < 0.05, s.payback_months
    assert abs(m["cac"] - 3000) < 0.5, m["cac"]
    assert abs(m["ltv"] - 32000) < 0.5, m["ltv"]
    assert abs(m["ltv_cac_ratio"] - 10.67) < 0.05, m["ltv_cac_ratio"]
    assert abs(m["cac_payback_months"] - 3.12) < 0.05, m["cac_payback_months"]
    assert abs(m["magic_number"] - 4.0) < 0.01, m["magic_number"]
    assert abs(coh["avg_period_churn"] - 0.1253) < 0.01, coh["avg_period_churn"]
    assert r40["rule_of_40_score"] == 45.0 and r40["passes"], r40
    print("\n[OK] self-test passed — все контрольные числа сошлись.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unit-economics calculator (pure stdlib).")
    p.add_argument("--self-test", action="store_true", help="прогнать встроенный кофейный пример end-to-end")
    p.add_argument("--json", action="store_true", help="вывод в JSON вместо текста")
    sub = p.add_subparsers(dest="cmd")

    # --json дублируется в каждый подпарсер: argparse не принимает родительскую опцию
    # ПОСЛЕ имени подкоманды, поэтому `calc.py saas ... --json` без этого падал бы.
    ps = sub.add_parser("store", help="юнит-экономика точки")
    ps.add_argument("--aov", type=float, required=True)
    ps.add_argument("--tx-per-day", type=float, required=True)
    ps.add_argument("--cogs-pct", type=float, required=True, help="доля 0..1")
    ps.add_argument("--labor", type=float, required=True)
    ps.add_argument("--rent", type=float, required=True)
    ps.add_argument("--other-fixed", type=float, default=0.0)
    ps.add_argument("--days", type=float, default=30.0)
    ps.add_argument("--variable-labor-pct", type=float, default=0.0)
    ps.add_argument("--investment", type=float, default=None)
    ps.add_argument("--json", action="store_true", help="вывод в JSON вместо текста")

    pa = sub.add_parser("saas", help="SaaS/subscription метрики")
    pa.add_argument("--arpu", type=float, required=True, help="МЕСЯЧНЫЙ ARPU")
    pa.add_argument("--gross-margin", type=float, required=True, help="доля 0..1")
    pa.add_argument("--churn", type=float, required=True, help="месячный churn, доля 0..1")
    pa.add_argument("--sm-spend", type=float, required=True)
    pa.add_argument("--new-customers", type=float, required=True)
    pa.add_argument("--net-new-arr", type=float, default=None)
    pa.add_argument("--json", action="store_true", help="вывод в JSON вместо текста")

    pc = sub.add_parser("cohort", help="когортное удержание")
    pc.add_argument("--sizes", type=int, nargs="+", required=True, help="активны в периодах 0 1 2 ...")
    pc.add_argument("--json", action="store_true", help="вывод в JSON вместо текста")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        self_test()
        return 0

    if args.cmd == "store":
        s = store_unit_economics(
            aov=args.aov, tx_per_day=args.tx_per_day, cogs_pct=args.cogs_pct,
            labor=args.labor, rent=args.rent, other_fixed=args.other_fixed,
            days=args.days, variable_labor_pct=args.variable_labor_pct,
            investment=args.investment,
        )
        if args.json:
            print(json.dumps(asdict(s), ensure_ascii=False, indent=2))
        else:
            _print_store(s)
        return 0

    if args.cmd == "saas":
        m = saas_economics(
            arpu=args.arpu, gross_margin=args.gross_margin, churn=args.churn,
            sm_spend=args.sm_spend, new_customers=args.new_customers,
            net_new_arr=args.net_new_arr,
        )
        if args.json:
            print(json.dumps(m, ensure_ascii=False, indent=2))
        else:
            _print_saas(m)
        return 0

    if args.cmd == "cohort":
        coh = cohort_retention(args.sizes)
        if args.json:
            print(json.dumps(coh, ensure_ascii=False, indent=2))
        else:
            print(f"Retention: {coh['retention_curve']}")
            print(f"Avg churn/период: {_pct(coh['avg_period_churn'])}")
            print(f"Срок жизни: {coh['implied_lifetime_periods']} периодов")
        return 0

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
