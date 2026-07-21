"""Gunicorn configuration for production.

Sets CCTV_WORKER_ID per worker so only worker 0 runs the AutoPreparer
background thread (avoiding multiple ffmpeg processes fighting over files).
"""
import os


def post_fork(server, worker):
    worker_id = str(worker.age - 1)
    os.environ["CCTV_WORKER_ID"] = worker_id
    server.log.info("Worker %s spawned (CCTV_WORKER_ID=%s, pid=%s)",
                    worker.age, worker_id, worker.pid)
