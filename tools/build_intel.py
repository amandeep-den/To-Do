"""Build the Taskly completion-forecast model from the 2025 task dataset.

Development-only helper. Requires openpyxl; the server only reads the
generated ``task_intel.json`` and never needs openpyxl at runtime.

    python tools/build_intel.py "C:\\path\\to\\To-Do List DataSet.xlsx"

The output is a compact, transparent set of averages. The client blends
them per task; no machine-learning runtime is involved.
"""

import json
import os
import sys
from collections import defaultdict

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_XLSX = r"C:\Users\AMANDEEP SINGH\Downloads\To-Do List DataSet.xlsx"
OUT_PATH = os.path.join(ROOT, "task_intel.json")

PRIORITIES = ["Urgent", "High", "Medium", "Low"]
BUCKETS = [
    ("3-4", 3, 4),
    ("5-7", 5, 7),
    ("8-10", 8, 10),
    ("11-14", 11, 14),
    ("15-21", 15, 21),
]
REASONS = [
    "Scope Expansion",
    "Client Feedback Delay",
    "Technical Roadblock",
    "Dependency Block",
    "Resource Reallocation",
]


def bucket_key(days):
    for key, low, high in BUCKETS:
        if low <= days <= high:
            return key
    if days < BUCKETS[0][1]:
        return BUCKETS[0][0]
    return BUCKETS[-1][0]


def mean(values):
    return sum(values) / len(values) if values else 0.0


def summarize(rows):
    n = len(rows)
    if not n:
        return {
            "n": 0,
            "delayed_rate": 0.0,
            "avg_lead": 0.0,
            "avg_dur": 0.0,
            "avg_diff": 0.0,
            "avg_shift": 0.0,
        }
    delayed = [r for r in rows if r["is_delayed"]]
    return {
        "n": n,
        "delayed_rate": round(len(delayed) / n, 4),
        "avg_lead": round(mean([r["allocated_lead_days"] for r in rows]), 3),
        "avg_dur": round(mean([r["actual_duration_days"] for r in rows]), 3),
        "avg_diff": round(mean([r["completion_difference_days"] for r in rows]), 3),
        "avg_shift": round(mean([r["shift_days"] for r in rows]), 3),
    }


def summarize_reason(rows):
    n = len(rows)
    if not n:
        return {
            "n": 0,
            "delayed_rate": 0.0,
            "avg_shift": 0.0,
            "avg_extra_when_delayed": 0.0,
            "avg_actual_when_delayed": 0.0,
            "max_extra": 0.0,
            "avg_lead": 0.0,
            "avg_dur": 0.0,
        }
    delayed = [r for r in rows if r["is_delayed"]]
    return {
        "n": n,
        "delayed_rate": round(len(delayed) / n, 4),
        "avg_shift": round(mean([r["shift_days"] for r in rows]), 3),
        "avg_extra_when_delayed": round(
            mean([r["completion_difference_days"] for r in delayed]), 3
        ),
        "avg_actual_when_delayed": round(
            mean([r["actual_duration_days"] for r in delayed]), 3
        ),
        "max_extra": max((r["completion_difference_days"] for r in delayed), default=0),
        "avg_lead": round(mean([r["allocated_lead_days"] for r in rows]), 3),
        "avg_dur": round(mean([r["actual_duration_days"] for r in rows]), 3),
    }


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    headers = [h for h in rows[0] if h]
    records = []
    for raw in rows[1:]:
        if raw[0] is None:
            continue
        records.append(dict(zip(headers, raw[: len(headers)])))

    for record in records:
        record["allocated_lead_days"] = int(record["allocated_lead_days"] or 0)
        record["actual_duration_days"] = float(record["actual_duration_days"] or 0)
        record["completion_difference_days"] = float(
            record["completion_difference_days"] or 0
        )
        record["shift_days"] = float(record["shift_days"] or 0)
        record["is_delayed"] = int(record["is_delayed"] or 0)

    by_bucket = defaultdict(list)
    by_priority = defaultdict(list)
    by_cell = defaultdict(list)
    by_reason = defaultdict(list)
    by_reason_priority = defaultdict(list)
    by_category = defaultdict(list)

    for record in records:
        key = bucket_key(record["allocated_lead_days"])
        priority = record["Priority"]
        reason = record["Shift Reason"]
        by_bucket[key].append(record)
        by_priority[priority].append(record)
        by_cell[(key, priority)].append(record)
        if reason not in (None, "None", ""):
            by_reason[reason].append(record)
            by_reason_priority[(reason, priority)].append(record)
        by_category[record["Category"]].append(record)

    created = [r["Task Creation Date"] for r in records if r["Task Creation Date"]]
    completed = [r["Date Completed"] for r in records if r["Date Completed"]]

    payload = {
        "version": 1,
        "source": os.path.basename(xlsx),
        "year": 2025,
        "sample_size": len(records),
        "date_range": {
            "start": min(created).date().isoformat(),
            "end": max(completed).date().isoformat(),
        },
        "lead_min": min(r["allocated_lead_days"] for r in records),
        "lead_max": max(r["allocated_lead_days"] for r in records),
        "overall": summarize(records),
        "buckets": [
            dict(key=key, min=low, max=high, **summarize(by_bucket[key]))
            for key, low, high in BUCKETS
        ],
        "priorities": {
            priority: summarize(by_priority[priority]) for priority in PRIORITIES
        },
        "cells": {
            key + "|" + priority: summarize(rows)
            for (key, priority), rows in sorted(by_cell.items())
        },
        "reasons": {
            reason: summarize_reason(by_reason[reason]) for reason in REASONS
        },
        "reason_priority": {
            reason + "|" + priority: summarize(rows)
            for (reason, priority), rows in sorted(by_reason_priority.items())
        },
        "categories": {
            category: summarize(rows)
            for category, rows in sorted(by_category.items())
        },
        "notes": [
            "Probabilities are the share of tasks that were not delayed.",
            "Lead days are calendar days between creation and the original deadline.",
            "Shift days are calendar days the deadline moved.",
            "Extra days are days past the revised deadline when a task was delayed.",
        ],
    }

    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print("wrote", OUT_PATH)
    print("records", len(records))
    print("buckets", json.dumps(payload["buckets"], indent=2))
    print("reasons", json.dumps(payload["reasons"], indent=2))


if __name__ == "__main__":
    main()
