"""
Cosmos v5 — Universal SQL Aggregator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Works with ANY category — not just trade.
Uses json_extract() on typed_data for field access.
Schema-aware: knows which fields are numeric/categorical.
"""
import json
import sqlite3
from datetime import datetime, timedelta


class UniversalAggregator:
    """
    SQL-based aggregation engine for any category.
    Uses json_extract() on typed_data column.
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn

    def compute(self, category: str, operation: str,
                field: str = None, group_field: str = None,
                filters: dict = None) -> dict:
        """
        Universal aggregation entry point.

        Args:
            category: Schema category (e.g., "expense", "trade")
            operation: sum, avg, count, min, max, group_by, time_series,
                       win_rate, overview, top, worst
            field: Field to aggregate (for sum/avg/min/max)
            group_field: Field to group by (for group_by)
            filters: Optional filters dict with:
                - date_range: "today", "this_week", "this_month", "this_year"
                - date_from: ISO date string
                - date_to: ISO date string
                - Any typed_data field: exact match value

        Returns:
            Dict with type + data
        """
        filters = filters or {}

        # Build WHERE clause
        where, params = self._build_where(category, filters)

        handler = {
            "sum":         lambda: self._agg_func("SUM", category, field, where, params),
            "avg":         lambda: self._agg_func("AVG", category, field, where, params),
            "min":         lambda: self._agg_func("MIN", category, field, where, params),
            "max":         lambda: self._agg_func("MAX", category, field, where, params),
            "count":       lambda: self._count(where, params),
            "group_by":    lambda: self._group_by(category, group_field, field, where, params),
            "time_series": lambda: self._time_series(category, field, filters.get("period", "day"), where, params),
            "win_rate":    lambda: self._win_rate(where, params),
            "overview":    lambda: self._overview(category, where, params),
            "brain_overview": lambda: self._brain_overview(),
            "top":         lambda: self._top_n(category, field or "net_pnl", where, params, ascending=False),
            "worst":       lambda: self._top_n(category, field or "net_pnl", where, params, ascending=True),
        }.get(operation)

        if not handler:
            return {"type": "error", "message": f"Unknown operation: {operation}"}

        try:
            return handler()
        except Exception as e:
            return {"type": "error", "message": str(e)}

    # ─── Aggregate Functions ──────────────────────────

    def _count(self, where: str, params: list) -> dict:
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM memories_v2 WHERE {where}", params)
        return {"type": "count", "count": cursor.fetchone()[0]}

    def _agg_func(self, func: str, category: str, field: str,
                  where: str, params: list) -> dict:
        """Generic SUM/AVG/MIN/MAX on a typed_data field."""
        if not field:
            return {"type": "error", "message": "field is required for this operation"}

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                {func}(CAST(json_extract(typed_data, '$.{field}') AS REAL)),
                COUNT(*)
            FROM memories_v2
            WHERE {where}
            AND json_extract(typed_data, '$.{field}') IS NOT NULL
        """, params)

        result, count = cursor.fetchone()
        return {
            "type": func.lower(),
            "field": field,
            "category": category,
            "value": round(result, 2) if result else 0,
            "count": count,
        }

    def _group_by(self, category: str, group_field: str, agg_field: str,
                  where: str, params: list) -> dict:
        """Group by a field and aggregate another."""
        if not group_field:
            return {"type": "error", "message": "group_field is required"}

        cursor = self.conn.cursor()

        agg_select = ""
        if agg_field:
            agg_select = f""",
                COALESCE(SUM(CAST(json_extract(typed_data, '$.{agg_field}') AS REAL)), 0) as total,
                COALESCE(AVG(CAST(json_extract(typed_data, '$.{agg_field}') AS REAL)), 0) as avg_val
            """
        else:
            agg_select = ", 0 as total, 0 as avg_val"

        cursor.execute(f"""
            SELECT
                json_extract(typed_data, '$.{group_field}') as grp,
                COUNT(*) as cnt
                {agg_select}
            FROM memories_v2
            WHERE {where}
            GROUP BY grp
            ORDER BY cnt DESC
        """, params)

        rows = cursor.fetchall()
        return {
            "type": "group_by",
            "group_field": group_field,
            "agg_field": agg_field,
            "category": category,
            "data": [
                {
                    group_field: r[0] or "unknown",
                    "count": r[1],
                    "total": round(r[2], 2) if agg_field else None,
                    "avg": round(r[3], 2) if agg_field else None,
                }
                for r in rows
            ],
        }

    def _time_series(self, category: str, field: str, period: str,
                     where: str, params: list) -> dict:
        """Aggregate by time period (day/week/month/year)."""
        date_format = {
            "day":   "%Y-%m-%d",
            "week":  "%Y-W%W",
            "month": "%Y-%m",
            "year":  "%Y",
        }.get(period, "%Y-%m-%d")

        agg_select = ""
        if field:
            agg_select = f""",
                COALESCE(SUM(CAST(json_extract(typed_data, '$.{field}') AS REAL)), 0) as total
            """
        else:
            agg_select = ", COUNT(*) as total"

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                strftime('{date_format}', created_at) as period,
                COUNT(*) as cnt
                {agg_select}
            FROM memories_v2
            WHERE {where}
            GROUP BY period
            ORDER BY period
        """, params)

        rows = cursor.fetchall()
        return {
            "type": "time_series",
            "period": period,
            "field": field,
            "category": category,
            "data": [
                {"period": r[0], "count": r[1], "total": round(r[2], 2)}
                for r in rows
            ],
        }

    def _win_rate(self, where: str, params: list) -> dict:
        """Calculate win rate (trade-specific but works with any result field).

        Schema reality (verified 2026-05-07): the trade schema's `result`
        column stores 'win' / 'loss' / 'break-even' (lowercase, hyphen).
        Earlier hardcoded checks for 'WIN' / 'LOSS' / 'BREAK_EVEN' always
        returned 0 — `brain_aggregate(category='trade', operation='overview')`
        reported 0% win rate against 25 real trades, completely wrong.
        Normalize via LOWER + REPLACE so both casings + separators match.
        """
        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                COUNT(*),
                SUM(CASE WHEN LOWER(REPLACE(json_extract(typed_data, '$.result'), '_', '-')) = 'win' THEN 1 ELSE 0 END),
                SUM(CASE WHEN LOWER(REPLACE(json_extract(typed_data, '$.result'), '_', '-')) = 'loss' THEN 1 ELSE 0 END),
                SUM(CASE WHEN LOWER(REPLACE(json_extract(typed_data, '$.result'), '_', '-')) = 'break-even' THEN 1 ELSE 0 END)
            FROM memories_v2 WHERE {where}
        """, params)
        total, wins, losses, be = cursor.fetchone()
        wins = wins or 0
        losses = losses or 0
        be = be or 0
        total = total or 0
        return {
            "type": "win_rate",
            "total": total,
            "wins": wins,
            "losses": losses,
            "break_even": be,
            "win_rate_pct": round((wins / total * 100), 1) if total else 0,
        }

    def _overview(self, category: str, where: str, params: list) -> dict:
        """General overview stats for a category."""
        cursor = self.conn.cursor()

        # Count
        cursor.execute(f"SELECT COUNT(*) FROM memories_v2 WHERE {where}", params)
        count = cursor.fetchone()[0]

        # Date range
        cursor.execute(f"""
            SELECT MIN(created_at), MAX(created_at)
            FROM memories_v2 WHERE {where}
        """, params)
        date_min, date_max = cursor.fetchone()

        result = {
            "type": "overview",
            "category": category,
            "total_records": count,
            "date_range": {"from": date_min, "to": date_max},
        }

        # Add category-specific stats
        if category == "trade":
            wr = self._win_rate(where, params)
            result.update({
                "wins": wr["wins"],
                "losses": wr["losses"],
                "win_rate_pct": wr["win_rate_pct"],
            })
            # PnL stats
            cursor.execute(f"""
                SELECT
                    COALESCE(SUM(CAST(COALESCE(json_extract(typed_data, '$.pnl'), json_extract(typed_data, '$.net_pnl')) AS REAL)), 0),
                    COALESCE(AVG(CAST(COALESCE(json_extract(typed_data, '$.pnl'), json_extract(typed_data, '$.net_pnl')) AS REAL)), 0),
                    COALESCE(MAX(CAST(COALESCE(json_extract(typed_data, '$.pnl'), json_extract(typed_data, '$.net_pnl')) AS REAL)), 0),
                    COALESCE(MIN(CAST(COALESCE(json_extract(typed_data, '$.pnl'), json_extract(typed_data, '$.net_pnl')) AS REAL)), 0)
                FROM memories_v2 WHERE {where}
            """, params)
            total_pnl, avg_pnl, max_pnl, min_pnl = cursor.fetchone()
            result.update({
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
                "max_pnl": round(max_pnl, 2),
                "min_pnl": round(min_pnl, 2),
            })

        elif category == "expense":
            cursor.execute(f"""
                SELECT
                    COALESCE(SUM(CAST(json_extract(typed_data, '$.amount') AS REAL)), 0),
                    COALESCE(AVG(CAST(json_extract(typed_data, '$.amount') AS REAL)), 0)
                FROM memories_v2 WHERE {where}
            """, params)
            total_amount, avg_amount = cursor.fetchone()
            result.update({
                "total_amount": round(total_amount, 2),
                "avg_amount": round(avg_amount, 2),
            })

        return result

    def _top_n(self, category: str, field: str, where: str,
               params: list, ascending: bool = False, n: int = 5) -> dict:
        """Get top/bottom N records by a numeric field."""
        order = "ASC" if ascending else "DESC"
        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                id,
                content,
                typed_data,
                CAST(json_extract(typed_data, '$.{field}') AS REAL) as sort_val,
                created_at
            FROM memories_v2
            WHERE {where}
            AND json_extract(typed_data, '$.{field}') IS NOT NULL
            ORDER BY sort_val {order}
            LIMIT {n}
        """, params)

        rows = cursor.fetchall()
        return {
            "type": "top" if not ascending else "worst",
            "field": field,
            "category": category,
            "data": [
                {
                    "id": r[0],
                    "content_preview": (r[1] or "")[:100],
                    "typed_data": json.loads(r[2] or "{}"),
                    "value": round(r[3] or 0, 2),
                    "created_at": r[4],
                }
                for r in rows
            ],
        }

    def _brain_overview(self) -> dict:
        """Overview of the entire brain across all categories.

        Counts EVERY memory (incl. source='universal-index' indexed files)
        so "Total Memories" matches the Memory Browser + sidebar, and
        deleting files there makes this number drop (the dashboard polls
        this every few seconds).
        """
        cursor = self.conn.cursor()

        # 1. Total count
        cursor.execute("SELECT COUNT(*) FROM memories_v2")
        total_memories = cursor.fetchone()[0]

        # 2. Count by Category
        cursor.execute("""
            SELECT category, COUNT(*) as cnt
            FROM memories_v2
            GROUP BY category
            ORDER BY cnt DESC
        """)
        categories = [{"category": r[0], "count": r[1]} for r in cursor.fetchall()]

        # 3. Recent Activity (Last 14 days)
        now = datetime.now()
        start_date = (now - timedelta(days=14)).isoformat()
        cursor.execute("""
            SELECT strftime('%Y-%m-%d', created_at) as day, COUNT(*) as cnt
            FROM memories_v2
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
        """, (start_date,))
        activity = [{"date": r[0], "count": r[1]} for r in cursor.fetchall()]

        # 4. Average Importance
        cursor.execute("SELECT AVG(importance_score) FROM memories_v2")
        avg_importance = cursor.fetchone()[0] or 0.5
        
        return {
            "type": "brain_overview",
            "total_memories": total_memories,
            "avg_importance": round(avg_importance, 2),
            "categories": categories,
            "activity": activity
        }

    # ─── WHERE builder ────────────────────────────────

    def _build_where(self, category: str, filters: dict) -> tuple:
        """Build SQL WHERE clause from category + filters."""
        parts = ["category = ?"]
        params = [category]

        # Date range presets
        if filters.get("date_range"):
            now = datetime.now()
            range_map = {
                "today":      now.replace(hour=0, minute=0, second=0),
                "this_week":  now - timedelta(days=now.weekday()),
                "this_month": now.replace(day=1, hour=0, minute=0, second=0),
                "this_year":  now.replace(month=1, day=1, hour=0, minute=0, second=0),
                "last_7d":    now - timedelta(days=7),
                "last_30d":   now - timedelta(days=30),
                "last_90d":   now - timedelta(days=90),
            }
            start = range_map.get(filters["date_range"])
            if start:
                parts.append("created_at >= ?")
                params.append(start.isoformat())

        # Explicit date range
        if filters.get("date_from"):
            parts.append("created_at >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            parts.append("created_at <= ?")
            params.append(filters["date_to"])

        # Folder filter
        if filters.get("folder_id"):
            parts.append("folder_id = ?")
            params.append(filters["folder_id"])

        # Custom typed_data field filters
        for key, val in filters.items():
            if key in ("date_range", "date_from", "date_to", "folder_id", "period"):
                continue
            parts.append(f"json_extract(typed_data, '$.{key}') = ?")
            params.append(str(val))

        return " AND ".join(parts), params

    # ─── Convenience ──────────────────────────────────

    def sum(self, category: str, field: str, filters: dict = None) -> float:
        """Shortcut for sum aggregation."""
        result = self.compute(category, "sum", field=field, filters=filters)
        return result.get("value", 0)

    def avg(self, category: str, field: str, filters: dict = None) -> float:
        """Shortcut for avg aggregation."""
        result = self.compute(category, "avg", field=field, filters=filters)
        return result.get("value", 0)

    def count(self, category: str, filters: dict = None) -> int:
        """Shortcut for count."""
        result = self.compute(category, "count", filters=filters)
        return result.get("count", 0)

    def group_by(self, category: str, group_field: str,
                 agg_field: str = None, filters: dict = None) -> list:
        """Shortcut for group_by."""
        result = self.compute(
            category, "group_by",
            field=agg_field, group_field=group_field,
            filters=filters
        )
        return result.get("data", [])
