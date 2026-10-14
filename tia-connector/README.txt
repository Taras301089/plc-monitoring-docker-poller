TIA Connector for PMM — first prototype, TIA Portal V17

Offline GRAPH inspection (does not start TIA):
  .\tia-connector\bin\TiaConnector.exe inspect "D:\Exports\UB020.xml" 30 17
Returns JSON with step actions, interlock/supervision XML trees, transition
network, next nodes and a symbolic AND/OR/NOT expression for supported
contact/OR networks. Step and transition must be directly connected.
Unsupported instructions are reported explicitly; the original network tree
is retained. No current PLC values or automatic time-monitoring inference.
The expression is descriptive; live evaluation is not implemented yet.

Regression check against the exported UB020 fixture:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tia-connector\test-inspect.ps1

Build (Windows PowerShell):
  .\tia-connector\build.ps1

Check installation without starting TIA or opening a project:
  .\tia-connector\bin\TiaConnector.exe self-test

List PLC software and blocks (opens a NEW headless TIA session):
  .\tia-connector\bin\TiaConnector.exe list "D:\Projects\UB\UB.ap17"

Export one block, using the exact PLC name returned by list:
  .\tia-connector\bin\TiaConnector.exe export "D:\Projects\UB\UB.ap17" "PLC name" "Graph_002_UB020" "D:\Exports\UB020.xml"

The output directory must exist; overwriting an existing file is rejected.
Commands emit JSON; failures exit with code 1. Output includes the resolved
project/PLC/block and export time. This time is NOT a PLC version identifier.

Prerequisites: installed/licensed TIA V17 with Openness, Windows user in
Siemens TIA Openness group (log out/in after joining). Siemens may show an
application access prompt. Do not use unattended until that is resolved.
For the first live trial use a separate complete copy of the project folder.
An already-open/locked/protected project may refuse to open; this prototype
does not attach to existing sessions, upgrade, save, compile or go online.
No PLC writes/downloads or credentials are implemented.

Scope: ordinary PLC block groups and device groups; software units are not
enumerated yet. Protected blocks may not expose their implementation.
Successful compilation/self-test does NOT prove a live TIA export works.

Integration: keep this executable on Windows next to TIA. PMM's Linux
containers cannot run the Windows Openness API. A Windows service can queue
requests and invoke it, then return XML to PMM. Before exposing it, add an
allowlisted project catalogue, authentication, serialized requests, timeout
handling and export storage. Do not expose arbitrary paths/process execution.
PMM backend/UI and MCP are not implemented in this first prototype.
