import os
import unittest
import tempfile
import json
from core.code_indexer.design_extractor import DesignExtractor

class TestDesignExtractor(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = self.test_dir.name

    def tearDown(self):
        self.test_dir.cleanup()

    def test_detect_frameworks(self):
        # Create mock package.json
        pkg_data = {
            "dependencies": {
                "react": "^18.2.0",
                "tailwindcss": "^3.3.0",
                "@radix-ui/react-dialog": "^1.0.0",
                "class-variance-authority": "^0.7.0"
            },
            "devDependencies": {
                "lucide-react": "^0.290.0"
            }
        }
        with open(os.path.join(self.root, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg_data, f)

        extractor = DesignExtractor(self.root)
        frameworks = extractor._detect_frameworks()
        
        self.assertIn("TailwindCSS", frameworks)
        self.assertIn("Shadcn UI", frameworks)
        self.assertIn("Lucide Icons", frameworks)

    def test_parse_css_variables(self):
        # Create mock CSS file
        css_content = """
        :root {
          --background: 0 0% 100%;
          --foreground: 222.2 84% 4.9%;
          --primary: 221.2 83.2% 53.3%;
          --radius: 0.5rem;
        }
        
        .dark {
          --background: 222.2 84% 4.9%;
          --foreground: 210 40% 98%;
          --primary: 217.2 91.2% 59.8%;
        }
        
        body {
          margin: 0;
          padding: 0;
        }
        """
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        with open(os.path.join(self.root, "src", "index.css"), "w", encoding="utf-8") as f:
            f.write(css_content)

        extractor = DesignExtractor(self.root)
        css_vars = extractor._parse_css_variables()

        # Check light theme variables
        self.assertEqual(css_vars["light"]["--background"], "0 0% 100%")
        self.assertEqual(css_vars["light"]["--primary"], "221.2 83.2% 53.3%")
        self.assertEqual(css_vars["light"]["--radius"], "0.5rem")

        # Check dark theme variables
        self.assertEqual(css_vars["dark"]["--background"], "222.2 84% 4.9%")
        self.assertEqual(css_vars["dark"]["--primary"], "217.2 91.2% 59.8%")
        
        # Check files referenced
        self.assertIn("src/index.css", css_vars["_files"])

    def test_parse_tailwind_config(self):
        # Create mock tailwind config file
        tailwind_content = """
        import type { Config } from 'tailwindcss'

        const config: Config = {
          content: [
            './src/**/*.{js,ts,jsx,tsx}',
          ],
          theme: {
            extend: {
              colors: {
                border: 'hsl(var(--border))',
                input: 'hsl(var(--input))',
                ring: 'hsl(var(--ring))',
                background: 'hsl(var(--background))',
                primary: {
                  DEFAULT: 'hsl(var(--primary))',
                  foreground: 'hsl(var(--primary-foreground))'
                }
              },
              borderRadius: {
                lg: 'var(--radius)',
                md: 'calc(var(--radius) - 2px)',
                sm: 'calc(var(--radius) - 4px)'
              },
              spacing: {
                '128': '32rem',
                '144': '36rem'
              },
              fontFamily: {
                sans: ['Inter', 'sans-serif'],
                mono: ['Fira Code', 'monospace']
              }
            }
          },
          plugins: [],
        }
        export default config
        """
        with open(os.path.join(self.root, "tailwind.config.ts"), "w", encoding="utf-8") as f:
            f.write(tailwind_content)

        extractor = DesignExtractor(self.root)
        tailwind_theme = extractor._parse_tailwind_config()["theme"]

        # Check parsed colors
        self.assertEqual(tailwind_theme["colors"]["border"], "hsl(var(--border))")
        self.assertEqual(tailwind_theme["colors"]["primary"]["DEFAULT"], "hsl(var(--primary))")
        
        # Check border radius
        self.assertEqual(tailwind_theme["borderRadius"]["lg"], "var(--radius)")
        
        # Check spacing
        self.assertEqual(tailwind_theme["spacing"]["128"], "32rem")
        
        # Check fonts
        self.assertEqual(tailwind_theme["fontFamily"]["sans"], ["Inter", "sans-serif"])

    def test_full_extract(self):
        # Create package.json, tailwind.config.ts, and index.css
        pkg_data = {"dependencies": {"tailwindcss": "latest"}}
        with open(os.path.join(self.root, "package.json"), "w") as f:
            json.dump(pkg_data, f)
            
        with open(os.path.join(self.root, "tailwind.config.js"), "w") as f:
            f.write("module.exports = { theme: { extend: { colors: { primary: '#ff0000' } } } }")
            
        with open(os.path.join(self.root, "global.css"), "w") as f:
            f.write(":root { --text-color: #333; }")

        extractor = DesignExtractor(self.root)
        res = extractor.extract()

        self.assertIn("TailwindCSS", res["frameworks"])
        self.assertEqual(res["css_variables"]["light"]["--text-color"], "#333")
        self.assertEqual(res["tailwind_theme"]["colors"]["primary"], "#ff0000")
        self.assertIn("tailwind.config.js", res["files_found"])
        self.assertIn("global.css", res["files_found"])

if __name__ == '__main__':
    unittest.main()
