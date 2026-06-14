# 🌌 Cosmos Core (Local-First AI Memory Substrate)

[![Download Cosmos Desktop App](https://img.shields.io/badge/Download-Cosmos%20Desktop%20App-violet?style=for-the-badge&logo=apple)](https://cosmos.atitechs.com)
[![Join the Waitlist](https://img.shields.io/badge/Join-Waitlist%20%26%20Demo-cyan?style=for-the-badge)](https://cosmos.atitechs.com/#register)

Cosmos Core is the open-source engine of Cosmos. It runs a local Model Context Protocol (MCP) server that indexes your code structure (using tree-sitter AST parsing) and matches it against your SQLite database of bug-fix lessons to supply high-quality context to AI agents.

> [!IMPORTANT]
> **🚀 Looking for the full Visual Experience?**
> This repository contains the CLI-only core engine for advanced developers. If you want a **1-click automatic installer**, the gorgeous **2D/3D Neural Map (visual memory graph)**, the **Local Dev Server Controller**, and the **Interactive Timeline**, please download the Desktop App instead of manual CLI setup.
>
> 👉 **[Download Cosmos Desktop App Demo at cosmos.atitechs.com](https://cosmos.atitechs.com)**

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
