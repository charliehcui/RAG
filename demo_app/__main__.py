"""Run the local demo application."""

from __future__ import annotations

from demo_app import DemoAppServer, SeededBugs


def main() -> None:
    server = DemoAppServer(bugs=SeededBugs.from_environment(), testing=True, port=8000).start()
    print(f"Demo App running at {server.base_url}")
    try:
        assert server.thread is not None
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
