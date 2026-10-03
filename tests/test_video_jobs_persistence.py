"""Tests de persistance, reprise, isolation et idempotence des jobs vidéo."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import web
import video_jobs
from database import VideoGenerationJob, session_base
from tool_providers import VideoJob, VideoArtifact


class VideoJobPersistenceTests(unittest.TestCase):
    def _insert(self, job_id, **kwargs):
        now = datetime.utcnow()
        values = dict(
            id=job_id, user_id=101, visitor_key=None, conversation_id=None,
            provider="gemini", tool_type="video_generation", prompt="video test",
            status="processing", progress=None, status_message="Génération en cours…",
            provider_job_id="operations/" + job_id, expires_at=now + timedelta(hours=1),
            retention_until=now + timedelta(days=7),
        )
        values.update(kwargs)
        with session_base() as db:
            db.add(VideoGenerationJob(**values))

    def _delete(self, job_id):
        with session_base() as db:
            db.query(VideoGenerationJob).filter_by(id=job_id).delete()

    def test_persistent_schema_contains_video_jobs(self):
        from sqlalchemy import inspect
        columns = {c["name"] for c in inspect(__import__("database").engine).get_columns("video_generation_jobs")}
        required = {"id","user_id","visitor_key","provider","tool_type","status","progress",
                    "provider_job_id","result_data","error","lease_token","expires_at",
                    "retention_until","created_at","updated_at"}
        self.assertTrue(required.issubset(columns))

    def test_owner_isolation(self):
        job_id = "test-owner-isolation"
        self._insert(job_id, user_id=101)
        try:
            self.assertIsNone(video_jobs.get_owned(job_id, user_id=202, visitor_key_value=None))
            self.assertIsNotNone(video_jobs.get_owned(job_id, user_id=101, visitor_key_value=None))
        finally:
            self._delete(job_id)

    def test_claim_is_idempotent(self):
        job_id = "test-claim-idempotent"
        self._insert(job_id, status="queued")
        try:
            first = video_jobs._claim(job_id)
            second = video_jobs._claim(job_id)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
        finally:
            self._delete(job_id)

    def test_processing_job_resumes_and_persists_completed_result(self):
        job_id = "test-resume-completed"
        self._insert(job_id, status="processing", progress=None)
        class FakeProvider:
            def status(self, provider_job_id, *, timeout_s):
                return VideoJob(provider_job_id, "succeeded", 100.0)
            def retrieve(self, provider_job_id, *, timeout_s):
                return VideoArtifact(b"video-bytes", "video/mp4", "resume.mp4")
        class FakeRegistry:
            def get(self, name):
                return FakeProvider()
            def available(self, name):
                return True
        old = video_jobs._PROVIDER_REGISTRY
        video_jobs.configure(FakeRegistry())
        try:
            video_jobs._process(job_id)
            with session_base() as db:
                job = db.get(VideoGenerationJob, job_id)
                self.assertEqual(job.status, "completed")
                self.assertEqual(job.progress, 100.0)
                self.assertEqual(job.result_data, b"video-bytes")
                self.assertEqual(job.result_filename, "resume.mp4")
        finally:
            video_jobs._PROVIDER_REGISTRY = old
            self._delete(job_id)

    def test_provider_progress_is_real_and_null_when_missing(self):
        job_id = "test-progress-null"
        self._insert(job_id, progress=None)
        try:
            event = next(video_jobs.stream_events(job_id, user_id=101, visitor_key_value=None))
            self.assertIsNone(event["action"]["result"]["progress"])
        finally:
            self._delete(job_id)

    def test_sse_result_does_not_embed_video_bytes(self):
        job_id = "test-sse-result-url"
        self._insert(job_id, status="completed", progress=100.0, result_mime_type="video/mp4",
                     result_filename="video.mp4", result_data=b"SECRET_VIDEO_BYTES")
        try:
            event = next(video_jobs.stream_events(job_id, user_id=101, visitor_key_value=None))
            self.assertEqual(event["event"], "action_completed")
            artifact = event["action"]["result"]["artifact"]
            self.assertIn("/api/outils/jobs/test-sse-result-url/result", artifact["url"])
            self.assertNotIn("SECRET_VIDEO_BYTES", str(event))
        finally:
            self._delete(job_id)

    def test_expired_job_becomes_expired(self):
        job_id = "test-expired-job"
        self._insert(job_id, expires_at=datetime.utcnow() - timedelta(seconds=1))
        try:
            class FakeRegistry:
                def get(self, name):
                    return object()
                def available(self, name):
                    return True
            old = video_jobs._PROVIDER_REGISTRY
            video_jobs.configure(FakeRegistry())
            video_jobs._process(job_id)
            video_jobs._PROVIDER_REGISTRY = old
            with session_base() as db:
                self.assertEqual(db.get(VideoGenerationJob, job_id).status, "expired")
        finally:
            self._delete(job_id)

    def test_result_endpoint_isolated(self):
        from database import User
        job_id = "test-result-owner"
        with session_base() as db:
            owner = User(email="video-result-owner@example.invalid", password_hash="test")
            other = User(email="video-result-other@example.invalid", password_hash="test")
            db.add_all([owner, other]); db.flush()
            owner_id, other_id = owner.id, other.id
        self._insert(job_id, user_id=owner_id, status="completed", result_mime_type="video/mp4",
                     result_filename="video.mp4", result_data=b"video")
        try:
            with web.app.test_client() as client:
                with client.session_transaction() as state:
                    state["user_id"] = other_id
                self.assertEqual(client.get(f"/api/outils/jobs/{job_id}/result").status_code, 404)
                with client.session_transaction() as state:
                    state["user_id"] = owner_id
                self.assertEqual(client.get(f"/api/outils/jobs/{job_id}/result").status_code, 200)
        finally:
            self._delete(job_id)
            with session_base() as db:
                db.query(User).filter(User.id.in_([owner_id, other_id])).delete(synchronize_session=False)

    def test_reconnect_endpoint_lists_active_job_for_owner(self):
        from database import User
        job_id = "test-reconnect-list"
        with session_base() as db:
            owner = User(email="video-reconnect-owner@example.invalid", password_hash="test")
            db.add(owner); db.flush()
            owner_id = owner.id
        self._insert(job_id, user_id=owner_id, status="processing", progress=37.0, status_message="Génération en cours… 37 %")
        try:
            with web.app.test_client() as client:
                with client.session_transaction() as state:
                    state["user_id"] = owner_id
                response = client.get("/api/outils/jobs")
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertTrue(any(item["job_id"] == job_id for item in payload["jobs"]))
        finally:
            self._delete(job_id)
            with session_base() as db:
                db.query(User).filter_by(id=owner_id).delete()


if __name__ == "__main__":
    unittest.main()
