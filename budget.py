"""Расчёт месячного плана.

Чистые функции: на вход — обычные словари/числа, никакой БД и телеграма.
Так план можно покрыть тестами без запуска бота.
"""
import calendar
from datetime import datetime

# Сколько месяцев в периоде (если в записи не задано поле months)
PERIOD_MONTHS = {
    "monthly": 1.0,
    "2months": 2.0,
    "quarterly": 3.0,
    "halfyear": 6.0,
    "yearly": 12.0,
}

AUTO = "auto"          # план категории = доля свободного остатка
PERIODIC = "periodic"  # план категории = её периодические платежи
FIXED = "fixed"        # план задан числом
MIXED = "mixed"        # и платежи, и фикс-план: план = сумма обоих
NONE = "none"          # плана нет (0)


def months_of(sub: dict) -> float:
    """Период платежа в месяцах: сначала поле months, потом period, иначе 1."""
    raw = sub.get("months")
    try:
        m = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        m = 0.0
    if m > 0:
        return m
    return PERIOD_MONTHS.get(str(sub.get("period") or "").lower(), 1.0)


def monthly_amount(sub: dict) -> float:
    """Сколько платёж стоит в месяц."""
    try:
        amount = abs(float(sub.get("amount") or 0))
    except (TypeError, ValueError):
        amount = 0.0
    m = months_of(sub)
    return amount / m if m > 0 else amount


def period_label(months: float) -> str:
    if abs(months - 1) < 1e-9:
        return "в месяц"
    if abs(months - 12) < 1e-9:
        return "в год"
    return f"каждые {months:g} мес."


def month_bounds(now: datetime | None = None) -> tuple[str, str, int]:
    """(первое число месяца, сегодня, сколько дней до конца месяца включительно)."""
    now = now or datetime.now()
    first = now.replace(day=1)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = days_in_month - now.day + 1
    return first.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"), days_left


def build_plan(base: float, categories: list[dict], subs: list[dict],
               spent_by_cat: dict[str, float], warn_threshold: float = 7.0,
               now: datetime | None = None) -> dict:
    """Собирает месячный план.

    base          — деньги месяца: доход этого месяца, иначе плановый доход, иначе баланс
    categories    — [{'name': 'еда', 'monthly_limit': 140.0|None}, ...]
    subs          — периодические платежи: [{'name','amount','months'/'period','category_name'}, ...]
    spent_by_cat  — {'еда': 42.5, ...} траты с начала месяца
    """
    _, _, days_left = month_bounds(now)

    periodic_by_cat: dict[str, float] = {}
    periodic_total = 0.0
    for s in subs:
        per_month = monthly_amount(s)
        periodic_total += per_month
        cat = s.get("category_name") or "подписки"
        periodic_by_cat[cat] = periodic_by_cat.get(cat, 0.0) + per_month

    items = []
    fixed_total = 0.0
    auto_names: list[str] = []

    for c in categories:
        name = c["name"]
        periodic = periodic_by_cat.get(name, 0.0)
        raw_limit = c.get("monthly_limit")
        fixed = None
        if raw_limit is not None:
            try:
                fixed = float(raw_limit)
            except (TypeError, ValueError):
                fixed = None
        if fixed is None and periodic <= 0:
            auto_names.append(name)
        fixed_value = fixed or 0.0
        if fixed_value:
            fixed_total += fixed_value
        items.append({
            "name": name,
            "periodic": periodic,
            "fixed": fixed_value,
            "auto": fixed is None and periodic <= 0,
            "spent": float(spent_by_cat.get(name, 0.0)),
            "plan": 0.0,
            "left": 0.0,
            "source": NONE,
        })

    # Платежи, у которых категория не совпадает ни с одной категорией пользователя,
    # тоже должны быть видны в плане — иначе деньги «исчезают» из распределения.
    known = {it["name"] for it in items}

    free_after_periodic = base - periodic_total
    auto_share = (free_after_periodic - fixed_total) / len(auto_names) if auto_names else 0.0

    planned_total = 0.0
    for name, per_month in periodic_by_cat.items():
        if name in known:
            continue
        items.append({
            "name": name,
            "periodic": per_month,
            "fixed": 0.0,
            "auto": False,
            "spent": float(spent_by_cat.get(name, 0.0)),
            "plan": 0.0,
            "left": 0.0,
            "source": NONE,
        })

    for it in items:
        if it["auto"]:
            it["plan"] = max(auto_share, 0.0)
            it["source"] = AUTO
        else:
            it["plan"] = it["periodic"] + it["fixed"]
            if it["periodic"] > 0 and it["fixed"] > 0:
                it["source"] = MIXED
            elif it["periodic"] > 0:
                it["source"] = PERIODIC
            elif it["fixed"] > 0:
                it["source"] = FIXED
            else:
                it["source"] = NONE
        it["left"] = it["plan"] - it["spent"]
        it["per_day"] = it["left"] / days_left if days_left > 0 else it["left"]
        planned_total += it["plan"]

    spent_total = sum(it["spent"] for it in items)
    unallocated = base - planned_total

    warnings = [it for it in items if 0 < it["left"] <= warn_threshold]
    over = [it for it in items if it["spent"] > it["plan"] and (it["plan"] > 0 or it["spent"] > 0)]

    return {
        "base": base,
        "periodic_total": periodic_total,
        "periodic_by_cat": periodic_by_cat,
        "fixed_total": fixed_total,
        "auto_share": auto_share if auto_names else 0.0,
        "auto_names": auto_names,
        "planned_total": planned_total,
        "unallocated": unallocated,
        "spent_total": spent_total,
        "days_left": days_left,
        "warn_threshold": warn_threshold,
        "items": items,
        "warnings": warnings,
        "over": over,
        "fits": base >= planned_total - 1e-9,
    }


def warning_lines(plan: dict, only_category: str | None = None) -> list[str]:
    """Строки предупреждений: остаток близко к нулю или перерасход."""
    lines = []
    for it in plan["items"]:
        if only_category and it["name"] != only_category:
            continue
        if it["left"] < 0 and (it["plan"] > 0 or it["spent"] > 0):
            lines.append(
                f"🔴 «{it['name']}» в перерасходе: потрачено {it['spent']:.2f}€ при плане {it['plan']:.2f}€ "
                f"(минус {abs(it['left']):.2f}€)"
            )
        elif 0 < it["left"] <= plan.get("warn_threshold", 7.0) and it["plan"] > 0:
            lines.append(
                f"⚠️ «{it['name']}» заканчивается: осталось {it['left']:.2f}€ "
                f"до конца месяца ({it['per_day']:.2f}€/день)"
            )
    return lines
