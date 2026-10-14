"""Check two real local factory processes, then stop only the processes started here."""

import socket
import subprocess
import sys
import tempfile
import time

import httpx


def main() -> None:
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for module, port in (("app.main:create_app", 8000), ("tester.main:create_app", 8001)):
            # Refuse an occupied port before starting; do not touch an existing server.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        module,
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    stdout=log,
                    stderr=log,
                )
                processes.append(process)
                deadline = time.monotonic() + 15
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False
                ) as client:
                    while True:
                        if process.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(log.read().decode("utf-8", errors="replace"))
                        try:
                            response = client.get("/openapi.json")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"{module} startup deadline exceeded")
                        time.sleep(0.1)
                    if (
                        response.json()["paths"] != {}
                        or response.json()["openapi"] != "3.1.1"
                        or client.get("/docs").status_code != 200
                    ):
                        raise RuntimeError(f"{module} has an unexpected foundation schema/docs")
                    for path in ("/api/v1/health", "/test/setup", "/test/report"):
                        if client.get(path).status_code != 404:
                            raise RuntimeError(f"{module} unexpectedly implements {path}")
                print(f"{module}: port {port}; docs 200; paths empty; pending routes 404")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
