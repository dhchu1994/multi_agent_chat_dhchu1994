"""Pre-flight experiment readiness verification tool.

Usage:
    python -m app.tools.preflight [--config config/config.yaml]
"""

from __future__ import annotations

import argparse
import sys

from ..config_loader import load_config, resolve_api_key
from ..event_store import EventStore


def run_preflight(config_path: str | None = None) -> bool:
    print("=======================================================")
    print(" Interaction Portal: Pre-Flight Readiness Verification")
    print("=======================================================")
    all_ok = True

    try:
        cfg = load_config(config_path)
        print(f"[OK] Loaded config v{cfg.config_version} (sha256: {cfg.config_sha256[:12]}...)")
        print(f"[OK] Max concurrent participants configured: {cfg.max_concurrent_participants}")
    except Exception as exc:
        print(f"[FAIL] Config load failed: {exc}", file=sys.stderr)
        return False

    print(f"[OK] Conditions loaded: {len(cfg.conditions)} ({', '.join(cfg.conditions.keys())})")
    print(f"[OK] Clients loaded: {len(cfg.clients)} (randomised={cfg.randomised_clients}, fixed_last={cfg.fixed_last_client})")

    for cid, client in cfg.clients.items():
        if len(client.requirements) != 3:
            print(f"[FAIL] Client {cid} does not have exactly 3 stated requirements.")
            all_ok = False
        for s in cfg.specialists:
            pm = client.private.get(s.name)
            if not pm or not pm.hidden_items:
                print(f"[FAIL] Client {cid} specialist {s.name} is missing hidden items.")
                all_ok = False
    if all_ok:
        print("[OK] All client briefs and specialist private files verified.")

    try:
        store = EventStore(cfg.db_path)
        print(f"[OK] Database initialized and writable at: {cfg.db_path}")
    except Exception as exc:
        print(f"[FAIL] Database initialization failed: {exc}", file=sys.stderr)
        all_ok = False

    if cfg.llm.provider == "mock":
        print("[WARN] LLM provider is 'mock' (offline deterministic mode). Set PORTAL_LLM_PROVIDER for live study runs.")
    else:
        key, var_name = resolve_api_key(cfg)
        if key:
            print(f"[OK] LLM provider '{cfg.llm.provider}' (model={cfg.llm.model}) API key found in {var_name}.")
        else:
            print(f"[FAIL] LLM provider '{cfg.llm.provider}' selected, but no API key found in environment!")
            all_ok = False

    print("=======================================================")
    print(" Verdict:", "READY" if all_ok else "NOT READY")
    print("=======================================================")
    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pre-flight checks for the Interaction Portal.")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args()
    return 0 if run_preflight(args.config) else 1


if __name__ == "__main__":
    sys.exit(main())
