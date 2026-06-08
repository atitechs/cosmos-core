"""
BrainRetriever v3 — ครบทุกด้าน:
  1. Intent Router with multi-intent + follow-up awareness
  2. Aggregate path with date/numeric range filters
  3. Hybrid Retrieval (vector + typed_data filter)
  4. Cross-Encoder Rerank
  5. 2-hop Graph Expansion with weight scoring
  6. Adaptive Compression (intent-aware max_tokens)
  7. Multi-intent Fusion (stats + examples for WHY queries)
  8. LRU Cache for repeated queries
  9. Conversation memory for follow-ups
 10. Confidence scoring on results
"""
import json
import hashlib
from collections import OrderedDict
from core.memory.store import get_store
from core.memory.embedder import get_embedder
from core.ai.bridge import get_bridge
from core.ai.router import get_router
from core.ai.aggregator import get_aggregator
from core.ai.conversation import get_conversation


class LRUCache:
    def __init__(self, max_size=64):
        self.cache = OrderedDict()
        self.max_size = max_size

    def get(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def set(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)

    def clear(self):
        self.cache.clear()


class BrainRetriever:
    def __init__(self):
        self.store = get_store()
        self.embedder = get_embedder()
        self.bridge = get_bridge()
        self.router = get_router()
        self.aggregator = get_aggregator(self.store)
        self.conversation = get_conversation()
        self.cache = LRUCache(max_size=64)
        self._compressor = None

    @property
    def compressor(self):
        if self._compressor is None:
            from core.ai.compressor import get_compressor
            self._compressor = get_compressor()
        return self._compressor

    def retrieve(self, query, top_k=5, category=None, compress=True):
        # --- Cache check ---
        cache_key = self._cache_key(query, top_k, category, compress)
        cached = self.cache.get(cache_key)
        if cached:
            print("⚡ Cache hit")
            return cached

        # --- Detect intent ---
        intent = self.router.detect(query)

        # --- Follow-up: inherit filters from previous turn ---
        if self.conversation.is_followup(query):
            intent['filters'] = self.conversation.merge_filters(intent.get('filters', {}))
            last = self.conversation.last_intent()
            if last and intent['intent'] == 'general':
                intent['intent'] = last['intent']
                intent['agg_type'] = last.get('agg_type')
            print(f"🔁 Follow-up detected, inherited filters: {intent['filters']}")

        print(f"🎯 Intent: {intent['intent']} | filters: {intent.get('filters', {})}")

        # --- Route ---
        if intent['intent'] == 'multi':
            result = self._multi_intent(query, top_k, intent, category, compress)
        elif intent['intent'] == 'aggregate':
            result = self._aggregate_route(intent)
        else:
            result = self._semantic_route(query, top_k, intent, category, compress)

        # --- Update conversation + cache ---
        self.conversation.add(query, intent, result.get('context', '')[:200])
        self.cache.set(cache_key, result)
        return result

    # ========== ROUTES ==========

    def _aggregate_route(self, intent):
        stats = self.aggregator.compute(
            intent.get('agg_type', 'overview'),
            intent.get('filters', {}),
        )
        return {
            "context": self._format_stats(stats),
            "sources": [],
            "status": "success",
            "mode": "aggregate",
            "intent": intent['intent'],
            "stats": stats,
            "confidence": 1.0,  # SQL is always certain
        }

    def _multi_intent(self, query, top_k, intent, category, compress):
        """รัน aggregate + semantic แล้วผสมกัน (สำหรับ WHY queries หรือ stats+examples)"""
        # 1. Get stats
        stats = self.aggregator.compute(
            intent.get('agg_type', 'overview'),
            intent.get('filters', {}),
        )

        # 2. Get examples (use 'specific' style retrieval)
        sub_intent = dict(intent)
        sub_intent['intent'] = 'specific'
        sub_intent['needs_detail'] = True
        examples = self._semantic_route(query, min(top_k, 3), sub_intent, category, compress=False)

        # 3. Compose
        merged_context = (
            f"=== STATISTICS ===\n{self._format_stats(stats)}\n\n"
            f"=== EXAMPLE NOTES ===\n{examples.get('context', '(no examples)')}"
        )

        # 4. Compress if requested
        if compress and self.bridge.mode != "none":
            compressed = self.compressor.compress(
                query, [merged_context], max_tokens=700, intent='multi'
            )
            merged_context = compressed

        return {
            "context": merged_context,
            "sources": examples.get('sources', []),
            "status": "success",
            "mode": "multi",
            "intent": intent['intent'],
            "stats": stats,
            "confidence": self._confidence(examples.get('sources', [])),
        }

    def _semantic_route(self, query, top_k, intent, category, compress):
        candidates = self._hybrid_search(query, top_k, intent.get('filters', {}), category)
        if not candidates:
            return {"context": "", "sources": [], "status": "no_results",
                    "intent": intent['intent'], "confidence": 0.0}

        # Cross-encoder rerank
        if self.bridge.mode != "none":
            from core.ai.reranker import get_reranker
            print(f"📊 Reranking {len(candidates)} candidates...")
            final = get_reranker().rank(query, candidates, top_k=top_k)
        else:
            final = candidates[:top_k]

        # 2-hop graph expansion (สำหรับ summary/specific)
        if intent['intent'] in ('summary', 'specific') and final:
            related = self._expand_via_graph_2hop(
                [r['id'] for r in final[:3]], max_extra=3
            )
            final.extend(related)

        # Build context พร้อม typed_data
        raw_contexts, sources = [], []
        ids = [r['id'] for r in final]
        typed_map = self._fetch_typed_data(ids)

        for r in final:
            td = typed_map.get(r['id'], {})
            text = r.get('text', '')
            if td:
                summary_fields = {k: v for k, v in td.items() if k in (
                    'trade_date', 'time', 'action', 'result', 'net_pnl',
                    'session', 'topic', 'clean_summary', 'confidence',
                ) and v not in (None, '', 0, 0.0)}
                if summary_fields:
                    text = f"[{json.dumps(summary_fields, ensure_ascii=False)}]\n{text}"
            raw_contexts.append(text)
            sources.append({
                "id": r['id'],
                "category": r.get('category', ''),
                "score": r.get('_rerank_score', 1.0 - r.get('_distance', 1.0)),
                "via_graph": r.get('_via_graph', False),
                "hop": r.get('_hop', 1),
            })

        confidence = self._confidence(sources)

        if compress and self.bridge.mode != "none" and raw_contexts:
            max_tok = 800 if intent['needs_detail'] else 400
            compressed = self.compressor.compress(
                query, raw_contexts, max_tokens=max_tok, intent=intent['intent']
            )
            return {
                "context": compressed,
                "sources": sources,
                "status": "success",
                "mode": "compressed",
                "intent": intent['intent'],
                "confidence": confidence,
            }

        return {
            "context": "\n---\n".join(raw_contexts),
            "sources": sources,
            "status": "success",
            "mode": "raw",
            "intent": intent['intent'],
            "confidence": confidence,
        }

    # ========== HELPERS ==========

    def _hybrid_search(self, query, top_k, filters, category):
        query_vector = self.embedder.encode(query)
        table_name = "memories"
        if table_name not in self.store.vector_db.table_names():
            return []
        table = self.store.vector_db.open_table(table_name)

        # Vector search
        if category:
            results = table.search(query_vector).where(f"category = '{category}'").limit(top_k * 4).to_list()
        else:
            results = table.search(query_vector).limit(top_k * 4).to_list()

        if not results or not filters:
            return results

        # Post-filter ด้วย typed_data (ทั้ง simple + range filters)
        ids = [r['id'] for r in results]
        typed_map = self._fetch_typed_data(ids)
        filtered = []
        for r in results:
            td = typed_map.get(r['id'], {})
            if self._td_matches_filters(td, filters):
                filtered.append(r)
        return filtered or results  # fallback

    def _td_matches_filters(self, td: dict, filters: dict) -> bool:
        for key, val in filters.items():
            if key.startswith('_'):
                continue
            if isinstance(val, dict) and 'op' in val:
                # Numeric range or date range
                actual = td.get(val.get('field', key))
                if actual is None: return False
                try:
                    actual = float(actual)
                except (ValueError, TypeError):
                    return False
                op = val['op']
                if op == 'between':
                    if not (val['from'] <= actual <= val['to']): return False
                elif op == '>' and not (actual > val['val']): return False
                elif op == '<' and not (actual < val['val']): return False
                elif op == '>=' and not (actual >= val['val']): return False
                elif op == '<=' and not (actual <= val['val']): return False
            elif isinstance(val, dict) and 'from' in val and 'to' in val:
                # Date range
                actual = td.get(key, '')
                if not (val['from'] <= actual <= val['to']): return False
            else:
                if td.get(key) != val: return False
        return True

    def _expand_via_graph_2hop(self, top_ids, max_extra=3):
        """2-hop expansion: hop 1 (direct neighbors) + hop 2 (neighbors of neighbors)"""
        if not top_ids:
            return []
        seen = set(top_ids)
        hop1, hop2 = [], []

        with self.store.lock:
            cursor = self.store.sqlite_conn.cursor()
            # Hop 1
            for tid in top_ids:
                cursor.execute("""
                    SELECT target_id, weight FROM relationships
                    WHERE source_id = ? ORDER BY weight DESC LIMIT 3
                """, (tid,))
                for target_id, weight in cursor.fetchall():
                    if target_id not in seen:
                        seen.add(target_id)
                        hop1.append((target_id, weight))

            # Hop 2 (only if hop1 found enough)
            if len(hop1) < max_extra:
                for source_id, _ in hop1[:2]:
                    cursor.execute("""
                        SELECT target_id, weight FROM relationships
                        WHERE source_id = ? ORDER BY weight DESC LIMIT 2
                    """, (source_id,))
                    for target_id, weight in cursor.fetchall():
                        if target_id not in seen:
                            seen.add(target_id)
                            # Hop 2 weight is reduced (decay)
                            hop2.append((target_id, weight * 0.7))

        # Sort all by weight, take top
        all_related = sorted(hop1 + hop2, key=lambda x: x[1], reverse=True)[:max_extra]
        related_ids = [r[0] for r in all_related]
        hop_map = {r[0]: 1 for r in hop1}
        hop_map.update({r[0]: 2 for r in hop2})

        if not related_ids:
            return []
        placeholders = ','.join('?' * len(related_ids))
        with self.store.lock:
            cursor = self.store.sqlite_conn.cursor()
            cursor.execute(
                f"SELECT id, content, category FROM memories WHERE id IN ({placeholders})",
                related_ids,
            )
            return [
                {'id': r[0], 'text': r[1], 'category': r[2],
                 '_via_graph': True, '_hop': hop_map.get(r[0], 1)}
                for r in cursor.fetchall()
            ]

    def _fetch_typed_data(self, ids):
        if not ids:
            return {}
        placeholders = ','.join('?' * len(ids))
        with self.store.lock:
            cursor = self.store.sqlite_conn.cursor()
            cursor.execute(
                f"SELECT id, typed_data FROM memories WHERE id IN ({placeholders})",
                ids,
            )
            out = {}
            for row in cursor.fetchall():
                try:
                    out[row[0]] = json.loads(row[1] or '{}')
                except json.JSONDecodeError:
                    out[row[0]] = {}
            return out

    def _confidence(self, sources):
        """Return 0..1 based on source scores"""
        if not sources:
            return 0.0
        scores = [s.get('score', 0) for s in sources if not s.get('via_graph')]
        if not scores:
            return 0.5
        avg = sum(scores) / len(scores)
        return round(min(max(avg, 0.0), 1.0), 2)

    def _cache_key(self, query, top_k, category, compress):
        raw = f"{query}|{top_k}|{category}|{compress}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _format_stats(self, stats: dict) -> str:
        t = stats.get('type')
        if t == 'win_rate':
            return (f"Total: {stats['total']} | Wins: {stats['wins']} | "
                    f"Losses: {stats['losses']} | Win Rate: {stats['win_rate_pct']}%")
        if t == 'pnl_stats':
            return (f"Records: {stats['total_records']} | "
                    f"Total PnL: {stats['total_pnl']:+.2f} | Avg: {stats['avg_pnl']:+.2f} | "
                    f"Max: {stats['max_pnl']:+.2f} | Min: {stats['min_pnl']:+.2f} | "
                    f"Wins: {stats['wins']} | Losses: {stats['losses']} | "
                    f"Win Rate: {stats['win_rate_pct']}%")
        if t in ('by_session', 'by_action', 'by_result'):
            field = t.replace('by_', '')
            lines = [f"By {field}:"]
            for d in stats['data']:
                lines.append(f"  - {d[field]}: {d['count']} trades, "
                             f"{d['wins']} wins ({d['win_rate_pct']}%), "
                             f"PnL {d['total_pnl']:+.2f}, avg {d['avg_pnl']:+.2f}")
            return "\n".join(lines)
        if t == 'by_month':
            lines = ["By month:"]
            for d in stats['data']:
                lines.append(f"  - {d['month']}: {d['count']} trades, "
                             f"{d['wins']} wins ({d['win_rate_pct']}%), "
                             f"PnL {d['total_pnl']:+.2f}")
            return "\n".join(lines)
        if t in ('top', 'worst'):
            label = "Top trades" if t == 'top' else "Worst trades"
            lines = [f"{label}:"]
            for d in stats['data']:
                lines.append(f"  - {d['trade_date']} {d['session']} "
                             f"{d['action']}: {d['result']} {d['net_pnl']:+.2f}")
            return "\n".join(lines)
        if t == 'count':
            return f"Count: {stats['count']}"
        return json.dumps(stats, ensure_ascii=False, indent=2)


_retriever = None
def get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = BrainRetriever()
    return _retriever
