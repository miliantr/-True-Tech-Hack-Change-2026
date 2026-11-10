"""Competition entry point: python solution.py"""
import csv
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config import Config, env
from consultant import Consultant, UNAVAILABLE, parse_dialogue
from retrieval import locate_kb


def read_questions(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"id", "question"}.issubset(reader.fieldnames):
            raise ValueError("Input requires id,question columns")
        rows = list(reader)
    ids = [r["id"] for r in rows]
    if any(not i or not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Input IDs must be nonempty and unique")
    if any(not isinstance(r["question"],str) or not r["question"].strip() for r in rows):
        raise ValueError("Questions must be nonempty")
    return rows


def write_submission(path, rows, answers):
    if len(rows) != len(answers) or any(not a.strip() or len(a)>10000 for a in answers):
        raise ValueError("Invalid output answers")
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(["id", "answer"])
        writer.writerows((r["id"], " ".join(a.split())) for r,a in zip(rows,answers))
    os.replace(temporary, path)


def main():
    config = Config.from_env()
    root = Path(env("DATA_DIR", default="data"))
    question_path = next((root/n for n in ("questions.csv","train.csv") if (root/n).is_file()), None)
    if question_path is None:
        raise FileNotFoundError("DATA_DIR/questions.csv not found")
    rows = read_questions(question_path)
    preview = env("ALLOW_PREVIEW", default="0") == "1"
    if not config.live and not preview:
        raise ValueError("Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL; preview is disabled for submissions")
    started = time.monotonic()
    deadline = started + config.runtime
    answers = [UNAVAILABLE] * len(rows)
    # Checkpoint first so every input ID has output even if generation is interrupted.
    write_submission("submission.csv", rows, answers)
    engine = Consultant(locate_kb(root), config, deadline=min(deadline-5,time.monotonic()+180))

    def work(pair):
        i, row = pair
        if time.monotonic() >= deadline-5:
            return i, UNAVAILABLE, "unavailable", ["Run deadline exceeded"]
        result = engine.answer(parse_dialogue(row["question"]), deadline=min(deadline-5,time.monotonic()+110), preview=preview)
        return i, result["answer"], result["mode"], result["debug"]["warnings"]

    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        futures = [pool.submit(work, pair) for pair in enumerate(rows)]
        unavailable = 0
        warnings = Counter()
        for done, future in enumerate(as_completed(futures), 1):
            i, answer, mode, row_warnings = future.result()
            answers[i] = answer
            unavailable += mode == "unavailable"
            warnings.update(row_warnings)
            if done % 10 == 0 or done == len(rows):
                write_submission("submission.csv", rows, answers)
    print(f"Created submission.csv: {len(answers)} answers, {unavailable} unavailable, mode={'live' if config.live else 'preview'}")
    print(f"Run: {time.monotonic() - started:.1f}s, retrieval={config.retrieval}, context={config.top_n}, "
          f"rerank={config.rerank}, workers={config.workers}")
    for warning, count in warnings.most_common():
        print(f"{count} × {warning}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
