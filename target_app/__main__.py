"""Run the mock bank app: python -m target_app [--port 5001] [--fault NAME]"""

import argparse

from dotenv import load_dotenv

from target_app.app import FAULTS, create_app


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="LegacyCore mock bank app")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--fault", choices=sorted(FAULTS), help="start with a fault injected")
    args = parser.parse_args()

    app = create_app(fault=args.fault)
    print(f"LegacyCore running on http://localhost:{args.port}  (fault={app.config['FAULT']})")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
