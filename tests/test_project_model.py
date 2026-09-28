"""Checks that project schema additions preserve existing rows and compile for PostgreSQL."""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import database
from database import ProjectFile


class ProjectModelTests(unittest.TestCase):
    def test_legacy_project_table_gets_instructions_without_losing_rows(self):
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE projects ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "name VARCHAR(100) NOT NULL, created_at TIMESTAMP NOT NULL)"
                ))
                connection.execute(text(
                    "INSERT INTO projects (id, user_id, name, created_at) "
                    "VALUES (7, 42, 'Existing project', '2026-01-01 00:00:00')"
                ))

            with patch.object(database, "engine", engine):
                database.initialiser_base()

            columns = {column["name"] for column in inspect(engine).get_columns("projects")}
            self.assertIn("instructions", columns)
            self.assertIn("project_files", inspect(engine).get_table_names())
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT id, name, instructions FROM projects WHERE id = 7"
                )).one()
            self.assertEqual(tuple(row), (7, "Existing project", ""))
        finally:
            engine.dispose()

    def test_project_file_table_has_postgresql_compatible_binary_storage(self):
        ddl = str(CreateTable(ProjectFile.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("BYTEA", ddl)
        self.assertIn("ON DELETE CASCADE", ddl)


if __name__ == "__main__":
    unittest.main()
