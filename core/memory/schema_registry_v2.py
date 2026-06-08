"""
Cosmos v5 — Universal Schema Registry
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
10+ built-in categories + user-defined custom schemas.
No AI required — pure data definitions.
"""
import json
import sqlite3
from datetime import datetime


# ═══════════════════════════════════════════════════════
# Built-in Schemas (10 categories)
# ═══════════════════════════════════════════════════════

BUILT_IN_SCHEMAS = {

    # ── Personal Finance ──────────────────────────────
    "expense": {
        "display_name": "💰 Expense",
        "fields": {
            "amount":           {"type": "float", "required": True},
            "vendor":           {"type": "str",   "required": False},
            "expense_category": {"type": "str",   "required": False,
                                 "enum": ["food", "transport", "entertainment",
                                          "health", "shopping", "bills", "other"]},
            "payment_method":   {"type": "str",   "required": False,
                                 "enum": ["cash", "card", "transfer", "wallet"]},
        },
        "aggregations": ["sum", "avg", "by_category", "by_month"],
    },

    "income": {
        "display_name": "💵 Income",
        "fields": {
            "amount": {"type": "float", "required": True},
            "source": {"type": "str",   "required": True},
        },
        "aggregations": ["sum", "avg", "by_month"],
    },

    # ── Time / Schedule ───────────────────────────────
    "calendar": {
        "display_name": "📅 Calendar",
        "fields": {
            "start_time": {"type": "datetime", "required": True},
            "end_time":   {"type": "datetime", "required": False},
            "person":     {"type": "str",      "required": False},
            "location":   {"type": "str",      "required": False},
            "agenda":     {"type": "str",      "required": False},
        },
        "aggregations": ["count", "by_month"],
    },

    "task": {
        "display_name": "✅ Task",
        "fields": {
            "due_date": {"type": "date",  "required": False},
            "priority": {"type": "str",   "required": False,
                         "enum": ["low", "medium", "high", "critical"]},
            "done":     {"type": "bool",  "default": False},
        },
        "aggregations": ["count"],
    },

    # ── Trading ───────────────────────────────────────
    "trade": {
        "display_name": "📈 Trade",
        "fields": {
            "pair":        {"type": "str",   "required": True},
            "direction":   {"type": "str",   "enum": ["long", "short"]},
            "session":     {"type": "str",   "enum": ["london", "ny", "asia"]},
            "entry_price": {"type": "float"},
            "sl_price":    {"type": "float"},
            "tp_price":    {"type": "float"},
            "result":      {"type": "str",   "enum": ["WIN", "LOSS", "BREAK_EVEN"]},
            "net_pnl":     {"type": "float"},
            "trade_date":  {"type": "date"},
            "action":      {"type": "str"},
            "lot_size":    {"type": "float"},
            "confidence":  {"type": "float", "min": 0, "max": 10},
        },
        "aggregations": ["sum", "avg", "win_rate", "by_session", "by_month",
                         "by_result", "top", "worst"],
    },

    # ── Knowledge ─────────────────────────────────────
    "reading": {
        "display_name": "📚 Reading",
        "fields": {
            "title":  {"type": "str", "required": True},
            "author": {"type": "str"},
            "source": {"type": "str"},
            "url":    {"type": "str"},
            "rating": {"type": "int", "min": 1, "max": 5},
        },
        "aggregations": ["count", "avg"],
    },

    "research": {
        "display_name": "🔬 Research",
        "fields": {
            "topic":      {"type": "str",       "required": True},
            "key_points": {"type": "list[str]"},
        },
        "aggregations": ["count"],
    },

    # ── Personal ──────────────────────────────────────
    "journal": {
        "display_name": "📔 Journal",
        "fields": {
            "mood":   {"type": "str", "enum": ["great", "good", "ok", "bad", "awful"]},
            "energy": {"type": "int", "min": 1, "max": 10},
        },
        "aggregations": ["count", "by_month"],
    },

    "recipe": {
        "display_name": "🥘 Recipe",
        "fields": {
            "servings":     {"type": "int"},
            "time_minutes": {"type": "int"},
            "difficulty":   {"type": "str", "enum": ["easy", "medium", "hard"]},
            "ingredients":  {"type": "list[str]"},
        },
        "aggregations": ["count"],
    },

    # ── Dogfooding Telemetry ──────────────────────────
    "claude_session": {
        "display_name": "🤖 Claude Code Task",
        "fields": {
            "task":            {"type": "str", "required": True},
            "tokens_input":    {"type": "int",   "default": 0},
            "tokens_output":   {"type": "int",   "default": 0},
            "tools_used":      {"type": "list[str]"},
            "files_edited":    {"type": "list[str]"},
            "compile_errors":  {"type": "int",   "default": 0},
            "retries":         {"type": "int",   "default": 0},
            "semantic_bugs":   {"type": "int",   "default": 0},
            "duration_min":    {"type": "float", "default": 0.0},
            "outcome":         {"type": "str",
                                "enum": ["success", "fixed-after-retry",
                                         "failed", "abandoned"],
                                "default": "success"},
            "model":           {"type": "str"},
            "started_at":      {"type": "datetime"},
            "ended_at":        {"type": "datetime"},
        },
        "aggregations": ["sum", "avg", "count", "by_outcome", "by_month"],
    },

    # ── Catch-all ─────────────────────────────────────
    "note": {
        "display_name": "📝 Quick Note",
        "fields": {},
        "aggregations": ["count"],
    },
}


# ═══════════════════════════════════════════════════════
# Type validators
# ═══════════════════════════════════════════════════════

def _validate_field(value, field_def):
    """Validate a single field value against its definition. Returns (ok, error)."""
    if value is None:
        if field_def.get("required"):
            return False, "required field is missing"
        return True, None

    ftype = field_def.get("type", "str")

    # Type check
    type_map = {
        "str":       str,
        "float":     (int, float),
        "int":       int,
        "bool":      bool,
        "date":      str,       # ISO date string
        "datetime":  str,       # ISO datetime string
        "list[str]": list,
    }
    expected = type_map.get(ftype, str)
    if not isinstance(value, expected):
        # Try coercion for numeric types
        if ftype == "float":
            try:
                value = float(value)
            except (ValueError, TypeError):
                return False, f"expected float, got {type(value).__name__}"
        elif ftype == "int":
            try:
                value = int(value)
            except (ValueError, TypeError):
                return False, f"expected int, got {type(value).__name__}"
        else:
            return False, f"expected {ftype}, got {type(value).__name__}"

    # Enum check
    if "enum" in field_def and value not in field_def["enum"]:
        return False, f"value '{value}' not in enum {field_def['enum']}"

    # Range check
    if "min" in field_def and value < field_def["min"]:
        return False, f"value {value} < min {field_def['min']}"
    if "max" in field_def and value > field_def["max"]:
        return False, f"value {value} > max {field_def['max']}"

    return True, None


# ═══════════════════════════════════════════════════════
# Schema Registry
# ═══════════════════════════════════════════════════════

class SchemaRegistryV2:
    """
    Manages built-in + user-defined category schemas.
    Custom schemas are persisted in SQLite.
    """

    def __init__(self, db_conn: sqlite3.Connection = None):
        self._builtin = BUILT_IN_SCHEMAS
        self._custom = {}
        self._db_conn = db_conn
        if db_conn:
            self._load_custom_schemas()

    def _load_custom_schemas(self):
        """Load user-defined schemas from SQLite."""
        try:
            cursor = self._db_conn.cursor()
            cursor.execute("SELECT name, fields, aggregations FROM custom_schemas")
            for name, fields_json, agg_json in cursor.fetchall():
                self._custom[name] = {
                    "display_name": f"🔧 {name.title()}",
                    "fields": json.loads(fields_json),
                    "aggregations": json.loads(agg_json) if agg_json else ["count"],
                    "custom": True,
                }
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet — will be created by store

    def get(self, name: str) -> dict:
        """Get schema by category name. Falls back to 'note' if unknown."""
        return self._builtin.get(name) or self._custom.get(name) or self._builtin["note"]

    def exists(self, name: str) -> bool:
        """Check if a schema category exists."""
        return name in self._builtin or name in self._custom

    def list_all(self) -> dict:
        """Return all schemas (built-in + custom)."""
        combined = dict(self._builtin)
        combined.update(self._custom)
        return combined

    def list_categories(self) -> list:
        """Return list of all category names with display info."""
        result = []
        for name, schema in self.list_all().items():
            result.append({
                "name": name,
                "display_name": schema.get("display_name", name),
                "field_count": len(schema.get("fields", {})),
                "custom": schema.get("custom", False),
            })
        return result

    def register_custom(self, name: str, fields: dict, aggregations: list = None) -> bool:
        """Register a user-defined schema. Persists to SQLite."""
        if name in self._builtin:
            raise ValueError(f"Cannot override built-in schema '{name}'")

        schema = {
            "display_name": f"🔧 {name.title()}",
            "fields": fields,
            "aggregations": aggregations or ["count"],
            "custom": True,
        }
        self._custom[name] = schema

        if self._db_conn:
            cursor = self._db_conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO custom_schemas (name, fields, aggregations, created_at)
                VALUES (?, ?, ?, ?)
            """, (name, json.dumps(fields), json.dumps(schema["aggregations"]),
                  datetime.now().isoformat()))
            self._db_conn.commit()

        return True

    def delete_custom(self, name: str) -> bool:
        """Delete a user-defined schema."""
        if name not in self._custom:
            return False
        del self._custom[name]
        if self._db_conn:
            self._db_conn.execute("DELETE FROM custom_schemas WHERE name = ?", (name,))
            self._db_conn.commit()
        return True

    def validate(self, category: str, typed_data: dict) -> tuple:
        """
        Validate typed_data against the schema for a category.
        Returns (is_valid: bool, errors: list[str])
        """
        schema = self.get(category)
        fields = schema.get("fields", {})
        errors = []

        # Check required fields
        for field_name, field_def in fields.items():
            if field_def.get("required") and field_name not in typed_data:
                errors.append(f"Missing required field: {field_name}")

        # Validate provided fields
        for field_name, value in typed_data.items():
            if field_name in fields:
                ok, err = _validate_field(value, fields[field_name])
                if not ok:
                    errors.append(f"Field '{field_name}': {err}")
            # Unknown fields are allowed (flexible schema)

        return len(errors) == 0, errors

    def get_numeric_fields(self, category: str) -> list:
        """Return list of numeric field names for a category (for aggregation)."""
        schema = self.get(category)
        return [
            name for name, fdef in schema.get("fields", {}).items()
            if fdef.get("type") in ("float", "int")
        ]

    def get_categorical_fields(self, category: str) -> list:
        """Return list of fields with enum values (for group_by)."""
        schema = self.get(category)
        return [
            name for name, fdef in schema.get("fields", {}).items()
            if "enum" in fdef
        ]

    def apply_defaults(self, category: str, typed_data: dict) -> dict:
        """Fill in default values for missing fields."""
        schema = self.get(category)
        result = dict(typed_data)
        for field_name, field_def in schema.get("fields", {}).items():
            if field_name not in result and "default" in field_def:
                result[field_name] = field_def["default"]
        return result


# ═══════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════

_registry_v2 = None

def get_registry_v2(db_conn=None):
    global _registry_v2
    if _registry_v2 is None:
        _registry_v2 = SchemaRegistryV2(db_conn)
    return _registry_v2
