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
    parser.add_argument("--compose-only", action="store_true", help="only rebuild the final video from cached artifacts")
    parser.add_argument("--marketing-cover-only", action="store_true", help="only rebuild titles, synopses and cover")
    parser.add_argument("--preprocess-only", action="store_true", help="accelerator: prepare API-backed stages, then pause")
    args = parser.parse_args(argv)

    job_dir = pr.job_dir_for(args.job_id)
    pr.record_worker_pid(args.job_id, os.getpid())
    pr.write_status(job_dir, job_id=args.job_id, worker_pid=os.getpid(), stage="worker_starting")
    pr.append_log(job_dir, f"== worker pid={os.getpid()} starting ==")
    try:
        if args.preprocess_only:
            pr.run_acceleration_preprocess(
                args.input,
                job_id=args.job_id,
            )
        elif args.marketing_cover_only:
            cover = pr.regenerate_job_marketing_and_cover(
                args.job_id, on_log=lambda message: pr.append_log(job_dir, message), allow_running=True
            )
            status = pr.load_status(args.job_id, include_worker=False)
            pr.write_status(
                job_dir, job_id=args.job_id, worker_pid=None, stage="completed", progress=1.0,
                cover=str(cover), video=str(status.get("video") or ""), error="",
            )
        elif args.compose_only:
            pr.write_status(job_dir, job_id=args.job_id, worker_pid=os.getpid(), stage="compose", progress=0.90, error="")
            video = pr.resume_compose_only(args.job_id, on_log=lambda message: pr.append_log(job_dir, message))
            pr.write_status(
                job_dir,
                job_id=args.job_id,
                worker_pid=None,
                stage="completed",
                progress=1.0,
                video=str(video),
                error="",
            )
        else:
            pr.run_full(args.input, job_id=args.job_id, resume=args.resume)
        return 0
    except Exception as exc:
        pr.append_log(job_dir, f"== worker failed: {exc} ==")
        if args.compose_only or args.marketing_cover_only or args.preprocess_only:
            pr.write_status(
                job_dir,
                job_id=args.job_id,
                worker_pid=None,
                stage="failed",
                progress=0.12 if args.preprocess_only else 0.90,
                error=str(exc),
            )
        return 1
    finally:
        pr.clear_worker_pid(args.job_id, os.getpid())
        # Clear our durable PID before dispatching.  Otherwise the capacity
        # check still counts this just-finished process and a one-at-a-time
        # batch can never advance to its next queued job.
        pr.start_next_queued_job(
            exclude_job_id=None if args.preprocess_only else args.job_id,
            on_log=lambda message: pr.append_log(job_dir, message),
        )
        pr.append_log(job_dir, f"== worker pid={os.getpid()} stopped ==")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
