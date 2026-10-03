"""Stockage relationnel pour l'interface web de Dashle."""

import os
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_URL = f"sqlite:///{(BASE_DIR / 'dashle.db').as_posix()}"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if os.environ.get("RENDER", "").lower() == "true":
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL must be configured on Render; refusing ephemeral storage.")
    if DATABASE_URL.startswith("sqlite:"):
        raise RuntimeError("Render must use the configured PostgreSQL database, not local SQLite.")
else:
    DATABASE_URL = DATABASE_URL or DEFAULT_SQLITE_URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgres://")
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgresql://")

options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    options["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **options)


SessionLocale = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    nom: Mapped[str | None] = mapped_column(String(160), nullable=True)
    palier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    acces_manuel: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"), nullable=False)
    pays: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    telephone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telephone_national: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_level: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class SubscriptionPayment(Base):
    __tablename__ = "subscription_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    reference: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    cadence: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    pays: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    moyen_paiement: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StatisticalAnalysisUsage(Base):
    __tablename__ = "statistical_analysis_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

class VoiceTranscriptionUsage(Base):
    __tablename__ = "voice_transcription_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_voice_usage_user_day"),
        UniqueConstraint("visitor_key", "usage_date", name="uq_voice_usage_visitor_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    visitor_key: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ImageGenerationUsage(Base):
    __tablename__ = "image_generation_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_image_usage_user_day"),
        UniqueConstraint("visitor_key", "usage_date", name="uq_image_usage_visitor_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    visitor_key: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    files: Mapped[list["ProjectFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectFile(Base):
    __tablename__ = "project_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    project: Mapped[Project] = relationship(back_populates="files")


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(String(12), nullable=False)
    run_time: Mapped[str] = mapped_column(String(5), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ScheduledTaskRun(Base):
    __tablename__ = "scheduled_task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserPlugin(Base):
    __tablename__ = "user_plugins"
    __table_args__ = (UniqueConstraint("user_id", "plugin", name="uq_user_plugin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    plugin: Mapped[str] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), default="Nouvelle conversation", nullable=False)
    resume: Mapped[str] = mapped_column(Text, default="", nullable=False)
    archivee: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=False)
    auteur: Mapped[str] = mapped_column(String(10), nullable=False)
    texte: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MessageFeedback(Base):
    __tablename__ = "message_feedback"
    __table_args__ = (UniqueConstraint("user_id", "message_id", name="uq_message_feedback_user_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False)
    valeur: Mapped[str] = mapped_column(String(10), nullable=False)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_preferences_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    theme: Mapped[str] = mapped_column(String(20), default="clair", nullable=False)
    voix_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    lecture_automatique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    conserver_historique: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    memoire_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("TRUE"), nullable=False)
    voix_nom: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    voix_vitesse: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    voix_tonalite: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    voix_volume: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    actif: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserMemory(Base):
    __tablename__ = "user_memories"
    __table_args__ = (UniqueConstraint("user_id", "cle", name="uq_user_memory_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    cle: Mapped[str] = mapped_column(String(100), nullable=False)
    valeur: Mapped[str] = mapped_column(Text, nullable=False)


class ConnectorCredential(Base):
    __tablename__ = "connector_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_connector_credential_user_provider"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    expiration: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ConnectorPermission(Base):
    __tablename__ = "connector_permissions"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_connector_permission_user_provider"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    access_level: Mapped[str] = mapped_column(String(30), default="read_only", nullable=False)
    resources: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    actions: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    write_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ConnectorAuditLog(Base):
    __tablename__ = "connector_audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    plugin: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    result: Mapped[str] = mapped_column(String(30), nullable=False)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ConnectorActionConfirmation(Base):
    __tablename__ = "connector_action_confirmations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    parameters: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MobileOAuthHandoff(Base):
    __tablename__ = "mobile_oauth_handoffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AdminAuditLog(Base):
    """Journal non sensible des connexions et actions administratives."""
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    section: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    details: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


def initialiser_base():
    Base.metadata.create_all(engine)
    colonnes_utilisateurs = {
        "nom": "VARCHAR(160) NULL",
        "palier": "VARCHAR(20) NULL",
        "acces_manuel": "BOOLEAN NOT NULL DEFAULT FALSE",
        "subscription_level": "VARCHAR(20) NOT NULL DEFAULT 'free'",
        "subscription_expires_at": "TIMESTAMP NULL",
        "subscription_provider": "VARCHAR(20) NULL",
        "provider_subscription_id": "VARCHAR(255) NULL",
        "is_active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "pays": "VARCHAR(2) NULL",
        "telephone": "VARCHAR(32) NULL",
        "telephone_national": "VARCHAR(32) NULL",
    }
    colonnes_existantes = {colonne["name"] for colonne in inspect(engine).get_columns("users")}
    with engine.begin() as connexion:
        for nom, definition in colonnes_utilisateurs.items():
            if nom not in colonnes_existantes:
                connexion.execute(text(f"ALTER TABLE users ADD COLUMN {nom} {definition}"))

    colonnes_paiements = {
        "pays": "VARCHAR(2) NULL",
        "moyen_paiement": "VARCHAR(30) NULL",
    }
    colonnes_existantes = {colonne["name"] for colonne in inspect(engine).get_columns("subscription_payments")}
    with engine.begin() as connexion:
        for nom, definition in colonnes_paiements.items():
            if nom not in colonnes_existantes:
                connexion.execute(text(f"ALTER TABLE subscription_payments ADD COLUMN {nom} {definition}"))

    colonnes_conversations = {
        "resume": "TEXT NOT NULL DEFAULT ''",
        "archivee": "BOOLEAN NOT NULL DEFAULT FALSE",
        "project_id": "INTEGER NULL",
    }
    colonnes_existantes = {colonne["name"] for colonne in inspect(engine).get_columns("conversations")}
    with engine.begin() as connexion:
        for nom, definition in colonnes_conversations.items():
            if nom not in colonnes_existantes:
                connexion.execute(text(f"ALTER TABLE conversations ADD COLUMN {nom} {definition}"))

    colonnes_projets = {
        "instructions": "TEXT NOT NULL DEFAULT ''",
    }
    colonnes_existantes = {colonne["name"] for colonne in inspect(engine).get_columns("projects")}
    with engine.begin() as connexion:
        for nom, definition in colonnes_projets.items():
            if nom not in colonnes_existantes:
                connexion.execute(text(f"ALTER TABLE projects ADD COLUMN {nom} {definition}"))

    colonnes_preferences = {
        "memoire_active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "voix_nom": "VARCHAR(160) NOT NULL DEFAULT ''",
        "voix_vitesse": "FLOAT NOT NULL DEFAULT 1.0",
        "voix_tonalite": "FLOAT NOT NULL DEFAULT 1.0",
        "voix_volume": "FLOAT NOT NULL DEFAULT 1.0",
    }
    colonnes_existantes = {colonne["name"] for colonne in inspect(engine).get_columns("user_preferences")}
    with engine.begin() as connexion:
        for nom, definition in colonnes_preferences.items():
            if nom not in colonnes_existantes:
                connexion.execute(text(f"ALTER TABLE user_preferences ADD COLUMN {nom} {definition}"))


@contextmanager
def session_base():
    session = SessionLocale()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()