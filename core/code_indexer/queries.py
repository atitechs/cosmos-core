"""
Tree-sitter Query Strings for Cosmos Code Indexer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Defines AST queries for symbol extraction, imports, and call graph building.
Supported: Python, TypeScript, JavaScript, Rust.
"""

# ==========================================
# PYTHON
# ==========================================

PY_QUERIES = {
    # 1. Extract definitions (functions, classes)
    "definitions": """
        (function_definition
            name: (identifier) @function.name
            body: (block) @function.body) @function.def

        (class_definition
            name: (identifier) @class.name
            body: (block) @class.body) @class.def

        (decorated_definition
            (decorator) @decorator
            definition: (function_definition
                name: (identifier) @function.name
                body: (block) @function.body)) @function.def.decorated
                
        (decorated_definition
            (decorator) @decorator
            definition: (class_definition
                name: (identifier) @class.name
                body: (block) @class.body)) @class.def.decorated
    """,
    
    # 2. Extract imports (for dependency resolver)
    "imports": """
        (import_statement
            name: (dotted_name) @import.name) @import
        (import_from_statement
            module_name: (dotted_name) @import.module
            name: (dotted_name) @import.name) @import.from
        (import_from_statement
            module_name: (dotted_name) @import.module
            name: (aliased_import (dotted_name) @import.name)) @import.from.aliased
    """,
    
    # 3. Extract calls (for call graph)
    "calls": """
        (call
            function: (identifier) @call.func_name) @call
        (call
            function: (attribute
                attribute: (identifier) @call.method_name)) @call.method
        (call
            function: (attribute) @call.member_chain)
    """,
    
    # 4. Extract docstrings
    "docstring": """
        (function_definition
            body: (block
                (expression_statement
                    (string) @docstring)))
        (class_definition
            body: (block
                (expression_statement
                    (string) @docstring)))
    """
}

# ==========================================
# JAVASCRIPT
# ==========================================

JS_QUERIES = {
    "definitions": """
        (function_declaration
            name: (identifier) @function.name) @function.def
        (lexical_declaration
            (variable_declarator
                name: (identifier) @function.name
                value: (arrow_function))) @function.def.arrow
        (class_declaration
            name: (identifier) @class.name) @class.def
        (method_definition
            name: (property_identifier) @method.name) @method.def
    """,
    
    "imports": """
        (import_statement
            (import_clause (identifier) @import.name)
            source: (string) @import.source) @import
        (import_statement
            (import_clause (named_imports (import_specifier name: (identifier) @import.name)))
            source: (string) @import.source) @import.named
    """,
    
    "calls": """
        (call_expression
            function: (identifier) @call.func_name) @call
        (call_expression
            function: (member_expression
                property: (property_identifier) @call.method_name)) @call.method
        (call_expression
            function: (member_expression) @call.member_chain)
    """,
    "docstring": ""
}

# ==========================================
# TYPESCRIPT
# ==========================================

TS_QUERIES = {
    "definitions": """
        (function_declaration
            name: (identifier) @function.name) @function.def
        (lexical_declaration
            (variable_declarator
                name: (identifier) @function.name
                value: (arrow_function))) @function.def.arrow
        (class_declaration
            name: (type_identifier) @class.name) @class.def
        (method_definition
            name: (property_identifier) @method.name) @method.def
    """,
    
    "imports": """
        (import_statement
            (import_clause (identifier) @import.name)
            source: (string) @import.source) @import
        (import_statement
            (import_clause (named_imports (import_specifier name: (identifier) @import.name)))
            source: (string) @import.source) @import.named
    """,
    
    "calls": """
        (call_expression
            function: (identifier) @call.func_name) @call
        (call_expression
            function: (member_expression
                property: (property_identifier) @call.method_name)) @call.method
        (call_expression
            function: (member_expression) @call.member_chain)
    """,
    "docstring": ""
}

# ==========================================
# RUST
# ==========================================

RS_QUERIES = {
    "definitions": """
        (function_item
            name: (identifier) @function.name) @function.def
        (impl_item
            body: (declaration_list
                (function_item
                    name: (identifier) @method.name))) @method.def
        (struct_item
            name: (type_identifier) @struct.name) @struct.def
        (enum_item
            name: (type_identifier) @enum.name) @enum.def
        (trait_item
            name: (type_identifier) @trait.name) @trait.def
    """,
    
    "imports": """
        (use_declaration
            argument: (scoped_identifier
                path: (identifier) @import.path
                name: (identifier) @import.name)) @import
        (use_declaration
            argument: (identifier) @import.name) @import.simple
    """,
    
    "calls": """
        (call_expression
            function: (identifier) @call.func_name) @call
        (call_expression
            function: (field_expression
                field: (field_identifier) @call.method_name)) @call.method
        (call_expression
            function: (scoped_identifier
                name: (identifier) @call.func_name)) @call.scoped
    """,
    
    "docstring": """
        (line_comment) @comment.line
        (block_comment) @comment.block
    """
}

# ==========================================
# Mapping dictionary
# ==========================================

# ==========================================
# KOTLIN  (KMP)  — node types verified against tree-sitter-kotlin 1.1.0
# ==========================================

KOTLIN_QUERIES = {
    "definitions": """
        (function_declaration
            name: (identifier) @function.name) @function.def
        (class_declaration
            name: (identifier) @class.name) @class.def
    """,
    "imports": "",
    "calls": "",
    "docstring": "",
}

# ==========================================
# DART  (Flutter)  — node types verified against tree-sitter-language-pack dart
# (function_signature also matches methods, which wrap a function_signature)
# ==========================================

DART_QUERIES = {
    "definitions": """
        (class_definition
            name: (identifier) @class.name) @class.def
        (function_signature
            name: (identifier) @function.name) @function.def
    """,
    "imports": "",
    "calls": "",
    "docstring": "",
}

# ==========================================
# Mapping dictionary
# ==========================================

QUERIES = {
    "python": PY_QUERIES,
    "typescript": TS_QUERIES,
    "tsx": TS_QUERIES,
    "javascript": JS_QUERIES,
    "jsx": JS_QUERIES,
    "rust": RS_QUERIES,
    "kotlin": KOTLIN_QUERIES,
    "dart": DART_QUERIES,
}
