"""Compare two Clarity schema dumps and print every difference."""

import csv
import io
import json
import sys
from pathlib import Path


def load_dump(path):
    """Load a plain JSON dump or a Supabase CSV export of one dump cell."""
    text = Path(path).read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(text)
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] == ["dump"] and len(rows) >= 2 and len(rows[1]) == 1:
        return json.loads(rows[1][0])
    if len(rows) == 1 and len(rows[0]) == 1:
        return json.loads(rows[0][0])
    raise ValueError("unrecognized schema dump: %s" % path)


def _rows(value):
    if not value:
        return []
    return list(value)


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _only(section, left_items, right_items):
    left_set = {_canon(item) for item in left_items}
    right_set = {_canon(item) for item in right_items}
    found = []
    for item in sorted(left_set - right_set):
        found.append("%s only on left: %s" % (section, item))
    for item in sorted(right_set - left_set):
        found.append("%s only on right: %s" % (section, item))
    return found


def _columns(dump):
    grouped = {}
    for column in _rows(dump.get("columns")):
        grouped.setdefault(column["table"], []).append(column)
    ordered = {}
    for table, columns in grouped.items():
        columns = sorted(columns, key=lambda item: (item["position"], item["column"]))
        ordered[table] = [
            (item["column"], item["udt"], item["type"], item["nullable"], item["default"])
            for item in columns
        ]
    return ordered


def _column_findings(left, right):
    found = []
    left_columns = _columns(left)
    right_columns = _columns(right)
    for table in sorted(set(left_columns) | set(right_columns)):
        left_rows = left_columns.get(table)
        right_rows = right_columns.get(table)
        if left_rows is None:
            found.append("columns %s missing on left" % table)
            continue
        if right_rows is None:
            found.append("columns %s missing on right" % table)
            continue
        left_names = [item[0] for item in left_rows]
        right_names = [item[0] for item in right_rows]
        for name in left_names:
            if name not in right_names:
                found.append("columns %s.%s missing on right" % (table, name))
        for name in right_names:
            if name not in left_names:
                found.append("columns %s.%s missing on left" % (table, name))
        shared_left = [name for name in left_names if name in right_names]
        shared_right = [name for name in right_names if name in left_names]
        if shared_left != shared_right:
            found.append(
                "columns %s order left=%s right=%s"
                % (table, ",".join(shared_left), ",".join(shared_right))
            )
        right_by_name = {item[0]: item for item in right_rows}
        for name, udt, col_type, nullable, default in left_rows:
            other = right_by_name.get(name)
            if other is None:
                continue
            if udt != other[1]:
                found.append(
                    "columns %s.%s udt left=%s right=%s" % (table, name, udt, other[1])
                )
            if col_type != other[2]:
                found.append(
                    "columns %s.%s type left=%s right=%s" % (table, name, col_type, other[2])
                )
            if nullable != other[3]:
                found.append(
                    "columns %s.%s nullable left=%s right=%s"
                    % (table, name, nullable, other[3])
                )
            if default != other[4]:
                found.append(
                    "columns %s.%s default left=%s right=%s"
                    % (table, name, default, other[4])
                )
    return found


def _functions(dump):
    found = []
    for item in _rows(dump.get("functions")):
        found.append(
            (
                item["function"],
                _newlines(item["definition"]),
                bool(item["security_definer"]),
            )
        )
    return found


def _triggers(dump):
    found = []
    for item in _rows(dump.get("table_triggers")):
        found.append((item["name"], item["table"], _newlines(item["definition"])))
    for item in _rows(dump.get("auth_user_triggers")):
        found.append((item["name"], "auth.users", _newlines(item["definition"])))
    return found


def _buckets(dump):
    found = []
    for item in _rows(dump.get("storage_buckets")):
        mimes = tuple(sorted(item.get("allowed_mime_types") or []))
        found.append((item["id"], bool(item["public"]), item["file_size_limit"], mimes))
    return found


def _nonempty(section, dump, key):
    rows = _rows(dump.get(key))
    if not rows:
        return []
    return ["%s is not empty: %s" % (section, _canon(row)) for row in rows]


def differences(left, right):
    """Return one line for each schema difference. Empty means the dumps agree."""
    found = []
    found.extend(
        _only(
            "tables",
            [(item["table"], item["rls"]) for item in _rows(left.get("tables"))],
            [(item["table"], item["rls"]) for item in _rows(right.get("tables"))],
        )
    )
    found.extend(_column_findings(left, right))
    found.extend(_only("constraints", _rows(left.get("constraints")), _rows(right.get("constraints"))))
    found.extend(_only("indexes", _rows(left.get("indexes")), _rows(right.get("indexes"))))
    found.extend(_only("triggers", _triggers(left), _triggers(right)))
    found.extend(_only("functions", _functions(left), _functions(right)))
    for key, label in (
        ("policies", "policies"),
        ("table_grants", "table_grants"),
        ("function_grants", "function_grants"),
        ("storage_policies", "storage_policies"),
    ):
        found.extend(_nonempty(label, left, key))
        found.extend(_nonempty(label, right, key))
    found.extend(_only("storage_buckets", _buckets(left), _buckets(right)))
    found.extend(_only("extensions", _rows(left.get("extensions")), _rows(right.get("extensions"))))
    found.extend(
        _only(
            "default_privileges",
            _rows(left.get("default_privileges")),
            _rows(right.get("default_privileges")),
        )
    )
    return sorted(found)


def main(argv):
    if len(argv) != 3:
        print("usage: compare_schema_dumps.py LEFT RIGHT", file=sys.stderr)
        return 2
    found = differences(load_dump(argv[1]), load_dump(argv[2]))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
