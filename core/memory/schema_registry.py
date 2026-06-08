class SchemaRegistry:
    def __init__(self):
        self.schemas = {
            "trade": {
                "fields": {
                    "pair": {"type": "str", "required": True},
                    "direction": {"type": "str", "required": True, "enum": ["long", "short"]},
                    "entry_price": {"type": "float", "required": False},
                    "rr_ratio": {"type": "float", "required": False},
                    "outcome": {"type": "str", "required": False, "enum": ["win", "loss", "neutral"]}
                }
            },
            "meeting": {
                "fields": {
                    "attendees": {"type": "list", "required": False},
                    "action_items": {"type": "list", "required": False},
                    "deadline": {"type": "str", "required": False}
                }
            },
            "task": {
                "fields": {
                    "priority": {"type": "str", "required": False, "enum": ["low", "medium", "high", "critical"]},
                    "due_date": {"type": "str", "required": False}
                }
            },
            "note": {
                "fields": {}
            }
        }

    def get_schema(self, category):
        return self.schemas.get(category, self.schemas["note"])

_registry = None

def get_registry():
    global _registry
    if _registry is None:
        _registry = SchemaRegistry()
    return _registry
