"""Connection helper for the PostgreSQL database"""

import psycopg

from src.settings import settings

## make sure this is a percentage
MARKUP_PERCENTAGE = 15

class Database:
    """Connection helper for the PostgreSQL database"""

    def connection(self):
        """Open a connection; use as a context manager for multi-statement transactions."""
        return psycopg.connect(
            dbname=settings.db_name,
            user=settings.db_username,
            password=settings.db_password,
            port=settings.db_port,
            host=settings.db_host,
        )

    def fetchall(self, query, data=None, row_factory=None):
        """Helper to run a query and return all results"""
        with psycopg.connect(
            dbname=settings.db_name,
            user=settings.db_username,
            password=settings.db_password,
            port=settings.db_port,
            host=settings.db_host,
        ) as conn:
            with conn.cursor(row_factory=row_factory) as curs:
                curs.execute(query, data)
                return curs.fetchall()

    def fetchone(self, query, data=None, row_factory=None):
        """Helper to run a query and return one result"""
        with psycopg.connect(
            dbname=settings.db_name,
            user=settings.db_username,
            password=settings.db_password,
            port=settings.db_port,
            host=settings.db_host,
        ) as conn:
            with conn.cursor(row_factory=row_factory) as curs:
                curs.execute(query, data)
                return curs.fetchone()

    def execute(self, query, data=None) -> int:
        """Run a single statement and return the affected row count"""
        with psycopg.connect(
            dbname=settings.db_name,
            user=settings.db_username,
            password=settings.db_password,
            port=settings.db_port,
            host=settings.db_host,
        ) as conn:
            with conn.cursor() as curs:
                curs.execute(query, data)
                return curs.rowcount

    def batch_execute(self, sql, data):
        """
        Helper to run multiple queries in a row
        NOTE: this is not efficient, at some point it may be worth
        updating to the better batch execute code
        """
        with psycopg.connect(
            dbname=settings.db_name,
            user=settings.db_username,
            password=settings.db_password,
            port=settings.db_port,
            host=settings.db_host,
        ) as conn:
            with conn.cursor() as curs:
                curs.executemany(sql, data)
