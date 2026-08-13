from __future__ import annotations

import sqlite3
from pathlib import Path

from parcel_flow.models import Receipt


class ParcelStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    command_id TEXT PRIMARY KEY,
                    parcel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_token TEXT,
                    result TEXT
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    parcel_id TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                """
            )

    def ensure_receipt(self, command_id: str, parcel_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO receipts VALUES (?, ?, 'PENDING', NULL, NULL)",
                (command_id, parcel_id),
            )
            row = connection.execute(
                "SELECT parcel_id FROM receipts WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None or row["parcel_id"] != parcel_id:
                raise ValueError("Command ID is already bound to a different parcel")

    def get_receipt(self, command_id: str) -> Receipt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT command_id, parcel_id, status, owner_token, result "
                "FROM receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return Receipt(
            row["command_id"], row["parcel_id"], row["status"], row["owner_token"], row["result"]
        )

    def claim(self, command_id: str, owner_token: str, expected_owner: str | None) -> bool:
        del expected_owner
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE receipts SET status = 'CLAIMED', owner_token = ? "
                "WHERE command_id = ? AND status != 'COMPLETED'",
                (owner_token, command_id),
            )
            return cursor.rowcount == 1

    def record_dispatch(self, command_id: str, parcel_id: str, message: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO dispatches(command_id, parcel_id, message) VALUES (?, ?, ?)",
                (command_id, parcel_id, message),
            )
            return int(cursor.lastrowid)

    def complete(self, command_id: str, owner_token: str, result: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE receipts SET status = 'COMPLETED', result = ? "
                "WHERE command_id = ? AND owner_token = ?",
                (result, command_id, owner_token),
            )

    def dispatches_for(self, command_id: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM dispatches WHERE command_id = ? ORDER BY dispatch_id",
                    (command_id,),
                )
            )
