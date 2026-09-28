"""Plan, ownership, cron authentication, quota, and notification checks."""

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_COOKIE_SECURE"] = "0"

import web
from database import LibraryItem, ScheduledTask, ScheduledTaskRun, User, UserNotification, session_base


class ScheduledTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = web.app.test_client()
        with session_base() as db:
            user = User(email=f"task-{uuid.uuid4().hex}@example.invalid", password_hash="test-only",
                        subscription_level="pro")
            other = User(email=f"other-task-{uuid.uuid4().hex}@example.invalid", password_hash="test-only",
                         subscription_level="pro")
            db.add_all([user, other])
            db.flush()
            self.user_id, self.other_id = user.id, other.id
            task = ScheduledTask(user_id=user.id, instruction="Météo", frequency="quotidienne",
                                 run_time="08:00", timezone="UTC", next_run_at=datetime.utcnow()-timedelta(minutes=1))
            foreign = ScheduledTask(user_id=other.id, instruction="Secret", frequency="quotidienne",
                                    run_time="08:00", timezone="UTC", next_run_at=datetime.utcnow()+timedelta(days=30))
            db.add_all([task, foreign])
            db.flush()
            self.task_id, self.foreign_task_id = task.id, foreign.id
        with self.client.session_transaction() as state:
            state["user_id"] = self.user_id
            state["csrf_token"] = "task-token"

    def tearDown(self):
        with session_base() as db:
            db.query(UserNotification).filter(UserNotification.user_id.in_([self.user_id, self.other_id])).delete(synchronize_session=False)
            db.query(ScheduledTaskRun).filter(ScheduledTaskRun.user_id.in_([self.user_id, self.other_id])).delete(synchronize_session=False)
            db.query(ScheduledTask).filter(ScheduledTask.user_id.in_([self.user_id, self.other_id])).delete(synchronize_session=False)
            db.query(LibraryItem).filter(LibraryItem.user_id.in_([self.user_id, self.other_id])).delete(synchronize_session=False)
            db.query(User).filter(User.id.in_([self.user_id, self.other_id])).delete(synchronize_session=False)

    def test_task_page_is_user_scoped_and_create_uses_timezone(self):
        page = self.client.get("/taches-planifiees")
        self.assertEqual(page.status_code, 200)
        self.assertIn(bytes.fromhex("4dc3a974c3a96f"), page.data)
        self.assertNotIn(b"Secret", page.data)
        response = self.client.post("/taches-planifiees", data={
            "csrf_token": "task-token", "instruction": "Résumé hebdomadaire",
            "frequency": "hebdomadaire", "run_time": "09:30", "timezone": "Africa/Ouagadougou",
            "weekday": "1",
        })
        self.assertEqual(response.status_code, 200)
        with session_base() as db:
            created = db.query(ScheduledTask).filter_by(user_id=self.user_id,
                                                       instruction="Résumé hebdomadaire").one()
            self.assertEqual(created.weekday, 1)
            self.assertGreater(created.next_run_at, datetime.now(timezone.utc).replace(tzinfo=None))

    def test_cron_requires_secret_and_stores_success_in_library_with_notification(self):
        cron_client = web.app.test_client()
        self.assertEqual(cron_client.post("/internal/cron/run").status_code, 403)
        with patch.dict(os.environ, {"CRON_SECRET": "local-test-secret"}), \
             patch.object(web, "traiter_message", return_value="Rapport météo") as generate:
            response = cron_client.post("/internal/cron/run", headers={"X-Cron-Secret": "local-test-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["traite"], 1)
        generate.assert_called_once_with("Météo", [], self.user_id, "")
        with session_base() as db:
            self.assertTrue(db.query(LibraryItem).filter_by(user_id=self.user_id, type="analyse").count())
            run = db.query(ScheduledTaskRun).filter_by(task_id=self.task_id).one()
            self.assertTrue(run.success)
            self.assertEqual(db.query(UserNotification).filter_by(user_id=self.user_id).count(), 1)

    def test_three_failures_disable_task_and_notify_owner_only(self):
        cron_client = web.app.test_client()
        with patch.dict(os.environ, {"CRON_SECRET": "local-test-secret"}), \
             patch.object(web, "traiter_message", side_effect=RuntimeError("provider failure")):
            for _ in range(3):
                with session_base() as db:
                    task = db.get(ScheduledTask, self.task_id)
                    task.next_run_at = datetime.utcnow() - timedelta(minutes=1)
                response = cron_client.post("/internal/cron/run", headers={"X-Cron-Secret": "local-test-secret"})
                self.assertEqual(response.status_code, 200)
        with session_base() as db:
            task = db.get(ScheduledTask, self.task_id)
            self.assertFalse(task.active, f"failures={task.consecutive_failures}; runs={db.query(ScheduledTaskRun).filter_by(task_id=self.task_id).count()}")
            self.assertEqual(task.consecutive_failures, 3)
            self.assertEqual(db.query(ScheduledTaskRun).filter_by(task_id=self.task_id, success=False).count(), 3)
            notice = db.query(UserNotification).filter_by(user_id=self.user_id).one()
            self.assertIn("3 échecs", notice.title)
            self.assertEqual(db.query(UserNotification).filter_by(user_id=self.other_id).count(), 0)

    def test_user_cannot_change_another_users_task(self):
        response = self.client.post(
            f"/taches-planifiees/{self.foreign_task_id}/basculer",
            headers={"X-CSRF-Token": "task-token"},
        )
        self.assertEqual(response.status_code, 404)
        with session_base() as db:
            self.assertTrue(db.get(ScheduledTask, self.foreign_task_id).active)

    def test_pro_task_and_daily_execution_caps(self):
        for index in range(2):
            response = self.client.post("/taches-planifiees", data={
                "csrf_token": "task-token", "instruction": f"Extra task {index}",
                "frequency": "quotidienne", "run_time": "23:59", "timezone": "UTC",
            })
            self.assertEqual(response.status_code, 200)
        with session_base() as db:
            self.assertEqual(db.query(ScheduledTask).filter_by(user_id=self.user_id, active=True).count(), 3)
            db.add_all([ScheduledTaskRun(task_id=self.task_id, user_id=self.user_id,
                                         success=True, executed_at=datetime.utcnow())
                        for _ in range(web.TASK_PLAN_LIMITS["pro"]["daily_runs"])])
            task = db.get(ScheduledTask, self.task_id)
            task.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        with patch.dict(os.environ, {"CRON_SECRET": "local-test-secret"}), \
             patch.object(web, "traiter_message", return_value="Should not run") as generate:
            response = web.app.test_client().post(
                "/internal/cron/run", headers={"X-Cron-Secret": "local-test-secret"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["traite"], 0)
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
