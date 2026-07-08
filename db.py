"""SQLite adapter that mimics the MongoDB collection interface used by bot.py.

ponytail: flat tables + JSON blobs for nested docs. Not a general-purpose adapter.
         Only implements the methods and query shapes used by bot.py.
         If query patterns change, this needs updating.
"""
import sqlite3
import json
import re
from datetime import datetime
from typing import Any


class Collection:
    """A SQLite-backed collection that stores documents as JSON text rows.
    
    Each collection table has: id INTEGER PK, doc TEXT (JSON).
    Some collections have indexed columns for frequently-queried fields.
    """

    def __init__(self, conn: sqlite3.Connection, name: str, indexed_fields: list[str] | None = None):
        self.conn = conn
        self.name = name
        self._indexed = indexed_fields or []
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS [{self.name}] "
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, doc TEXT NOT NULL)"
        )
        for field in self._indexed:
            safe = field.replace(".", "_")
            self.conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.name}_{safe} "
                f"ON [{self.name}](json_extract(doc, '$.{field}'))"
            )
        self.conn.commit()

    def _all_docs(self) -> list[dict]:
        rows = self.conn.execute(f"SELECT doc FROM [{self.name}]").fetchall()
        return [json.loads(r[0]) for r in rows]

    def _insert_doc(self, doc: dict) -> Any:
        cur = self.conn.execute(
            f"INSERT INTO [{self.name}] (doc) VALUES (?)",
            (json.dumps(doc, default=str),)
        )
        self.conn.commit()
        return cur.lastrowid

    # --- Public API (mimicking pymongo collection) ---

    class Cursor:
        """Lazy cursor that supports .sort().limit() chaining like pymongo."""
        def __init__(self, docs: list[dict]):
            self._docs = docs
            self._sort_field = None
            self._sort_dir = 1
            self._limit_n = None

        def sort(self, field: str, direction: int = 1):
            self._sort_field = field
            self._sort_dir = direction
            return self

        def limit(self, n: int):
            self._limit_n = n
            return self

        def __iter__(self):
            docs = list(self._docs)
            if self._sort_field:
                docs.sort(
                    key=lambda d, f=self._sort_field: str(d.get(f, "")),
                    reverse=(self._sort_dir == -1)
                )
            if self._limit_n:
                docs = docs[:self._limit_n]
            return iter(docs)

        def __len__(self):
            return len(self._docs)

        def __getitem__(self, index):
            return list(self)[index]

    def find_one(self, filter: dict | None = None) -> dict | None:
        if filter is None:
            filter = {}
        docs = self._all_docs()
        for doc in docs:
            if self._matches(doc, filter):
                return doc
        return None

    def find(self, filter: dict | None = None, **kwargs) -> Cursor:
        if filter is None:
            filter = {}
        docs = self._all_docs()
        result = [doc for doc in docs if self._matches(doc, filter)]
        # Handle projection like {"partes.$": 1}
        projection = kwargs.get("projection")
        if projection:
            result = [self._apply_projection(doc, projection) for doc in result]
        return self.Cursor(result)

    def count_documents(self, filter: dict | None = None) -> int:
        if filter is None:
            filter = {}
        docs = self._all_docs()
        return sum(1 for doc in docs if self._matches(doc, filter))

    def insert_one(self, doc: dict) -> Any:
        if "_id" in doc:
            del doc["_id"]
        # Ensure ObjectId-like fields are stored as strings
        return self._insert_doc(doc)

    def insert_many(self, docs: list[dict]):
        for doc in docs:
            self.insert_one(doc)

    def update_one(self, filter: dict, update: dict):
        docs = self._all_docs()
        for i, doc in enumerate(docs):
            if self._matches(doc, filter):
                new_doc = self._apply_update(doc, update)
                row_id = i + 1  # SQLite rowid is 1-indexed
                self.conn.execute(
                    f"UPDATE [{self.name}] SET doc = ? WHERE id = ?",
                    (json.dumps(new_doc, default=str), row_id)
                )
                self.conn.commit()
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    def update_many(self, filter: dict, update: dict):
        docs = self._all_docs()
        modified = 0
        for i, doc in enumerate(docs):
            if self._matches(doc, filter):
                new_doc = self._apply_update(doc, update)
                row_id = i + 1
                self.conn.execute(
                    f"UPDATE [{self.name}] SET doc = ? WHERE id = ?",
                    (json.dumps(new_doc, default=str), row_id)
                )
                modified += 1
        self.conn.commit()
        return type("Result", (), {"modified_count": modified})()

    def aggregate(self, pipeline: list[dict]) -> list[dict]:
        docs = self._all_docs()
        for stage in pipeline:
            docs = self._apply_stage(docs, stage)
        return docs

    def delete_many(self, filter: dict | None = None):
        if filter is None:
            self.conn.execute(f"DELETE FROM [{self.name}]")
        else:
            docs = self._all_docs()
            ids_to_delete = []
            for i, doc in enumerate(docs):
                if self._matches(doc, filter):
                    ids_to_delete.append(i + 1)
            if ids_to_delete:
                placeholders = ",".join("?" for _ in ids_to_delete)
                self.conn.execute(
                    f"DELETE FROM [{self.name}] WHERE id IN ({placeholders})",
                    ids_to_delete
                )
        self.conn.commit()

    # --- Internal helpers ---

    def _matches(self, doc: dict, filter: dict) -> bool:
        for key, value in filter.items():
            if key.startswith("$"):
                continue  # skip top-level operators for now
            doc_value = self._get_nested(doc, key)
            if isinstance(value, dict):
                for op, operand in value.items():
                    if op == "$gte":
                        if not (isinstance(doc_value, (int, float, str, datetime)) and doc_value >= operand):
                            return False
                    elif op == "$gt":
                        if not (isinstance(doc_value, (int, float, str, datetime)) and doc_value > operand):
                            return False
                    elif op == "$lte":
                        if not (isinstance(doc_value, (int, float, str, datetime)) and doc_value <= operand):
                            return False
                    elif op == "$lt":
                        if not (isinstance(doc_value, (int, float, str, datetime)) and doc_value < operand):
                            return False
                    elif op == "$regex":
                        flags = 0
                        if isinstance(operand, str):
                            pattern = operand
                        elif isinstance(operand, dict) and "$options" in operand:
                            # Handle nested regex format from aggregation
                            continue  # handled in _apply_stage
                        else:
                            pattern = str(operand)
                        if not re.search(pattern, str(doc_value or ""), re.IGNORECASE if value.get("$options", "").count("i") else 0):
                            return False
                    elif op == "$in":
                        if doc_value not in operand:
                            return False
                    elif op == "$exists":
                        if operand and doc_value is None:
                            return False
                        if not operand and doc_value is not None:
                            return False
                    elif op == "$ne":
                        if doc_value == operand:
                            return False
                    else:
                        # Unknown operator, just compare
                        if doc_value != operand:
                            return False
            else:
                if doc_value != value:
                    return False
        return True

    def _get_nested(self, doc: dict, path: str):
        """Get a value by dot-notation path. If we hit a list and there are
        remaining path parts, search across all array elements (array match)."""
        parts = path.split(".")
        current = doc
        for i, part in enumerate(parts):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                remaining = parts[i:]
                return self._match_in_array(current, remaining)
            else:
                return None
            if current is None:
                return None
        return current

    def _match_in_array(self, arr: list, subpath: list[str]):
        """Search array elements for one matching the remaining subpath.
        Returns the matching element's sub-value, or None."""
        for item in arr:
            if not isinstance(item, dict):
                continue
            current = item
            match = True
            for part in subpath:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    match = False
                    break
                if current is None:
                    match = False
                    break
            if match:
                return current
        return None

    def _apply_update(self, doc: dict, update: dict) -> dict:
        new_doc = dict(doc)
        for op, fields in update.items():
            if op == "$set":
                for key, value in fields.items():
                    self._set_nested(new_doc, key, value)
            elif op == "$inc":
                for key, amount in fields.items():
                    current = self._get_nested(new_doc, key)
                    if current is None:
                        self._set_nested(new_doc, key, amount)
                    elif isinstance(current, (int, float)):
                        self._set_nested(new_doc, key, current + amount)
            elif op == "$push":
                for key, value in fields.items():
                    arr = self._get_nested(new_doc, key)
                    if arr is None:
                        self._set_nested(new_doc, key, [value])
                    elif isinstance(arr, list):
                        arr.append(value)
        return new_doc

    def _set_nested(self, doc: dict, path: str, value):
        parts = path.split(".")
        current = doc
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def _apply_projection(self, doc: dict, projection: dict) -> dict:
        result = {}
        for key, value in projection.items():
            if key.endswith(".$"):
                base_key = key[:-2]
                array = self._get_nested(doc, base_key)
                if isinstance(array, list):
                    # Return only the first element
                    self._set_nested(result, base_key, [array[0]] if array else [])
                else:
                    self._set_nested(result, base_key, None)
            elif value == 1:
                self._set_nested(result, key, self._get_nested(doc, key))
        return result

    def _apply_stage(self, docs: list[dict], stage: dict) -> list[dict]:
        if "$addFields" in stage:
            result = []
            for doc in docs:
                new_doc = dict(doc)
                for field, expr in stage["$addFields"].items():
                    new_doc[field] = self._eval_expr(doc, expr)
                result.append(new_doc)
            return result
        elif "$match" in stage:
            condition = stage["$match"]
            return [doc for doc in docs if self._matches_aggregate(doc, condition)]
        elif "$sort" in stage:
            for field, direction in stage["$sort"].items():
                docs.sort(key=lambda d, f=field: str(d.get(f, "")), reverse=(direction == -1))
            return docs
        elif "$limit" in stage:
            return docs[:stage["$limit"]]
        return docs

    def _eval_expr(self, doc: dict, expr) -> Any:
        if isinstance(expr, dict):
            if "$toLower" in expr:
                val = self._eval_expr(doc, expr["$toLower"])
                return str(val).lower() if val else ""
            if "$toUpper" in expr:
                val = self._eval_expr(doc, expr["$toUpper"])
                return str(val).upper() if val else ""
            if "$concat" in expr:
                return "".join(str(self._eval_expr(doc, e)) for e in expr["$concat"])
        if isinstance(expr, str) and expr.startswith("$"):
            return self._get_nested(doc, expr[1:])
        return expr

    def _matches_aggregate(self, doc: dict, condition: dict) -> bool:
        """Match condition after aggregation pipeline stages."""
        for key, value in condition.items():
            doc_val = self._get_nested(doc, key)
            if isinstance(value, dict):
                for op, operand in value.items():
                    if op == "$regex":
                        flags = 0
                        options = ""
                        if isinstance(operand, dict):
                            pattern = operand.get("$regex", "")
                            options = operand.get("$options", "")
                        else:
                            pattern = operand
                        if "i" in options:
                            flags = re.IGNORECASE
                        if not re.search(pattern, str(doc_val or ""), flags):
                            return False
                    else:
                        if doc_val != operand:
                            return False
            else:
                if doc_val != value:
                    return False
        return True


class DB:
    """Root database object, like pymongo.MongoClient.db"""

    def __init__(self, path: str = "kudo_tv.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

        # Define collections with indexed fields for performance
        self.usuarios = Collection(self.conn, "usuarios", ["user_id"])
        self.peliculas = Collection(self.conn, "peliculas", ["random_id", "id"])
        self.pedidos = Collection(self.conn, "pedidos", ["pedido_id", "user_id"])
        self.codigos = Collection(self.conn, "codigos", ["codigo"])
        self.codigos_regalo = Collection(self.conn, "codigos_regalo", ["codigo"])
