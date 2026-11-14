"""Build the synthetic college dataset used by the unseen-schema test.

    python scripts/build_college_dataset.py

Writes four CSV files to eval/college/: students, courses, marks and
attendance. It is a schema no part of the agent was built or tuned on, with
the shape of data a college would export from a spreadsheet: no foreign
keys, generic columns in several tables (name, department, title), dates as
ISO text, and tables related only by matching id columns.

Seeded, so the files are identical on every run. Two patterns are planted so
questions have clear answers: Mechanical students miss more classes, and
Computer Science final exams score higher.

It also writes eval/college_messy/: the SAME rows as a legacy system would
export them, to test the agent where names say little:

* cryptic table and column names (stu_mst.stu_nm, exm_mk.mk_obt, crs_mst.crd);
* codes instead of words: departments as CSE / ECE / ME / CE, exams as
  MT / FN, attendance as P / A / L, with no lookup table;
* a fifth, derived table att_sum (classes held and attended and att_pct per
  student and course), the kind of summary such systems keep beside the
  log. A class counts as attended when the log says P or L.

Nothing else differs, so every question has the same answer on both copies
(apart from a department appearing as its code), which
scripts/check_college_gold.py verifies.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "college"
SEED = 2026

DEPARTMENTS = ("Computer Science", "Electronics", "Mechanical", "Civil")
COURSES = (
    ("CS201", "Data Structures", "Computer Science", 4),
    ("CS305", "Database Systems", "Computer Science", 4),
    ("CS310", "Operating Systems", "Computer Science", 3),
    ("CS420", "Machine Learning", "Computer Science", 4),
    ("CS110", "Programming Fundamentals", "Computer Science", 3),
    ("EC210", "Signals and Systems", "Electronics", 4),
    ("EC220", "Digital Electronics", "Electronics", 3),
    ("EC330", "Microprocessors", "Electronics", 3),
    ("EC340", "Communication Systems", "Electronics", 4),
    ("EC150", "Circuit Theory", "Electronics", 3),
    ("ME201", "Engineering Thermodynamics", "Mechanical", 4),
    ("ME210", "Fluid Mechanics", "Mechanical", 4),
    ("ME320", "Machine Design", "Mechanical", 3),
    ("ME330", "Manufacturing Processes", "Mechanical", 3),
    ("ME140", "Engineering Drawing", "Mechanical", 3),
    ("CE201", "Structural Analysis", "Civil", 4),
    ("CE215", "Surveying", "Civil", 3),
    ("CE320", "Geotechnical Engineering", "Civil", 4),
    ("CE330", "Transportation Engineering", "Civil", 3),
    ("CE140", "Building Materials", "Civil", 3),
)
FIRST = ("Aarav", "Diya", "Kabir", "Meera", "Rohan", "Sneha", "Arjun", "Priya",
         "Vikram", "Ananya", "Ishaan", "Kavya", "Nikhil", "Pooja", "Rahul",
         "Tara", "Yash", "Zoya", "Aditya", "Neha")
LAST = ("Sharma", "Reddy", "Iyer", "Khan", "Patel", "Nair", "Gupta", "Das",
        "Rao", "Singh", "Menon", "Joshi", "Verma", "Bose", "Kulkarni")
SESSIONS = 15
TERM_START = date(2026, 1, 12)

MESSY_OUT = ROOT / "eval" / "college_messy"
DEPT_CODES = {"Computer Science": "CSE", "Electronics": "ECE", "Mechanical": "ME",
              "Civil": "CE"}
EXAM_CODES = {"midterm": "MT", "final": "FN"}
STATUS_CODES = {"present": "P", "absent": "A", "late": "L"}


def write(name: str, header: list[str], rows: list[list[object]],
          out: Path = OUT) -> None:
    with (out / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)
    OUT.mkdir(parents=True, exist_ok=True)

    students = []
    for i in range(240):
        dept = DEPARTMENTS[i % len(DEPARTMENTS)] if i < 200 else rng.choice(DEPARTMENTS)
        year = rng.randint(1, 4)
        admitted = date(2026 - year, 7, 15) + timedelta(days=rng.randint(0, 30))
        students.append([1001 + i, f"{rng.choice(FIRST)} {rng.choice(LAST)}", dept,
                         year, rng.choice(("F", "M")), admitted.isoformat()])
    write("students", ["student_id", "name", "department", "year_of_study",
                       "gender", "admission_date"], students)

    courses = [[cid, title, dept, credits, "2026-Spring", f"Prof. {rng.choice(LAST)}"]
               for cid, title, dept, credits in COURSES]
    write("courses", ["course_id", "title", "department", "credits", "semester",
                      "instructor"], courses)

    by_dept = {d: [c for c in COURSES if c[2] == d] for d in DEPARTMENTS}
    marks, attendance = [], []
    for sid, _, dept, _, _, _ in students:
        own = rng.sample(by_dept[dept], 4)
        other = rng.choice([c for c in COURSES if c[2] != dept])
        skill = rng.gauss(0, 8)
        miss = 0.16 if dept == "Mechanical" else 0.07
        for cid, _, cdept, _ in [*own, other]:
            for exam, day, bonus in (("midterm", date(2026, 3, 9), 0),
                                     ("final", date(2026, 5, 18),
                                      6 if cdept == "Computer Science" else 0)):
                score = max(0, min(100, round(rng.gauss(68 + bonus, 10) + skill)))
                marks.append([sid, cid, exam, score, (day + timedelta(
                    days=rng.randint(0, 4))).isoformat()])
            for n in range(SESSIONS):
                roll = rng.random()
                status = ("absent" if roll < miss else
                          "late" if roll < miss + 0.05 else "present")
                session = TERM_START + timedelta(weeks=n)
                attendance.append([sid, cid, session.isoformat(), status])
    write("marks", ["student_id", "course_id", "exam", "score", "exam_date"], marks)
    write("attendance", ["student_id", "course_id", "session_date", "status"],
          attendance)
    print(f"wrote {OUT}: {len(students)} students, {len(COURSES)} courses, "
          f"{len(marks)} marks, {len(attendance)} attendance rows")
    write_messy(students, courses, marks, attendance)


def write_messy(students: list[list], courses: list[list], marks: list[list],
                attendance: list[list]) -> None:
    """The same rows under a legacy system's names and codes. Uses no random
    numbers, so the clean files above are unaffected by it."""
    MESSY_OUT.mkdir(parents=True, exist_ok=True)
    write("stu_mst", ["stu_id", "stu_nm", "dept_cd", "yr", "gndr", "adm_dt"],
          [[sid, name, DEPT_CODES[dept], year, gender, admitted]
           for sid, name, dept, year, gender, admitted in students], MESSY_OUT)
    write("crs_mst", ["crs_cd", "crs_ttl", "dept_cd", "crd", "sem", "instr"],
          [[cid, title, DEPT_CODES[dept], credits, sem, instructor]
           for cid, title, dept, credits, sem, instructor in courses], MESSY_OUT)
    write("exm_mk", ["stu_id", "crs_cd", "exm_typ", "mk_obt", "exm_dt"],
          [[sid, cid, EXAM_CODES[exam], score, day]
           for sid, cid, exam, score, day in marks], MESSY_OUT)
    write("att_log", ["stu_id", "crs_cd", "ses_dt", "att_st"],
          [[sid, cid, day, STATUS_CODES[status]]
           for sid, cid, day, status in attendance], MESSY_OUT)
    held: dict[tuple[int, str], int] = {}
    attended: dict[tuple[int, str], int] = {}
    for sid, cid, _, status in attendance:
        held[(sid, cid)] = held.get((sid, cid), 0) + 1
        attended[(sid, cid)] = attended.get((sid, cid), 0) + (status != "absent")
    write("att_sum", ["stu_id", "crs_cd", "cls_hld", "cls_att", "att_pct"],
          [[sid, cid, n, attended[(sid, cid)], round(100 * attended[(sid, cid)] / n, 2)]
           for (sid, cid), n in held.items()], MESSY_OUT)
    print(f"wrote {MESSY_OUT}: the same rows, cryptic names and codes, plus "
          f"att_sum ({len(held)} rows)")


if __name__ == "__main__":
    main()
