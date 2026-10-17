"""Export actual system and tester routes with the shared DTO catalog."""

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from app.config import SystemSettings
from app.contracts.models import (
    AuditResponse,
    HealthResponse,
    ProjectError,
    RunRequest,
    SetupResponse,
    SuiteReport,
    SystemReport,
    SystemSetupRequest,
    TestEndpointError,
    TesterSetupRequest,
    UploadResponse,
    VersionResponse,
)
from app.main import create_app as create_system
from tester.config import TesterSettings
from tester.main import create_app as create_tester

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "docs" / "contracts"


def schema_documents() -> dict[str, dict[str, object]]:
    system_models = (
        HealthResponse,
        UploadResponse,
        VersionResponse,
        AuditResponse,
        ProjectError,
        SystemSetupRequest,
        SetupResponse,
        SystemReport,
        TestEndpointError,
    )
    tester_models = (TesterSetupRequest, SetupResponse, RunRequest, SuiteReport, TestEndpointError)
    documents = {}
    for name, application, models in (
        # Catalog generation uses documented local defaults, independent of private env inputs.
        (
            "system",
            create_system(SystemSettings.model_construct(enable_test_interface=True)),
            system_models,
        ),
        ("tester", create_tester(TesterSettings.model_construct()), tester_models),
    ):
        document = application.openapi()
        _, schemas = models_json_schema(
            [(model, "serialization") for model in models],
            ref_template="#/components/schemas/{model}",
        )
        document.setdefault("components", {}).setdefault("schemas", {}).update(schemas["$defs"])
        document["x-foundation-status"] = (
            "Integrated Phase 3 project and system testing routes delivered"
            if name == "system"
            else "Independent tester setup/run/report delivered"
        )
        document["x-team9-contract-profile"] = "1.0.0"
        document["x-instructor-interface-version"] = "0.4.2"
        documents[name] = document
    return documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on stale tracked catalogs")
    args = parser.parse_args()
    for name, document in schema_documents().items():
        path = CONTRACT_DIR / f"{name}.openapi.json"
        expected = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                raise SystemExit(f"stale schema catalog: {path}")
        else:
            path.write_text(expected, encoding="utf-8")
        print(f"{name}: {'checked' if args.check else 'exported'} schema catalog")


if __name__ == "__main__":
    main()
