#!/usr/bin/env python3
from __future__ import annotations
import os
import signal
import time
from pathlib import Path
from server.cloud_parity.config import PlatformSettings
from server.cloud_parity.store import PlatformStore
from server.inbound_control.knowledge import KnowledgeStore

ROOT=Path(__file__).resolve().parents[1]; running=True
def stop(*_args):
    global running; running=False
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
settings=PlatformSettings.from_env(ROOT)
platform=PlatformStore(settings.database_path,database_url=settings.database_url,min_pool_size=1,max_pool_size=4)
platform.initialize();knowledge=KnowledgeStore(platform);knowledge.migrate()
try:
    while running:
        jobs=knowledge.pending_job_ids(int(os.getenv("INBOUND_KNOWLEDGE_JOB_BATCH", "4")))
        for job_id in jobs: knowledge.process_job(job_id)
        if not jobs: time.sleep(float(os.getenv("INBOUND_KNOWLEDGE_POLL_SECONDS", "1")))
finally: platform.close()
