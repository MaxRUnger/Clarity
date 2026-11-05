import unittest.mock


def _revision_row(student_id, name, due, assignment, code="", objective="", assignment_id=None, top_score="R", learning_objective_id=""):
    return {
        "student_id": student_id,
        "student_name": name,
        "revision_due": due,
        "assignment_name": assignment,
        "assignment_id": assignment if assignment_id is None else assignment_id,
        "lo_code": code,
        "lo_objective_name": objective,
        "top_score": top_score,
        "learning_objective_id": learning_objective_id,
    }


class _RemovalQuery:
    def __init__(self, log, table, rows, fail_delete, fail_insert=None, fail_update=None, fail_select=None):
        self.log = log
        self.table = table
        self.rows = list(rows)
        self.fail_delete = fail_delete
        self.fail_insert = fail_insert
        self.fail_update = fail_update
        self.fail_select = fail_select
        self.op = None
        self.filters = []

    def select(self, *args, **kwargs):
        if kwargs.get("count") == "exact" and kwargs.get("head"):
            self.op = "count"
        else:
            self.op = "select"
        return self

    def delete(self):
        self.op = "delete"
        return self

    def insert(self, rows):
        self.op = "insert"
        self.inserted = rows
        return self

    def update(self, payload):
        self.op = "update"
        self.updated = payload
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def range(self, start, end):
        return self

    def execute(self):
        if self.op == "update" and self.fail_update == self.table:
            self.log.append({
                "table": self.table,
                "op": "update-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("update failed")
        if self.op == "insert" and self.fail_insert == self.table:
            self.log.append({
                "table": self.table,
                "op": "insert-failed",
                "filters": list(self.filters),
                "rows": self.inserted,
            })
            raise RuntimeError("insert failed")
        if self.op == "delete" and self.fail_delete == self.table:
            self.log.append({
                "table": self.table,
                "op": "delete-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("delete failed")
        if self.op == "select" and self.fail_select == self.table:
            self.log.append({
                "table": self.table,
                "op": "select-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("select failed")
        entry = {
            "table": self.table,
            "op": self.op,
            "filters": list(self.filters),
        }
        if self.op == "insert":
            entry["rows"] = self.inserted
        self.log.append(entry)
        count = len(self.rows) if self.op == "count" else None
        data = self.rows if self.op == "select" else []
        return unittest.mock.MagicMock(data=data, count=count)


class _ArchiveBucket:
    def __init__(self, fail=None, download_bytes=None, missing_after=None, remove_error=None, fail_remove_path=None):
        self.calls = []
        self.events = []
        self.fail = fail
        self.payload = None
        self.objects = {}
        self.download_bytes = download_bytes
        self.built = 0
        self.missing_after = missing_after
        self.remove_error = remove_error
        self.fail_remove_path = fail_remove_path
        self.remove_count = 0

    def upload(self, path, data, options):
        self.events.append("upload")
        self.calls.append(("upload", path, data, options))
        if self.fail == "upload":
            raise RuntimeError("upload failed")
        if self.fail == "snapshot-upload" and str(path).endswith(".snapshot.json.gz"):
            raise RuntimeError("snapshot upload failed")
        self.objects[path] = data
        self.payload = data

    def download(self, path):
        self.events.append("download")
        self.calls.append(("download", path))
        if self.fail == "download":
            raise RuntimeError("download failed")
        if self.fail == "snapshot-download" and str(path).endswith(".snapshot.json.gz"):
            return b"corrupt"
        if self.download_bytes is not None:
            return self.download_bytes
        if path in self.objects:
            return self.objects[path]
        return self.payload

    def remove(self, paths):
        self.events.append("remove")
        self.calls.append(("remove", list(paths)))
        self.remove_count += 1
        if self.fail_remove_path is not None and self.fail_remove_path in list(paths):
            raise RuntimeError("remove failed")
        if self.remove_error is not None:
            raise self.remove_error
        if self.fail == "remove":
            raise RuntimeError("remove failed")
        if self.missing_after is not None and self.remove_count > self.missing_after:
            from storage3.exceptions import StorageApiError
            raise StorageApiError("Object not found", "not_found", 404)


class _ClassDetailQuery:
    def __init__(self, rows, calls, selects, table):
        self._rows = [dict(row) for row in rows]
        self._calls = calls
        self._selects = selects
        self._table = table
        self._order = None
        self._window = None

    def select(self, *args, **kwargs):
        self._selects.append((self._table, " ".join(str(arg) for arg in args)))
        return self

    def eq(self, col, val):
        if "." in col:
            head, tail = col.split(".", 1)
            self._rows = [
                row for row in self._rows
                if isinstance(row.get(head), dict) and row.get(head).get(tail) == val
            ]
        else:
            self._rows = [row for row in self._rows if row.get(col) == val]
        return self

    def in_(self, col, values):
        allowed = set(values)
        self._rows = [row for row in self._rows if row.get(col) in allowed]
        return self

    def limit(self, count):
        self._rows = self._rows[:count]
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def range(self, start, end):
        self._window = (start, end)
        return self

    def execute(self):
        self._calls.append(self._table)
        rows = list(self._rows)
        if self._order:
            column, desc = self._order
            rows.sort(
                key=lambda row: (row.get(column) is None, str(row.get(column))),
                reverse=desc,
            )
        if self._window:
            start, end = self._window
            rows = rows[start:end + 1]
        result = unittest.mock.MagicMock()
        result.data = rows
        return result


def _class_detail_client(store, calls, selects):
    admin = unittest.mock.MagicMock()

    def table(name):
        return _ClassDetailQuery(store.get(name, []), calls, selects, name)

    admin.table.side_effect = table
    return admin
