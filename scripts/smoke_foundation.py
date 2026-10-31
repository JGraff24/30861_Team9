"""Smoke the integrated system and independent tester on separate local processes."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main() -> None:
    processes: list[subprocess.Popen[bytes]] = []
    with tempfile.TemporaryDirectory(prefix="team9-smoke-") as directory:
        try:
            for module, port in (("app.main:create_app", 8000), ("tester.main:create_app", 8001)):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
                environment = {
                    "PATH": os.defpath,
                    "PYTHONPATH": str(Path.cwd()),
                    "HOME": directory,
                    "METADATA_DB_PATH": directory + "/system.sqlite3",
                    "BLOB_ROOT": directory + "/sources",
                }
                # Windows' Python launcher needs SystemRoot to start the interpreter.
                # Preserve OS runtime inputs without inheriting application credentials.
                if os.name == "nt":
                    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
                        if value := os.environ.get(name):
                            environment[name] = value
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
                        "--no-access-log",
                    ],
                    env=environment,
                    cwd=directory,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(process)
                deadline = time.monotonic() + 15
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False
                ) as client:
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f"{module} exited during startup")
                        try:
                            response = client.get("/openapi.json")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"{module} startup deadline exceeded")
                        time.sleep(0.1)
                    expected = 13 if port == 8000 else 3
                    assert len(response.json()["paths"]) == expected
                    assert response.json()["openapi"] == "3.1.1"
                    assert client.get("/docs").status_code == 200
                    if port == 8000:
                        # Concrete defaults assemble all four owner modules.
                        assert client.get("/api/v1/health").status_code == 200
                        assert client.post("/api/v1/workbooks").status_code == 401
                    assert client.get("/test/report").status_code == (404 if port == 8000 else 401)
                print(f"{module}: port {port}; docs 200; {expected} actual route paths checked")
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
