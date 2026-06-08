"""
Cosmos Design-Aware Extractor — Phase 5.13
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Crawls the workspace to detect UI frameworks, Tailwind configs,
and CSS variable definitions. Synthesizes a structured "Design Blueprint"
so external AIs can build visually aligned pages.
"""

import os
import re
import json
from typing import Dict, Any, List

class DesignExtractor:
    def __init__(self, workspace_root: str):
        self.root = os.path.abspath(workspace_root)

    def extract(self) -> Dict[str, Any]:
        """Perform full design and styling token extraction."""
        frameworks = self._detect_frameworks()
        css_vars = self._parse_css_variables()
        tailwind = self._parse_tailwind_config()

        # Combine results
        return {
            "frameworks": frameworks,
            "css_variables": css_vars,
            "tailwind_theme": tailwind.get("theme", {}),
            "files_found": css_vars.get("_files", []) + tailwind.get("_files", [])
        }

    def _detect_frameworks(self) -> List[str]:
        """Detect CSS frameworks and libraries in package.json."""
        libs = []
        pkg_path = os.path.join(self.root, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    
                    # Core Frameworks/Libraries
                    if "tailwindcss" in deps: libs.append("TailwindCSS")
                    if "@radix-ui/react-slot" in deps or "@radix-ui/react-primitive" in deps: libs.append("Radix UI")
                    if "lucide-react" in deps: libs.append("Lucide Icons")
                    if "daisyui" in deps: libs.append("DaisyUI")
                    if "bootstrap" in deps: libs.append("Bootstrap")
                    if "sass" in deps or "node-sass" in deps: libs.append("Sass")
                    if "styled-components" in deps: libs.append("Styled Components")
                    
                    # Detect Shadcn by common imports or standard radix-ui dependencies
                    if "@radix-ui/react-dialog" in deps and "class-variance-authority" in deps:
                        libs.append("Shadcn UI")
            except Exception:
                pass
        return libs

    def _parse_css_variables(self) -> Dict[str, Any]:
        """Parse CSS files for variable declarations, grouping by light/dark modes."""
        css_files = []
        css_variables = {
            "light": {},
            "dark": {},
            "_files": []
        }

        # Locate CSS files in the workspace (excluding node_modules, dist, etc.)
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', 'dist', 'target', '__pycache__', 'build', 'public'}]
            for file in files:
                if file.endswith(".css"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.root)
                    # Prioritize common styles / globals stylesheets
                    if any(x in file.lower() for x in ["index", "global", "app", "main", "style", "tailwind"]):
                        css_files.append((full_path, rel_path))

        # Parse each stylesheet found
        for full_path, rel_path in css_files[:5]:  # Limit to top 5 key style files to keep performance fast
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Clean comments from CSS to avoid false matching
                content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)

                # Parse CSS selector blocks: selector { rules }
                block_pattern = re.compile(r"([^{]+)\{([^}]+)\}", re.DOTALL)
                var_pattern = re.compile(r"--([\w-]+)\s*:\s*([^;]+);?")

                for block_match in block_pattern.finditer(content):
                    selector = block_match.group(1).strip()
                    rules = block_match.group(2).strip()

                    # Decide if this is a dark theme block or general light/default block
                    # e.g., .dark, [data-theme="dark"], @media (prefers-color-scheme: dark)
                    is_dark = any(x in selector.lower() for x in ["dark", "data-theme", "prefers-color-scheme"])

                    # Extract variables
                    vars_found = {}
                    for var_match in var_pattern.finditer(rules):
                        name = f"--{var_match.group(1).strip()}"
                        val = var_match.group(2).strip()
                        vars_found[name] = val

                    if vars_found:
                        target = "dark" if is_dark else "light"
                        css_variables[target].update(vars_found)
                        if rel_path not in css_variables["_files"]:
                            css_variables["_files"].append(rel_path)
            except Exception:
                pass

        return css_variables

    def _parse_tailwind_config(self) -> Dict[str, Any]:
        """Parser for tailwind.config.{js,ts,cjs,mjs} using nested block analysis."""
        config_files = [
            "tailwind.config.js", "tailwind.config.ts",
            "tailwind.config.cjs", "tailwind.config.mjs"
        ]

        found_file = None
        content = ""
        for cfg in config_files:
            p = os.path.join(self.root, cfg)
            if os.path.isfile(p):
                found_file = cfg
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        content = f.read()
                    break
                except Exception:
                    pass

        if not found_file or not content:
            return {}

        theme = {
            "colors": {},
            "spacing": {},
            "borderRadius": {},
            "fontFamily": {}
        }

        # Remove comments
        content = re.sub(r"//.*", "", content)
        content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)

        def extract_nested_block(text: str, start_pos: int) -> tuple[str, int]:
            depth = 0
            for i in range(start_pos, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        return text[start_pos:i+1], i
            return text[start_pos:], len(text)

        def extract_pairs(block_text: str) -> dict:
            pairs = {}
            pos = 0
            key_pattern = re.compile(r"['\"]?([\w-]+)['\"]?\s*:\s*")
            while pos < len(block_text):
                match = key_pattern.search(block_text, pos)
                if not match:
                    break
                key = match.group(1)
                val_start = match.end()
                
                remaining = block_text[val_start:].strip()
                if remaining.startswith("{"):
                    brace_start = block_text.find("{", val_start)
                    block_str, end_idx = extract_nested_block(block_text, brace_start)
                    pairs[key] = extract_pairs(block_str[1:-1])
                    pos = end_idx + 1
                elif remaining.startswith("["):
                    bracket_start = block_text.find("[", val_start)
                    bracket_end = block_text.find("]", bracket_start)
                    if bracket_end != -1:
                        val_str = block_text[val_start:bracket_end + 1].strip()
                        pairs[key] = val_str
                        pos = bracket_end + 1
                    else:
                        pos = val_start + 1
                else:
                    comma_idx = block_text.find(",", val_start)
                    newline_idx = block_text.find("\n", val_start)
                    
                    end_idx = len(block_text)
                    candidates = [idx for idx in [comma_idx, newline_idx] if idx != -1]
                    if candidates:
                        end_idx = min(candidates)
                        
                    val_str = block_text[val_start:end_idx].strip()
                    val_str = re.sub(r"^['\"]|['\"]$", "", val_str).strip()
                    if val_str:
                        pairs[key] = val_str
                    pos = end_idx + 1
            return pairs

        def parse_block_content(content_str: str, key_name: str) -> dict:
            pattern = re.compile(key_name + r"\s*:\s*\{")
            match = pattern.search(content_str)
            if not match:
                return {}
            start_pos = match.end() - 1
            block_str, _ = extract_nested_block(content_str, start_pos)
            return extract_pairs(block_str[1:-1])

        # Parse key blocks
        theme["colors"] = parse_block_content(content, "colors")
        theme["spacing"] = parse_block_content(content, "spacing")
        theme["borderRadius"] = parse_block_content(content, "borderRadius")
        
        # fontFamily needs to split string representations of lists
        raw_fonts = parse_block_content(content, "fontFamily")
        for k, v in raw_fonts.items():
            if isinstance(v, str):
                cleaned_v = re.sub(r"[\[\]'\"\s]", "", v).split(",")
                theme["fontFamily"][k] = [x for x in cleaned_v if x]
            else:
                theme["fontFamily"][k] = v

        return {
            "theme": theme,
            "_files": [found_file]
        }
