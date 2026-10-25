"""HCM Lite — AI-assisted Human Capital Management (Saylani Hackathon, Oct 2026).

Layered architecture:  routes (HTTP)  ->  services (business rules + RBAC)  ->  data (SQLAlchemy).
The AI assistant calls the *same* services through a tool registry, so its answers
always match the dashboards.
"""

__version__ = "1.0.0"
