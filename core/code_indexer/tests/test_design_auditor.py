import os
import unittest
import tempfile
import json
from core.code_indexer.design_auditor import DesignAuditor

class TestDesignAuditor(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = self.test_dir.name

        # Create mock DESIGN.md
        self.design_md = """
        # Design Contract
        ## Layout
        - Panel Border Radius: 8px maximum (rounded-lg).
        - Floating Modal Radius: 12px maximum (rounded-2xl).
        ## Color
        - Accent: violet/cyan only.
        """
        with open(os.path.join(self.root, "DESIGN.md"), "w", encoding="utf-8") as f:
            f.write(self.design_md)

        # Create mock design.tokens.json
        self.design_tokens = {
            "radius": {
                "panel": 8,
                "modal": 12
            },
            "colors": {
                "appBackground": "#030712",
                "panelBackground": "#050b14",
                "accent": "#06b6d4"
            }
        }
        with open(os.path.join(self.root, "design.tokens.json"), "w", encoding="utf-8") as f:
            json.dump(self.design_tokens, f)

        # Set up a components directory
        self.components_dir = os.path.join(self.root, "src", "components")
        os.makedirs(self.components_dir, exist_ok=True)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_extract_allowed_colors(self):
        auditor = DesignAuditor(self.root)
        allowed = auditor.allowed_colors
        self.assertIn("#030712", allowed)
        self.assertIn("#050b14", allowed)
        self.assertIn("#06b6d4", allowed)
        self.assertIn("#ffffff", allowed)

    def test_get_max_radii(self):
        auditor = DesignAuditor(self.root)
        radii = auditor.max_radii
        self.assertEqual(radii["panel"], 8)
        self.assertEqual(radii["modal"], 12)

    def test_audit_file_compliance_success(self):
        # Create a perfectly compliant file
        component_content = """
        import React from 'react';
        import { FolderOpen } from '@phosphor-icons/react';
        
        export function CompliantCard() {
            return (
                <div className="p-4 bg-[#030712] rounded-lg border border-white/5">
                    <FolderOpen size={16} />
                    <h3 className="font-sans text-white">Project Title</h3>
                </div>
            );
        }
        """
        file_path = os.path.join(self.components_dir, "Compliant.tsx")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(component_content)

        auditor = DesignAuditor(self.root)
        res = auditor.audit_file(file_path)

        self.assertEqual(res["compliance_score"], 100)
        self.assertEqual(len(res["issues"]), 0)

    def test_audit_file_violations(self):
        # Create a file with multiple violations:
        # 1. Hardcoded non-token hex color (#7c3aed)
        # 2. Border radius rounded-2xl (16px) on a standard panel (limit 8px)
        # 3. Backdrop blur filter on a modal component without createPortal
        component_content = """
        import React from 'react';
        
        export function BadComponent() {
            return (
                <div className="p-6 bg-[#7c3aed] rounded-2xl border border-white/10 backdrop-blur-xl">
                    <div className="modal-content">
                        <h3>Clipping Modal Dialog</h3>
                    </div>
                </div>
            );
        }
        """
        file_path = os.path.join(self.components_dir, "BadModal.tsx")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(component_content)

        auditor = DesignAuditor(self.root)
        res = auditor.audit_file(file_path)

        self.assertLess(res["compliance_score"], 100)
        
        # Check issues list
        issue_types = [issue["type"] for issue in res["issues"]]
        self.assertIn("hardcoded_color", issue_types)
        self.assertIn("radius_violation", issue_types)
        self.assertIn("bug_vulnerability", issue_types)

if __name__ == '__main__':
    unittest.main()
