"""Version history of the transformation file.

`etl/transforms/order_revenue.yaml` in the repository is the baseline. The
running pipeline reads the active row of `pipeline.transform_versions`, so a
deployment (a fault injected by the scenario lab, or an approved repair) is a
new row, and the previous content stays available for rollback and review.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import psycopg

from datatracemind.config import REPO_ROOT, TRANSFORM_FILE
from datatracemind.db import connect


class NoActiveTransform(RuntimeError):
    """No transformation version is active; the pipeline cannot run."""


@dataclass(frozen=True)
class TransformVersion:
    version: int
    target_file: str
    content: str
    content_hash: str
    author: str
    message: str
    is_active: bool
    created_at: datetime


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def baseline_content() -> str:
    return (REPO_ROOT / TRANSFORM_FILE).read_text()


def register_version(
    conn: psycopg.Connection, content: str, *, author: str, message: str
) -> TransformVersion:
    """Record `content` as a new version and make it the active one."""
    conn.execute(
        "UPDATE pipeline.transform_versions SET is_active = false WHERE target_file = %s AND is_active",
        (TRANSFORM_FILE,),
    )
    row = conn.execute(
        """
        INSERT INTO pipeline.transform_versions (target_file, content, content_hash, author, message, is_active)
        VALUES (%s, %s, %s, %s, %s, true)
        RETURNING *
        """,
        (TRANSFORM_FILE, content, content_hash(content), author, message),
    ).fetchone()
    return TransformVersion(**row)


def get_active(conn: psycopg.Connection | None = None) -> TransformVersion:
    query = "SELECT * FROM pipeline.transform_versions WHERE target_file = %s AND is_active"
    if conn is None:
        with connect() as own:
            row = own.execute(query, (TRANSFORM_FILE,)).fetchone()
    else:
        row = conn.execute(query, (TRANSFORM_FILE,)).fetchone()
    if row is None:
        raise NoActiveTransform("no active transformation version; run the demo reset")
    return TransformVersion(**row)


def list_versions(conn: psycopg.Connection) -> list[TransformVersion]:
    rows = conn.execute(
        "SELECT * FROM pipeline.transform_versions WHERE target_file = %s ORDER BY version",
        (TRANSFORM_FILE,),
    ).fetchall()
    return [TransformVersion(**row) for row in rows]
