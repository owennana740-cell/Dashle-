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