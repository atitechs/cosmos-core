# 🌌 Cosmos Core (Local-First AI Memory Substrate)

Cosmos Core is the fully functional, open-source engine of Cosmos. It runs a local Model Context Protocol (MCP) server that indexes your code structure (using tree-sitter AST parsing) and matches it against your SQLite database of bug-fix lessons to supply high-quality context to AI agents (Claude Code, Cursor, Cline, Windsurf).

> [!NOTE]
> **This is the CLI-Only Core Engine.** It is fully functional as a developer tool. If you are looking for the polished macOS/Windows desktop application with the visual timeline graph, 1-click installer, and local dev server runner, visit [atitechs.com](https://atitechs.com) to download the premium app ($12/mo).

---

## 📊 Core Engine vs Premium Desktop App

| Feature | Cosmos Core (This Repo) | Cosmos Desktop App (Premium) |
|---|:---:|:---:|
| **Local-First SQLite DB & FTS5** | 🟢 Fully Functional | 🟢 Fully Functional |
| **AST Code Indexer & Call Graphs** | 🟢 Fully Functional | 🟢 Fully Functional |
| **Model Context Protocol (MCP)** | 🟢 Run via Stdio CLI | 🟢 1-Click Auto-Configuration |
| **Visual Interface & Neural Map** | ❌ None (CLI-only) | 🟢 2D PixiJS Visual Graph |
| **Interactive Timeline Planner** | ❌ None | 🟢 Drag & Drop Task Orchestrator |
| **Local Dev Server Controller** | ❌ None | 🟢 Run & Capture Subprocess Logs |
| **Auto-updater & Desktop Wizards** | ❌ None | 🟢 Easy Setup & Background Daemon |
| **License & Cloud Backups** | ❌ None | 🟢 1-Click Sync & Cloud Backup |

---

## 🚀 How to Run the Core MCP Server

This core is designed to be run as an MCP server by your AI coding assistants.

### 1. Installation
Clone this repository and install the dependencies:
```bash
pip install -r requirements-mcp.txt
```

### 2. Configure with your AI Client
To connect this core server to **Claude Desktop**, add the server configuration to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cosmos-core": {
      "command": "python",
      "args": ["-m", "core.api.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/cosmos-core"
      }
    }
  }
}
```
*(Replace `/path/to/cosmos-core` with the absolute path of this cloned directory).*

---

## 🔒 Security & Privacy by Design
Because Cosmos is built for developers, we want you to know exactly what happens to your code:
* **100% Local-first:** All AST parsing, SQLite querying, and FTS5 search run entirely on your local machine.
* **No Telemetry on Code:** No snippets, filenames, or codebase contents are ever sent to our servers.
* **Open Schema:** Your notes are stored in a standard SQLite database. You own your data.
