"""Worker process entry point for long pipeline jobs."""
from __future__ import annotations

import argparse
import os
import sys

from app import pipeline_runner as pr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one novel video pipeline job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    job_dir = pr.job_dir_for(args.job_id)
    pr.record_worker_pid(args.job_id, os.getpid())
    pr.write_status(job_dir, job_id=args.job_id, worker_pid=os.getpid(), stage="worker_starting")
    pr.append_log(job_dir, f"== worker pid={os.getpid()} starting ==")
    try:
        pr.run_full(args.input, job_id=args.job_id, resume=args.resume)
        return 0
    except Exception as exc:
        pr.append_log(job_dir, f"== worker failed: {exc} ==")
        return 1
    finally:
        pr.append_log(job_dir, f"== worker pid={os.getpid()} stopped ==")
        pr.clear_worker_pid(args.job_id, os.getpid())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
