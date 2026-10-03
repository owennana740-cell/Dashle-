"""Tests isolés de la persistance PostgreSQL des jobs vidéo."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import video_jobs
from database import Base, VideoGenerationJob


class FakeVideoProvider:
    def __init__(self):
        self.create_calls = 0

    def create(self, prompt, *, timeout_s):
        self.create_calls += 1
        return type("Job", (), {"job_id": "operations/fake-1", "status": "queued", "progress": None})()

    def status(self, job_id, *, timeout_s):
        return type("Job", (), {"job_id": job_id, "status": "running", "progress": 37.0, "error": None})()

    def retrieve(self, job_id, *, timeout_s):
        return type("Artifact", (), {
            "data": b"fake-video",
            "mime_type": "video/mp4",
            "filename": "video.mp4",
        })()


class VideoJobPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

        @contextmanager
        def isolated_session():
            db = self.Session()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        self.session_patch = patch.object(video_jobs, "session_base", isolated_session)
        self.session_patch.start()
        self.monitor_patch = patch.object(video_jobs, "_spawn_monitor")
        self.monitor_mock = self.monitor_patch.start()

    def tearDown(self):
        self.monitor_patch.stop()
        self.session_patch.stop()
        self.engine.dispose()

    def test_create_job_is_idempotent_and_persists(self):
        provider = FakeVideoProvider()
        first = video_jobs.create_job(
            provider, prompt="ville futuriste", user_id=7, owner_token_hash=None,
            conversation_id=3, idempotency_key="action-1"
        )
        second = video_jobs.create_job(
            provider, prompt="ville futuriste", user_id=7, owner_token_hash=None,
            conversation_id=3, idempotency_key="action-1"
        )
        self.assertEqual(first, second)
        self.assertEqual(provider.create_calls, 1)
        with self.Session() as db:
            job = db.get(VideoGenerationJob, first)
            self.assertEqual(job.provider_job_id, "operations/fake-1")
            self.assertEqual(job.status, "queued")

    def test_restart_resume_uses_persisted_active_jobs(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="restart-job", user_id=7, provider="veo", tool="video_generation",
                status="processing", progress=37.0, status_message="Génération en cours…",
                provider_job_id="operations/restart", idempotency_key="restart-key",
                created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        provider = FakeVideoProvider()
        count = video_jobs.resume_active_jobs(provider)
        self.assertEqual(count, 1)
        self.monitor_mock.assert_called_once_with("restart-job", provider)

    def test_provider_progress_is_persisted_without_fabrication(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="progress-job", user_id=7, provider="veo", tool="video_generation",
                status="processing", progress=None, status_message="Génération en cours…",
                provider_job_id="operations/progress", idempotency_key="progress-key",
                created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        provider = FakeVideoProvider()
        worker = video_jobs._claim("progress-job")
        self.assertIsNotNone(worker)
        remote = provider.status("operations/progress", timeout_s=20)
        video_jobs._persist_remote("progress-job", worker, remote)
        snap = video_jobs.snapshot_for_owner("progress-job", user_id=7, owner_token_hash=None)
        self.assertEqual(snap["progress"], 37.0)

    def test_completion_persists_result_and_metadata(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="complete-job", user_id=7, provider="veo", tool="video_generation",
                status="processing", progress=73.0, status_message="Génération en cours…",
                provider_job_id="operations/complete", idempotency_key="complete-key",
                created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        worker = video_jobs._claim("complete-job")
        video_jobs._store_artifact("complete-job", worker, FakeVideoProvider().retrieve("operations/complete", timeout_s=60))
        meta = video_jobs.result_metadata("complete-job", user_id=7, owner_token_hash=None)
        self.assertEqual(meta["mime_type"], "video/mp4")
        self.assertTrue(meta["url"].endswith("/complete-job/result"))
        data = video_jobs.read_result("complete-job", user_id=7, owner_token_hash=None)
        self.assertEqual(data[0], b"fake-video")

    def test_user_cannot_read_another_users_job(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="private-job", user_id=11, provider="veo", tool="video_generation",
                status="processing", progress=None, status_message="Génération en cours…",
                provider_job_id="operations/private", idempotency_key="private-key",
                created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        self.assertIsNone(video_jobs.snapshot_for_owner("private-job", user_id=12, owner_token_hash=None))
        self.assertIsNotNone(video_jobs.snapshot_for_owner("private-job", user_id=11, owner_token_hash=None))

    def test_visitor_owner_token_isolated(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="visitor-job", user_id=None, owner_token_hash=video_jobs.hash_owner_token("token-a"),
                provider="veo", tool="video_generation", status="processing", progress=None,
                status_message="Génération en cours…", provider_job_id="operations/visitor",
                idempotency_key="visitor-key", created_at=now, updated_at=now,
                expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        self.assertIsNone(video_jobs.snapshot_for_owner(
            "visitor-job", user_id=None, owner_token_hash=video_jobs.hash_owner_token("token-b")
        ))
        self.assertIsNotNone(video_jobs.snapshot_for_owner(
            "visitor-job", user_id=None, owner_token_hash=video_jobs.hash_owner_token("token-a")
        ))

    def test_expired_processing_job_becomes_expired(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="expired-job", user_id=7, provider="veo", tool="video_generation",
                status="processing", progress=None, status_message="Génération en cours…",
                provider_job_id="operations/expired", idempotency_key="expired-key",
                created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2),
                expires_at=now - timedelta(seconds=1),
            ))
            db.commit()
        self.assertIsNone(video_jobs._claim("expired-job"))
        snap = video_jobs.snapshot_for_owner("expired-job", user_id=7, owner_token_hash=None)
        self.assertEqual(snap["status"], "expired")


if __name__ == "__main__":
    unittest.main()
