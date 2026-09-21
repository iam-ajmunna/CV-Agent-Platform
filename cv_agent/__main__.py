"""
cv_agent.__main__ — CLI entrypoint.

Usage:
    python -m cv_agent
    cv-agent          (when installed via pip)
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser


def main(argv: list[str] | None = None) -> int:
    """Run a health check or enumerate skills and print to stdout."""
    parser = ArgumentParser(
        prog="cv-agent", description="CV Engineering Agent CLI and health check"
    )
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="Enumerate external and internal skills with operational availability",
    )
    args = parser.parse_args(argv)
    from cv_agent.runtime.agent import CVAgent

    try:
        agent = CVAgent()
    except Exception as exc:  # noqa: BLE001
        print(f"CV Engineering Agent — STARTUP FAILURE\n  {exc}", file=sys.stderr)
        return 2

    if args.list_skills:
        print("CV Engineering Agent — Skill Catalog & Operational Availability\n")
        print(
            f"{'Skill ID':<42} {'Vendor':<10} {'Availability State':<24} {'Presentation':<16}"
        )
        print("-" * 94)
        for skill in agent.enumerate_skills():
            if skill.is_executable:
                presentation = "installed"
            elif skill.is_merely_known:
                presentation = "merely known"
            else:
                presentation = "unavailable"
            vendor_str = skill.vendor or "internal"
            print(
                f"{skill.skill_id!s:<42} {vendor_str:<10} {skill.availability_state.value:<24} {presentation:<16}"
            )

        return 0

    health = agent.health_check()
    lg_label = (
        f"available (v{health['langgraph_version']})"
        if health["langgraph_available"]
        else "UNAVAILABLE"
    )

    print("CV Engineering Agent — Health Check")
    print(f"  Runtime status  : {health['status'].upper()}")
    print(f"  Provider        : {health['provider']}")
    print(f"  Model           : {health['model']}")
    print(f"  Capabilities    : {health['capability_count']} registered")
    print(f"  LangGraph       : {lg_label}")

    return 0 if health["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
