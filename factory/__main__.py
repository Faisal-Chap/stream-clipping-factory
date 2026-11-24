import argparse
import sys
import webbrowser
from pathlib import Path

from . import daily_release, indiepost
from .jobs import Worker, workspace_lock
from .media import PROJECT
from .settings import checks
from .store import initialize
from .web import FactoryServer


def main():
    parser = argparse.ArgumentParser(description="Stream Clipping Factory local control center")
    parser.add_argument("--data", type=Path, default=PROJECT / ".local" / "factory-local-workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--listen", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                        help="Use 0.0.0.0 inside an isolated container with a loopback-only published port")
    parser.add_argument("--open", action="store_true", help="Open the local control center in your browser")
    args = parser.parse_args()
    root = args.data.resolve()
    table = PROJECT / "docs" / "VIDEO-TABLE.md"
    try:
        with workspace_lock(root):
            initialize(root, table)
            indiepost.recover(root)
            daily_release.recover(root)
            worker = Worker(root)
            with FactoryServer(args.port, root, table, worker, listen=args.listen) as server:
                worker.start()
                print(f"GTA VI Media Factory · {server.url}", flush=True)
                for name, ready, hint in checks():
                    if not ready:
                        print(f"{name} — {hint}", flush=True)
                if args.open:
                    webbrowser.open(server.url + "/sources")
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    worker.shutdown.set()
                    worker.wake.set()
                    worker.join()
    except (OSError, RuntimeError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
