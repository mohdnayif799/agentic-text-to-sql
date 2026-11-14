"""Verify the unseen-schema test's gold answers, then freeze them.

    python scripts/check_college_gold.py            # verify only
    python scripts/check_college_gold.py --freeze   # verify, then write the manifest

No model is called. Four independent checks, any failure stops the freeze:

1. The messy copy holds the same rows as the clean copy (codes mapped back,
   and att_sum recomputed from the attendance log).
2. Each question's gold SQL on the clean upload and on the messy upload
   return the same answer (a department may appear as its code).
3. Each answer is recomputed with pandas straight from the clean CSV files,
   by code that shares nothing with the gold SQL, and must agree.
4. No answer depends on a tie: a top-N cut and a "lowest per department"
   have a strict winner, and no student's attendance is close enough to the
   85% line for rounding (att_pct has two decimals) to move them across it.
   A value exactly ON a threshold is not a tie, because every question says
   "above", "below", "at least" or "or more"; those cases are printed as
   notes so the wording can be checked against them.

The comparison in 2 and 3 uses the frozen grader (evalkit/grader.py), the
one the runs are graded with. The uploads go through the app's real ingest
path, so the table and column names are what a visitor's upload produces.
"""

from __future__ import annotations

import argparse
import gc
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.datasets.store import DatasetStore  # noqa: E402
from evalkit.grader import Gold, grade  # noqa: E402
from evalrun import college  # noqa: E402

PASS_MARK = 50
ATTENDANCE_CUT = 85.0


def frames(variant: str) -> dict[str, pd.DataFrame]:
    return {p.stem: pd.read_csv(p) for p in college.csv_files(variant)}


# ---------------------------------------------------------------------------
# 1. The messy copy is the clean copy
# ---------------------------------------------------------------------------
def check_copies() -> list[str]:
    clean, messy = frames("clean"), frames("messy")
    exams = {"MT": "midterm", "FN": "final"}
    status = {"P": "present", "A": "absent", "L": "late"}
    rebuilt = {
        "students": messy["stu_mst"].assign(
            dept_cd=messy["stu_mst"].dept_cd.map(college.CODES)),
        "courses": messy["crs_mst"].assign(
            dept_cd=messy["crs_mst"].dept_cd.map(college.CODES)),
        "marks": messy["exm_mk"].assign(exm_typ=messy["exm_mk"].exm_typ.map(exams)),
        "attendance": messy["att_log"].assign(
            att_st=messy["att_log"].att_st.map(status)),
    }
    problems = []
    for name, frame in rebuilt.items():
        if frame.values.tolist() != clean[name].values.tolist():
            problems.append(f"messy copy of {name} differs from the clean rows")
    log = clean["attendance"]
    summary = (log.assign(att=log.status != "absent")
               .groupby(["student_id", "course_id"], sort=False)
               .agg(cls_hld=("att", "size"), cls_att=("att", "sum")).reset_index())
    summary["att_pct"] = (100 * summary.cls_att / summary.cls_hld).round(2)
    if summary.values.tolist() != messy["att_sum"].values.tolist():
        problems.append("att_sum does not match the attendance log")
    return problems


# ---------------------------------------------------------------------------
# 3. Independent answers with pandas (from the clean CSV files)
# ---------------------------------------------------------------------------
def pandas_answers() -> dict[str, list[list[Any]]]:
    f = frames("clean")
    st, co, mk, at = f["students"], f["courses"], f["marks"], f["attendance"]
    fin = mk[mk.exam == "final"]
    mid = mk[mk.exam == "midterm"]
    fin_c = fin.merge(co, on="course_id")
    absent = at[at.status == "absent"]
    overall = at.assign(a=at.status.isin(["present", "late"])).groupby(
        "student_id").a.mean() * 100
    out: dict[str, list[list[Any]]] = {}
    out["co01"] = [[int((st.department == "Computer Science").sum())]]
    out["co02"] = [[int((co.credits == 4).sum())]]
    out["co03"] = [[float(fin_c[fin_c.title == "Database Systems"].score.mean())]]
    out["co04"] = [[d, float(v)] for d, v in
                   fin_c.groupby("department").score.mean().items()]
    by_dept = at.merge(st, on="student_id").assign(
        p=lambda d: d.status == "present").groupby("department").p.mean() * 100
    out["co05"] = [[d, float(v)] for d, v in by_dept.items()]
    out["co06"] = [[int((fin.groupby("student_id").score.mean() > 80).sum())]]
    out["co07"] = [[int(absent.session_date.between("2026-03-01", "2026-03-31").sum())]]
    counts = absent.groupby("student_id").size().rename("n").reset_index().merge(
        st, on="student_id").sort_values(["n", "student_id"], ascending=[False, True])
    out["co08"] = [[int(r.student_id), r["name"], int(r.n)]
                   for _, r in counts.head(3).iterrows()]
    out["co09"] = [[int(y), int(n)] for y, n in
                   st.groupby("year_of_study").size().items()]
    thermo = co[co.title == "Engineering Thermodynamics"].course_id.iloc[0]
    out["co10"] = [[float(mid[mid.course_id == thermo].score.mean())]]

    fails = fin[fin.score < PASS_MARK].groupby("student_id").size()
    picked = sorted(set(overall[overall < ATTENDANCE_CUT].index)
                    & set(fails[fails >= 2].index))
    names = st.set_index("student_id")["name"]
    out["cm01"] = [[int(s), names[s]] for s in picked]
    per_course = absent.groupby(["student_id", "course_id"]).size().rename(
        "absences").reset_index().merge(fin, on=["student_id", "course_id"])
    kept = per_course[(per_course.absences >= 5) & (per_course.score >= 80)]
    out["cm02"] = [[int(r.student_id), r.course_id, int(r.absences), int(r.score)]
                   for _, r in kept.iterrows()]
    course_avg = fin.groupby("course_id").score.mean()
    above = fin.assign(a=fin.score > fin.course_id.map(course_avg)).groupby(
        "student_id").a.all()
    out["cm03"] = [[int(above.sum())]]
    high = fin[fin.student_id.map(overall) >= ATTENDANCE_CUT].score.mean()
    low = fin[fin.student_id.map(overall) < ATTENDANCE_CUT].score.mean()
    out["cm04"] = [[float(high - low)]]
    rate = fin_c.assign(p=fin_c.score >= PASS_MARK).groupby(
        ["department", "course_id", "title"]).p.mean().mul(100).reset_index()
    lowest = rate.loc[rate.groupby("department").p.idxmin()]
    out["cm05"] = [[r.course_id, r.title, float(r.p)] for _, r in lowest.iterrows()]
    return out


# ---------------------------------------------------------------------------
# 4. No answer rests on a tie
# ---------------------------------------------------------------------------
def check_margins() -> tuple[list[str], list[str]]:
    """(problems, notes): ambiguities that block the freeze, and boundary
    values that the question wording settles."""
    f = frames("clean")
    st, co, mk, at = f["students"], f["courses"], f["marks"], f["attendance"]
    fin = mk[mk.exam == "final"]
    problems: list[str] = []
    notes: list[str] = []
    absences = at[at.status == "absent"].groupby("student_id").size().sort_values(
        ascending=False)
    if absences.iloc[2] == absences.iloc[3] or len(set(absences.iloc[:3])) < 3:
        problems.append("co08: the top 3 by absences is tied")
    student_avg = fin.groupby("student_id").score.mean()
    if (student_avg == 80).any():
        notes.append(f"co06: {int((student_avg == 80).sum())} student(s) average "
                     "exactly 80; the question says above 80")
    overall = at.assign(a=at.status.isin(["present", "late"])).groupby(
        "student_id").a.mean() * 100
    gap = float((overall - ATTENDANCE_CUT).abs().min())
    if gap < 0.05:
        problems.append("cm01/cm04: a student is within 0.05 points of 85% "
                        "attendance, inside att_pct's rounding")
    notes.append(f"cm01/cm04: the closest student is {gap:.2f} points from 85%")
    course_avg = fin.groupby("course_id").score.mean()
    equal = int((fin.score == fin.course_id.map(course_avg)).sum())
    if equal:
        notes.append(f"cm03: {equal} score(s) equal their course average; the "
                     "question says above the average")
    notes.append(f"cm01/cm05: {int((fin.score == PASS_MARK).sum())} final score(s) "
                 f"are exactly {PASS_MARK}; below 50 fails, 50 or more passes")
    fin_c = fin.merge(co, on="course_id")
    rate = fin_c.assign(p=fin_c.score >= PASS_MARK).groupby(
        ["department", "course_id"]).p.mean().reset_index()
    for dept, group in rate.groupby("department"):
        two = group.p.sort_values().iloc[:2].tolist()
        if two[0] == two[1]:
            problems.append(f"cm05: {dept} has two courses tied for the lowest rate")
    if len(st) != 240:
        problems.append("the student table changed size")
    return problems, notes


# ---------------------------------------------------------------------------
# 2 and 3 together
# ---------------------------------------------------------------------------
def decode(rows: list[list[Any]]) -> list[list[Any]]:
    return [[college.CODES.get(v, v) if isinstance(v, str) else v for v in row]
            for row in rows]


def check_gold() -> tuple[list[str], dict[str, dict[str, Any]]]:
    questions = college.load_questions()
    problems: list[str] = []
    # ignore_cleanup_errors: on Windows a converted file can still be open
    # (the converter's connection is closed by the garbage collector).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings(upload_dir=Path(tmp))
        store = DatasetStore(Path(tmp), max_bytes=10**9, ttl_seconds=10**6,
                             sweep=False)
        gold = {v: college.gold_for(college.upload(v, store, settings).path,
                                    questions, v) for v in college.VARIANTS}
        gc.collect()
    expected = pandas_answers()
    for q in questions:
        qid = q["id"]
        clean, messy = gold["clean"][qid], gold["messy"][qid]
        if not clean["rows"]:
            problems.append(f"{qid}: the gold answer is empty")
        want = Gold(rows=tuple(tuple(r) for r in clean["rows"]),
                    ordered=clean["ordered"])
        same = grade(want, decode(messy["rows"]))
        if not same.passed:
            problems.append(f"{qid}: clean and messy gold differ ({same.reason})")
        independent = grade(want, expected[qid])
        if not independent.passed:
            problems.append(f"{qid}: pandas disagrees with the gold SQL "
                            f"({independent.reason})")
    return problems, gold["clean"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true",
                    help="write eval/college_FREEZE.sha256 if every check passes")
    args = ap.parse_args()
    gold_problems, gold = check_gold()
    margin_problems, notes = check_margins()
    problems = check_copies() + gold_problems + margin_problems
    for qid, entry in gold.items():
        shown = entry["rows"] if len(entry["rows"]) <= 4 else (
            f"{len(entry['rows'])} rows, first {entry['rows'][0]}")
        print(f"{qid}: {shown}")
    print("\nboundary notes:")
    for note in notes:
        print(f"  - {note}")
    if problems:
        print("\nNOT VERIFIED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"\nverified: {len(gold)} questions; the two copies hold the same rows; "
          "clean SQL, messy SQL and pandas agree; no answer rests on a tie")
    if args.freeze:
        college.write_manifest()
        print(f"frozen: {college.FREEZE.relative_to(ROOT).as_posix()} "
              f"({len(college.frozen_files())} files)")
    else:
        stale = college.manifest_problems()
        print("manifest: " + ("matches" if not stale else "; ".join(stale)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
