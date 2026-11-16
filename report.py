#!/usr/bin/env python3
"""Build an as-of work and contract payment report from CSV files."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: str, *, field: str) -> Decimal:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid amount in {field}: {value!r}") from exc
    if not amount.is_finite() or amount < ZERO:
        raise ValueError(f"Amount in {field} must be a finite non-negative number")
    return amount.quantize(CENT)


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def as_date(value: str, *, field: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date in {field}: {value!r}; expected YYYY-MM-DD") from exc


def cents(value: Decimal) -> int:
    return int((value / CENT).to_integral_exact())


def from_cents(value: int) -> Decimal:
    return (Decimal(value) * CENT).quantize(CENT)


def allocate_pro_rata(total: Decimal, capacities: dict[str, Decimal]) -> dict[str, Decimal]:
    """Allocate cents proportionally, capped by each work's remaining price."""
    remaining = min(cents(total), sum(cents(v) for v in capacities.values() if v > ZERO))
    result = {key: 0 for key in capacities}
    active = {key: cents(value) for key, value in capacities.items() if value > ZERO}

    while remaining > 0 and active:
        pool = sum(active.values())
        if remaining >= pool:
            for key, capacity in active.items():
                result[key] += capacity
            remaining -= pool
            break

        shares: dict[str, int] = {}
        remainders: dict[str, int] = {}
        for key, capacity in active.items():
            numerator = remaining * capacity
            shares[key], remainders[key] = divmod(numerator, pool)
        assigned = sum(shares.values())
        cents_left = remaining - assigned
        for key in sorted(active, key=lambda item: (-remainders[item], item))[:cents_left]:
            shares[key] += 1

        spent = 0
        for key, share in shares.items():
            accepted = min(share, active[key])
            result[key] += accepted
            spent += accepted
        remaining -= spent
        active = {key: capacity - shares[key] for key, capacity in active.items() if capacity - shares[key] > 0}

    return {key: from_cents(value) for key, value in result.items()}


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    source_status: str
    completed_at: date | None
    cancelled_at: date | None
    status_as_of: str
    obligation: Decimal
    paid_direct: Decimal = ZERO
    paid_allocated: Decimal = ZERO
    queued_direct: Decimal = ZERO


def build_report(data_dir: Path, as_of: date) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    contracts_rows = read_csv(data_dir / "contracts.csv")
    works_rows = read_csv(data_dir / "works.csv")
    payments_rows = read_csv(data_dir / "payments.csv")

    contracts: dict[str, dict[str, object]] = {}
    for row in contracts_rows:
        contract_id = row["contract_id"].strip()
        if not contract_id or contract_id in contracts:
            raise ValueError(f"Missing or duplicate contract_id: {contract_id!r}")
        contracts[contract_id] = {
            "contractor": row["contractor"].strip(),
            "amount": money(row["amount"], field=f"contracts.csv:{contract_id}:amount"),
        }

    works: dict[str, Work] = {}
    works_by_contract: dict[str, list[Work]] = {key: [] for key in contracts}
    for row in works_rows:
        work_id = row["work_id"].strip()
        contract_id = row["contract_id"].strip()
        if not work_id or work_id in works:
            raise ValueError(f"Missing or duplicate work_id: {work_id!r}")
        if contract_id not in contracts:
            raise ValueError(f"Work {work_id} references unknown contract {contract_id}")
        status = row["status"].strip()
        completed_at = as_date(row["completed_at"], field=f"works.csv:{work_id}:completed_at")
        cancelled_at = as_date(row["cancelled_at"], field=f"works.csv:{work_id}:cancelled_at")
        if status not in {"planned", "done", "cancelled"}:
            raise ValueError(f"Unknown work status for {work_id}: {status!r}")
        if status == "done" and not completed_at:
            raise ValueError(f"Work {work_id} is done but completed_at is empty")
        if status == "cancelled" and not cancelled_at:
            raise ValueError(f"Work {work_id} is cancelled but cancelled_at is empty")
        if completed_at and cancelled_at:
            raise ValueError(f"Work {work_id} has both completed_at and cancelled_at")

        if cancelled_at and cancelled_at <= as_of:
            status_as_of, obligation = "cancelled", ZERO
        elif completed_at and completed_at <= as_of:
            status_as_of, obligation = "done", money(row["price"], field=f"works.csv:{work_id}:price")
        else:
            status_as_of, obligation = "planned", money(row["price"], field=f"works.csv:{work_id}:price")

        work = Work(
            work_id=work_id,
            contract_id=contract_id,
            title=row["title"].strip(),
            price=money(row["price"], field=f"works.csv:{work_id}:price"),
            source_status=status,
            completed_at=completed_at,
            cancelled_at=cancelled_at,
            status_as_of=status_as_of,
            obligation=obligation,
        )
        works[work_id] = work
        works_by_contract[contract_id].append(work)

    contract_cash_paid = {key: ZERO for key in contracts}
    contract_queued = {key: ZERO for key in contracts}
    unallocated_paid = {key: ZERO for key in contracts}
    unallocated_queued = {key: ZERO for key in contracts}

    for row in payments_rows:
        payment_id = row["payment_id"].strip()
        contract_id = row["contract_id"].strip()
        work_id = row["work_id"].strip()
        status = row["status"].strip()
        amount = money(row["amount"], field=f"payments.csv:{payment_id}:amount")
        created_at = as_date(row["created_at"], field=f"payments.csv:{payment_id}:created_at")
        paid_at = as_date(row["paid_at"], field=f"payments.csv:{payment_id}:paid_at")
        refunded_at = as_date(row["refunded_at"], field=f"payments.csv:{payment_id}:refunded_at")

        if contract_id not in contracts:
            raise ValueError(f"Payment {payment_id} references unknown contract {contract_id}")
        if work_id and (work_id not in works or works[work_id].contract_id != contract_id):
            raise ValueError(f"Payment {payment_id} references unknown or mismatched work {work_id}")
        if status not in {"paid", "queued", "refunded"}:
            raise ValueError(f"Unknown payment status for {payment_id}: {status!r}")

        net_paid = ZERO
        if paid_at and paid_at <= as_of:
            net_paid = amount
            if refunded_at and refunded_at <= as_of:
                net_paid -= amount
        if net_paid < ZERO:
            net_paid = ZERO
        if net_paid:
            contract_cash_paid[contract_id] += net_paid
            if work_id:
                works[work_id].paid_direct += net_paid
            else:
                unallocated_paid[contract_id] += net_paid

        if status == "queued" and created_at and created_at <= as_of:
            contract_queued[contract_id] += amount
            if work_id:
                works[work_id].queued_direct += amount
            else:
                unallocated_queued[contract_id] += amount

    unallocated_allocations: dict[str, Decimal] = {key: ZERO for key in contracts}
    for contract_id, contract_works in works_by_contract.items():
        active = [work for work in contract_works if work.obligation > ZERO]
        capacities = {
            work.work_id: max(work.obligation - work.paid_direct, ZERO)
            for work in active
        }
        allocations = allocate_pro_rata(unallocated_paid[contract_id], capacities)
        for work in active:
            work.paid_allocated += allocations[work.work_id]
            unallocated_allocations[contract_id] += allocations[work.work_id]

    work_report: list[dict[str, str]] = []
    for work in sorted(works.values(), key=lambda item: (item.contract_id, item.work_id)):
        paid = work.paid_direct + work.paid_allocated
        outstanding = max(work.obligation - paid, ZERO)
        overpaid = max(paid - work.obligation, ZERO)
        work_report.append({
            "as_of": as_of.isoformat(),
            "contract_id": work.contract_id,
            "work_id": work.work_id,
            "title": work.title,
            "status_as_of": work.status_as_of,
            "price": f"{work.price:.2f}",
            "payable_as_of": f"{work.obligation:.2f}",
            "paid_direct": f"{work.paid_direct:.2f}",
            "paid_from_contract_prepayments": f"{work.paid_allocated:.2f}",
            "paid_total": f"{paid:.2f}",
            "remaining_to_pay": f"{outstanding:.2f}",
            "overpaid": f"{overpaid:.2f}",
            "queued_direct": f"{work.queued_direct:.2f}",
        })

    contract_report: list[dict[str, str]] = []
    for contract_id in sorted(contracts):
        contract_works = works_by_contract[contract_id]
        signed_amount = contracts[contract_id]["amount"]
        active_total = sum((work.obligation for work in contract_works), ZERO)
        cancelled_total = sum((work.price for work in contract_works if work.status_as_of == "cancelled"), ZERO)
        allocated_cash = sum((work.paid_direct + work.paid_allocated for work in contract_works), ZERO)
        remaining = sum((max(work.obligation - work.paid_direct - work.paid_allocated, ZERO) for work in contract_works), ZERO)
        unassigned_excess = max(unallocated_paid[contract_id] - unallocated_allocations[contract_id], ZERO)
        contract_report.append({
            "as_of": as_of.isoformat(),
            "contract_id": contract_id,
            "contractor": str(contracts[contract_id]["contractor"]),
            "signed_amount": f"{signed_amount:.2f}",
            "active_work_payable": f"{active_total:.2f}",
            "cancelled_work_value": f"{cancelled_total:.2f}",
            "paid_total": f"{contract_cash_paid[contract_id]:.2f}",
            "paid_assigned_to_works": f"{allocated_cash:.2f}",
            "unassigned_or_excess_paid": f"{unassigned_excess:.2f}",
            "remaining_to_pay": f"{remaining:.2f}",
            "queued_not_paid": f"{contract_queued[contract_id]:.2f}",
            "queued_unallocated": f"{unallocated_queued[contract_id]:.2f}",
        })

    return work_report, contract_report


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError(f"No rows to write to {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Directory containing contracts.csv, works.csv, payments.csv")
    parser.add_argument("--as-of", required=True, help="Cut-off date in YYYY-MM-DD")
    parser.add_argument("--out", type=Path, required=True, help="Directory for work_report.csv and contract_report.csv")
    args = parser.parse_args()

    try:
        cutoff = date.fromisoformat(args.as_of)
        work_report, contract_report = build_report(args.data, cutoff)
        args.out.mkdir(parents=True, exist_ok=True)
        write_csv(args.out / "work_report.csv", work_report)
        write_csv(args.out / "contract_report.csv", contract_report)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    print(f"Отчёт готов: {args.out / 'work_report.csv'}")
    print(f"Итоги по договорам: {args.out / 'contract_report.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
