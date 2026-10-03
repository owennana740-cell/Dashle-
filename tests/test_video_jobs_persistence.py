import unittest
from unittest import mock
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import video_jobs
from database import Base, VideoGenerationJob
from tool_providers import ImageArtifact, ProviderUnavailable, VideoJob


class FakeVideoProvider:
    def __init__(self):
        self.create_calls = []
        self.status_calls = []
        self.cancel_calls = []
        self._statuses = {}

    def create(self, prompt, *, timeout_s):
        job_id = f"operations/{len(self.create_calls) + 1}"
        self.create_calls.append(prompt)
        self._statuses[job_id] = 0
        return VideoJob(job_id=job_id, status="queued", progress=None)

    def status(self, job_id, *, timeout_s):
        self.status_calls.append(job_id)
        step = self._statuses.get(job_id, 0)
        if step == 0:
            self._statuses[job_id] = 1
            return VideoJob(job_id=job_id, status="running", progress=37.0)
        return VideoJob(job_id=job_id, status="succeeded", progress=100.0)

    def retrieve(self, job_id, *, timeout_s):
        return ImageArtifact(b"video-bytes", "video/mp4", "video.mp4")

    def cancel(self, job_id, *, timeout_s):
        self.cancel_calls.append(job_id)
        return True


class FakeRegistry:
    def __init__(self, provider=None, available=True):
        self.provider = provider or FakeVideoProvider()
        self._available = available

    def get(self, tool):
        return self.provider if tool == "video_generation" else None

    def available(self, tool):
        return tool == "video_generation" and self._available


class VideoJobPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)

    def setUp(self):
        self.SessionLocal = self.Session

        @contextmanager
        def isolated_session():
            db = self.SessionLocal()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        self.session_patch = mock.patch.object(video_jobs, "session_base", isolated_session)
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        with self.SessionLocal() as db:
            db.query(VideoGenerationJob).delete()
            db.commit()

    def test_create_persists_job_without_provider_call_until_worker(self):
        provider = FakeVideoProvider()
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        with mock.patch.object(store, "_start_worker"):
            job_id = store.create("une ville futuriste", user_id=7, visitor_key_hash=None, conversation_id=11)
        with self.SessionLocal() as db:
            job = db.get(VideoGenerationJob, job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.user_id, 7)
            self.assertIsNone(job.provider_job_id)
            self.assertEqual(job.prompt, "une ville futuriste")

    def test_processing_job_survives_store_restart_and_completes(self):
        provider = FakeVideoProvider()
        first = video_jobs.VideoJobStore(FakeRegistry(provider))
        with mock.patch.object(first, "_start_worker"):
            job_id = first.create("ville", user_id=7, visitor_key_hash=None, conversation_id=11)
        first._process_job(job_id)

        second = video_jobs.VideoJobStore(FakeRegistry(provider))
        second._process_job(job_id)

        with self.SessionLocal() as db:
            job = db.get(VideoGenerationJob, job_id)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.progress, 100.0)
            self.assertEqual(job.provider_job_id, "operations/1")
            self.assertEqual(job.result_data, b"video-bytes")
            self.assertIsNone(job.prompt)
        self.assertEqual(len(provider.create_calls), 1)

    def test_recovery_with_saved_provider_id_never_creates_duplicate(self):
        provider = FakeVideoProvider()
        now = datetime.utcnow()
        job_id = "restart-job"
        with self.SessionLocal() as db:
            db.add(VideoGenerationJob(
                id=job_id, user_id=3, visitor_key_hash=None, provider="gemini",
                tool_type="video_generation", status="processing", progress=37.0,
                message="Génération en cours…", provider_job_id="operations/existing",
                prompt=None, error_code=None, error_message=None, result_data=None,
                result_mime_type=None, result_filename=None, result_size_bytes=None,
                conversation_id=None, created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=2), lease_until=now - timedelta(seconds=1),
                last_checked_at=now, completed_at=None,
            ))
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        store._process_job(job_id)
        self.assertEqual(provider.create_calls, [])
        with self.SessionLocal() as db:
            self.assertEqual(db.get(VideoGenerationJob, job_id).status, "completed")

    def test_expired_job_becomes_expired_without_provider_call(self):
        provider = FakeVideoProvider()
        now = datetime.utcnow()
        job_id = "expired-job"
        with self.SessionLocal() as db:
            db.add(VideoGenerationJob(
                id=job_id, user_id=4, visitor_key_hash=None, provider="gemini",
                tool_type="video_generation", status="processing", progress=None,
                message="Génération en cours…", provider_job_id="operations/expired",
                prompt=None, error_code=None, error_message=None, result_data=None,
                result_mime_type=None, result_filename=None, result_size_bytes=None,
                conversation_id=None, created_at=now - timedelta(hours=3),
                updated_at=now - timedelta(hours=3),
                expires_at=now - timedelta(minutes=1), lease_until=now - timedelta(minutes=1),
                last_checked_at=None, completed_at=None,
            ))
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        store._process_job(job_id)
        with self.SessionLocal() as db:
            job = db.get(VideoGenerationJob, job_id)
            self.assertEqual(job.status, "expired")
        self.assertEqual(provider.status_calls, [])

    def test_owner_isolation(self):
        provider = FakeVideoProvider()
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        now = datetime.utcnow()
        with self.SessionLocal() as db:
            db.add(VideoGenerationJob(
                id="private-job", user_id=10, visitor_key_hash=None, provider="gemini",
                tool_type="video_generation", status="processing", progress=None,
                message="Génération en cours…", provider_job_id="operations/private",
                prompt=None, error_code=None, error_message=None, result_data=None,
                result_mime_type=None, result_filename=None, result_size_bytes=None,
                conversation_id=None, created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=2), lease_until=now - timedelta(seconds=1),
                last_checked_at=None, completed_at=None,
            ))
        self.assertIsNone(store.get("private-job", user_id=11, visitor_key_hash=None))
        self.assertIsNotNone(store.get("private-job", user_id=10, visitor_key_hash=None))

    def test_provider_unavailable_is_explicit(self):
        store = video_jobs.VideoJobStore(FakeRegistry(available=False))
        with unittest.mock.patch.object(store, "_start_worker"):
            with self.assertRaises(ProviderUnavailable):
                store.create("test", user_id=1, visitor_key_hash=None, conversation_id=None)

    def test_multiple_jobs_are_independent_and_idempotent(self):
        provider = FakeVideoProvider()
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        with unittest.mock.patch.object(store, "_start_worker"):
            first = store.create("video A", user_id=1, visitor_key_hash=None, conversation_id=None)
            second = store.create("video B", user_id=2, visitor_key_hash=None, conversation_id=None)
        store._process_job(first)
        store._process_job(second)
        store._process_job(first)
        self.assertEqual(len(provider.create_calls), 2)
        with self.SessionLocal() as db:
            self.assertEqual(db.get(VideoGenerationJob, first).status, "completed")
            self.assertEqual(db.get(VideoGenerationJob, second).status, "completed")

    def test_sse_reports_real_provider_progress_without_fabrication(self):
        provider = FakeVideoProvider()
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        now = datetime.utcnow()
        with self.SessionLocal() as db:
            db.add(VideoGenerationJob(
                id="progress-job", user_id=6, visitor_key_hash=None, provider="gemini",
                tool_type="video_generation", status="processing", progress=37.0,
                message="Génération en cours… 37 %", provider_job_id="operations/progress",
                prompt=None, error_code=None, error_message=None, result_data=None,
                result_mime_type=None, result_filename=None, result_size_bytes=None,
                conversation_id=None, created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=2), lease_until=now + timedelta(seconds=10),
                last_checked_at=now, completed_at=None,
            ))
        stream = store.stream("progress-job", user_id=6, visitor_key_hash=None)
        event = next(stream)
        self.assertIn('"progress": 37.0', event)
        self.assertIn("Génération en cours… 37 %", event)

    def test_cancel_stops_local_tracking_and_reports_remote_limit(self):
        provider = FakeVideoProvider()
        store = video_jobs.VideoJobStore(FakeRegistry(provider))
        now = datetime.utcnow()
        with self.SessionLocal() as db:
            db.add(VideoGenerationJob(
                id="cancel-job", user_id=5, visitor_key_hash=None, provider="gemini",
                tool_type="video_generation", status="processing", progress=37.0,
                message="Génération en cours…", provider_job_id="operations/cancel",
                prompt=None, error_code=None, error_message=None, result_data=None,
                result_mime_type=None, result_filename=None, result_size_bytes=None,
                conversation_id=None, created_at=now, updated_at=now,
                expires_at=now + timedelta(hours=2), lease_until=now + timedelta(seconds=10),
                last_checked_at=now, completed_at=None,
            ))
        result = store.cancel("cancel-job", user_id=5, visitor_key_hash=None)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(provider.cancel_calls, ["operations/cancel"])


if __name__ == "__main__":
    unittest.main()
