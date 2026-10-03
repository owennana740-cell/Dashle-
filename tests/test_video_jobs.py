"""Tests isolés de la persistance PostgreSQL des jobs vidéo."""
import os
import tempfile
import time
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event
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
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(
            "sqlite+pysqlite:///" + self.db_path,
            connect_args={"check_same_thread": False},
        )
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
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

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
        deadline = time.monotonic() + 2
        while provider.create_calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(provider.create_calls, 1)
        with self.Session() as db:
            job = db.get(VideoGenerationJob, first)
            self.assertEqual(job.provider_job_id, "operations/fake-1")
            self.assertEqual(job.status, "queued")


    def test_concurrent_idempotent_creation_postgresql(self):
        database_url = os.environ.get("VIDEO_JOBS_TEST_DATABASE_URL")
        if not database_url:
            self.skipTest("VIDEO_JOBS_TEST_DATABASE_URL non configurée")

        engine = create_engine(database_url, pool_pre_ping=True)
        VideoGenerationJob.__table__.create(engine, checkfirst=True)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        barrier = threading.Barrier(2)
        insert_barrier = threading.Barrier(2)
        provider_calls = 0
        provider_lock = threading.Lock()

        @contextmanager
        def concurrent_session():
            db = Session()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if "INSERT INTO video_generation_jobs" in statement:
                insert_barrier.wait(timeout=5)

        event.listen(engine, "before_cursor_execute", before_cursor_execute)

        class ConcurrentProvider(FakeVideoProvider):
            def create(self, prompt, *, timeout_s):
                nonlocal provider_calls
                with provider_lock:
                    provider_calls += 1
                return super().create(prompt, timeout_s=timeout_s)

        provider = ConcurrentProvider()
        results = []
        errors = []

        def create_from_worker():
            try:
                barrier.wait(timeout=5)
                results.append(video_jobs.create_job(
                    provider, prompt="même demande vidéo", user_id=7,
                    owner_token_hash=None, conversation_id=None,
                    idempotency_key="concurrent-action"
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create_from_worker) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            with Session() as db:
                rows = (
                    db.query(VideoGenerationJob)
                    .filter_by(idempotency_key=video_jobs._scoped_idempotency_key(
                        "concurrent-action", user_id=7, owner_token_hash=None
                    ))
                    .all()
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].id, results[0])
                self.assertEqual(rows[0].user_id, 7)
            deadline = time.monotonic() + 2
            while provider_calls < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(provider_calls, 1)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
            with engine.begin() as connection:
                connection.execute(VideoGenerationJob.__table__.delete())
            engine.dispose()

    def test_same_idempotency_key_is_scoped_to_owner(self):
        provider = FakeVideoProvider()
        first = video_jobs.create_job(
            provider, prompt="vidéo A", user_id=7, owner_token_hash=None,
            conversation_id=None, idempotency_key="same-action"
        )
        second = video_jobs.create_job(
            provider, prompt="vidéo B", user_id=8, owner_token_hash=None,
            conversation_id=None, idempotency_key="same-action"
        )
        self.assertNotEqual(first, second)
        deadline = time.monotonic() + 2
        while provider.create_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(provider.create_calls, 2)

    def test_creation_returns_without_waiting_for_provider(self):
        started = threading.Event()
        release = threading.Event()

        class SlowProvider(FakeVideoProvider):
            def create(self, prompt, *, timeout_s):
                self.create_calls += 1
                started.set()
                release.wait(2)
                return type("Job", (), {"job_id": "operations/slow", "status": "queued", "progress": None})()

        provider = SlowProvider()
        started_at = time.monotonic()
        job_id = video_jobs.create_job(
            provider, prompt="ville futuriste", user_id=7, owner_token_hash=None,
            conversation_id=None, idempotency_key="slow-create"
        )
        elapsed = time.monotonic() - started_at
        self.assertTrue(job_id)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(started.wait(1))
        release.set()
        deadline = time.monotonic() + 2
        while not self.monitor_mock.called and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.monitor_mock.called)

    def test_restart_resume_recreates_queued_job_with_persisted_prompt(self):
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(VideoGenerationJob(
                id="queued-restart", user_id=7, provider="veo", tool="video_generation",
                status="queued", progress=None, status_message="Préparation de la génération…",
                provider_job_id=None, idempotency_key="queued-restart-key",
                prompt="reprendre cette vidéo", created_at=now, updated_at=now,
                expires_at=now + timedelta(minutes=30),
            ))
            db.commit()
        provider = FakeVideoProvider()
        video_jobs.resume_active_jobs(provider)
        deadline = time.monotonic() + 2
        while provider.create_calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(provider.create_calls, 1)
        deadline = time.monotonic() + 2
        while not self.monitor_mock.called and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.monitor_mock.called)
        with self.Session() as db:
            job = db.get(VideoGenerationJob, "queued-restart")
            self.assertEqual(job.provider_job_id, "operations/fake-1")
            self.assertIsNone(job.prompt)


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
