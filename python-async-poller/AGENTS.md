# AGENTS.md — Industrial Automation & Web Development (N8N SQLCursor)

## Purpose
This file is the primary operating manual for AI agents working in this repository. It defines the tech stack, strict architectural constraints, data safety protocols, naming conventions, and interaction rules.

---

## Tech Stack & Core Responsibilities
- **Infrastructure & Deployment:** Docker & Docker Compose for containerization and service management (databases, N8N, Grafana, etc.).
- **PLCs & Industrial Hardware:** Siemens SIMATIC S7-1500 programming and configuration (TIA Portal, SCL, IEC 61131-3, optimized DBs, OPC UA, MQTT, Modbus protocols).
- **Integration & Backend:** N8N automation workflows, database design, SQL query writing and optimization.
- **Monitoring & Visualization:** Grafana dashboards, charts, and telemetry visualization.
- **Frontend Development:** Web interfaces and control panels for system monitoring.
- **Notifications & Alerts:** Integration with Telegram and WhatsApp via APIs, bots, or N8N nodes.

---

## Anti-Hallucination Rule
- Never invent technical details, library functions, register addresses, or syntax if they are missing from the context.
- If data is insufficient, explicitly state: *"I do not know the exact answer to this question / these data are not present in the provided materials."*

---

## Step-by-Step Execution Mode
1. **One step at a time:** Never output a multi-step plan all at once or jump to subsequent tasks.
2. **Await confirmation:** After providing instructions or code for the current step, pause and wait for user confirmation or status update before proceeding.

---

## Naming Consistency & Verification
- Propose names for new tables, columns, files, or classes and provide a brief justification before writing production code.
- Ensure strict adherence to the project's standardized dictionary (e.g., `connection_status`, `plcs`).

---

## Data Safety & Persistence
1. **Ban on Destructive Commands:** Strictly prohibit destructive commands such as `docker compose down -v`, `docker volume rm`, `docker system prune -a --volumes`, or `rm -rf` on data directories unless explicitly requested with a full system reset warning.
2. **Safe Container Management:** Use only safe commands:
   - `docker compose up -d`
   - `docker compose restart [service_name]`
   - `docker compose up -d --no-deps --build [service_name]`
3. **Bind Mounts:** Ensure all configurations bind data to local project directories (e.g., `./n8n_data:/home/node/.n8n`) rather than anonymous or named Docker volumes.
4. **Backups:** Remind users to export N8N workflows (`.json`) and back up databases before restructuring changes.

---

## Formatting Rules (Code Output)
- Never output an entire file or program unless explicitly requested.
- Always display only changed or new lines of code.
- Provide exact context (surrounding lines or function/class names) to clarify insertion placement.
- Use short code blocks (diff or "Find and Replace" fragments).
