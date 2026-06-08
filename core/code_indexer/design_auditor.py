"""
Cosmos Design System Auditor — Phase 5.14
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Statically audits source code files against the rules in DESIGN.md
and styling variables in design.tokens.json. Detects design drifts,
hardcoded values, icon drifts, and preventable UI bug vulnerabilities.
"""

import os
import re
import json
from typing import Dict, Any, List

class DesignAuditor:
    def __init__(self, workspace_root: str):
        self.root = os.path.abspath(workspace_root)
        self.design_contract = self._load_design_contract()
        self.design_tokens = self._load_design_tokens()
        self.allowed_colors = self._extract_allowed_colors()
        self.max_radii = self._get_max_radii()

    def _load_design_contract(self) -> str:
        contract_path = os.path.join(self.root, "DESIGN.md")
        if os.path.isfile(contract_path):
            try:
                with open(contract_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return ""

    def _load_design_tokens(self) -> Dict[str, Any]:
        tokens_path = os.path.join(self.root, "design.tokens.json")
        if os.path.isfile(tokens_path):
            try:
                with open(tokens_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _extract_allowed_colors(self) -> List[str]:
        """Dynamically extract allowed colors from design.tokens.json color configurations."""
        allowed = ["#000000", "#ffffff", "#fff", "#000", "transparent"]
        colors_dict = self.design_tokens.get("colors", {})
        
        def recurse(val):
            if isinstance(val, str):
                if val.startswith("#"):
                    allowed.append(val.lower())
            elif isinstance(val, dict):
                for v in val.values():
                    recurse(v)
                    
        recurse(colors_dict)
        return list(set(allowed))

    def _get_max_radii(self) -> Dict[str, int]:
        """Dynamically load maximum radii definitions from design.tokens.json."""
        radius_tokens = self.design_tokens.get("radius", {})
        return {
            "panel": radius_tokens.get("panel", 8),
            "control": radius_tokens.get("control", 6),
            "modal": radius_tokens.get("modal", 12),
        }

    def audit_file(self, file_path: str) -> Dict[str, Any]:
        """Perform visual design contract and token audit on a specific file."""
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            return {"error": f"File not found: {file_path}"}

        rel_path = os.path.relpath(abs_path, self.root)
        
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": f"Failed to read file: {str(e)}"}

        content = "".join(lines)
        issues = []
        stats = {
            "hardcoded_colors": 0,
            "radius_violations": 0,
            "nested_cards": 0,
            "icon_drifts": 0,
            "bug_vulnerabilities": 0
        }

        # ─── 1. HARDCODED COLOR DETECTION ───
        hex_pattern = re.compile(r"#(?:[A-Fa-f0-9]{3}){1,2}\b")
        rgba_pattern = re.compile(r"\brgba?\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*[\d.]+)?\)")
        
        for idx, line in enumerate(lines, 1):
            if line.strip().startswith("//") or line.strip().startswith("*"):
                continue
                
            # Check Hex against dynamically loaded allowed_colors
            for match in hex_pattern.finditer(line):
                hex_val = match.group(0)
                if hex_val.lower() not in self.allowed_colors:
                    issues.append({
                        "line": idx,
                        "type": "hardcoded_color",
                        "severity": "warning",
                        "message": f"Hardcoded hex color '{hex_val}' found instead of token colors defined in design.tokens.json.",
                        "snippet": line.strip()
                    })
                    stats["hardcoded_colors"] += 1
            
            # Check RGB/RGBA
            for match in rgba_pattern.finditer(line):
                rgba_val = match.group(0)
                if "rgba(6, 182" not in rgba_val and "rgba(139, 92" not in rgba_val:
                    issues.append({
                        "line": idx,
                        "type": "hardcoded_color",
                        "severity": "warning",
                        "message": f"Raw CSS color function '{rgba_val}' used. Prefer HSL-tailored classes or design.tokens.json definitions.",
                        "snippet": line.strip()
                    })
                    stats["hardcoded_colors"] += 1

        # ─── 2. BORDER RADIUS CONTRACT CHECKS (DYNAMIC) ───
        tailwind_radii = {
            "rounded-sm": 2,
            "rounded": 4,
            "rounded-md": 6,
            "rounded-lg": 8,
            "rounded-xl": 12,
            "rounded-2xl": 16,
            "rounded-3xl": 24,
            "rounded-4xl": 32,
        }
        
        is_modal_file = "modal" in rel_path.lower() or "modal" in content.lower()
        max_panel = self.max_radii["panel"]
        max_modal = self.max_radii["modal"]
        
        for idx, line in enumerate(lines, 1):
            if line.strip().startswith("//") or line.strip().startswith("*"):
                continue
                
            for cls, val in tailwind_radii.items():
                if re.search(rf"\b{cls}\b", line):
                    limit = max_modal if is_modal_file else max_panel
                    if val > limit:
                        issues.append({
                            "line": idx,
                            "type": "radius_violation",
                            "severity": "error",
                            "message": f"Border radius class '{cls}' ({val}px) exceeds the {limit}px maximum boundary defined for {'modals' if is_modal_file else 'panels'} in design.tokens.json.",
                            "snippet": line.strip()
                        })
                        stats["radius_violations"] += 1
                        break  # Alert once per line

        # ─── 3. FORBIDDEN NESTED CARDS ───
        panel_pattern = re.compile(r"class(Name)?=.*?\b(glass-panel|border border-white/5)\b")
        for idx, line in enumerate(lines, 1):
            if panel_pattern.search(line):
                nested = False
                for sub_idx in range(idx, min(idx + 6, len(lines))):
                    if sub_idx != idx - 1 and panel_pattern.search(lines[sub_idx]):
                        nested = True
                        break
                if nested:
                    issues.append({
                        "line": idx,
                        "type": "nested_card",
                        "severity": "warning",
                        "message": "Potential nested card borders detected. Review layout density; do not place bordered panels inside other bordered panels.",
                        "snippet": line.strip()
                    })
                    stats["nested_cards"] += 1
                    break  # Alert once per card block

        # ─── 4. ICON DRIFT ───
        svg_pattern = re.compile(r"<svg\b[^>]*>(?!.*className)")
        for idx, line in enumerate(lines, 1):
            if svg_pattern.search(line) and "xmlns" in line and not line.strip().startswith("import") and not line.strip().startswith("*"):
                issues.append({
                    "line": idx,
                    "type": "icon_drift",
                    "severity": "warning",
                    "message": "Raw SVG without styling classes found. Prefer importing icons from '@phosphor-icons/react' or Lucide standard definitions.",
                    "snippet": line.strip()
                })
                stats["icon_drifts"] += 1

        # ─── 5. PREVENTABLE BUG VULNERABILITIES (Modal Stacking/Clipping) ───
        backdrop_filter = "backdrop-filter" in content or "backdrop-blur" in content
        has_modal = "modal" in rel_path.lower() or "Modal" in content
        uses_portal = "createPortal" in content or "portaled" in content.lower()
        
        if backdrop_filter and has_modal and not uses_portal:
            issues.append({
                "line": 1,
                "type": "bug_vulnerability",
                "severity": "error",
                "message": "Vulnerability: Component has background blurs and modal declarations but does NOT use React Portals. May result in severe border-clipping.",
                "snippet": "Vite container stacking boundary"
            })
            stats["bug_vulnerabilities"] += 1

        # ─── 6. Typography Check (Sarabun font) ───
        font_drift = False
        if "font-" in content and "font-sans" not in content and "font-mono" not in content:
            font_drift = True
            
        # Calculate Compliance Score
        deductions = (stats["hardcoded_colors"] * 5 + 
                      stats["radius_violations"] * 10 + 
                      stats["nested_cards"] * 8 + 
                      stats["icon_drifts"] * 4 + 
                      stats["bug_vulnerabilities"] * 15)
        compliance = max(0, 100 - deductions)
            
        return {
            "file": rel_path,
            "compliance_score": compliance,
            "stats": stats,
            "issues": issues,
            "font_drift": font_drift
        }

    def audit_directory(self, dir_path: str) -> Dict[str, Any]:
        """Perform visual design audits on all React and styling components in a directory."""
        abs_path = os.path.abspath(dir_path)
        if not os.path.isdir(abs_path):
            return {"error": f"Directory not found: {dir_path}"}

        results = []
        total_score = 0
        file_count = 0

        for root, dirs, files in os.walk(abs_path):
            # Prune hidden / dot folders and standard build outputs
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', 'dist', 'target', '__pycache__', 'build', 'public'}]
            for file in files:
                if file.endswith((".tsx", ".ts", ".jsx", ".js", ".css")):
                    full_p = os.path.join(root, file)
                    res = self.audit_file(full_p)
                    if "error" not in res:
                        results.append(res)
                        total_score += res["compliance_score"]
                        file_count += 1
                    
                    # Yield GIL to prevent stdio starving during deep walks
                    import time
                    time.sleep(0.005)

        avg_score = round(total_score / file_count) if file_count > 0 else 100
        
        return {
            "directory": os.path.relpath(abs_path, self.root),
            "average_compliance_score": avg_score,
            "files_audited": file_count,
            "details": results
        }
