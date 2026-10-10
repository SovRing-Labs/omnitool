"""TARSUS-0 C5: CycloneDX SBOM from the MCP lockfile (stdlib generator, same output locally and in CI)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "bin" / "fleet-exec").exists())
LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "tarsus-mcp-server", "dependencies": {"@modelcontextprotocol/sdk": "^1.30.0"}},
        "node_modules/@modelcontextprotocol/sdk": {"version": "1.32.1", "license": "MIT",
                                                   "integrity": "sha512-" + "A" * 86 + "=="},
        "node_modules/zod": {"version": "3.25.0", "license": "MIT"},
        "node_modules/fsevents": {"version": "2.3.3", "license": "MIT", "optional": True},
    },
}


def sbom(tmp_path, *extra):
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps(LOCK))
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "tarsus-sbom"), "--lock", str(lock), "--version", "9.9.9", *extra],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_cyclonedx_shape_and_root_component(tmp_path):
    b = sbom(tmp_path)
    assert b["bomFormat"] == "CycloneDX" and b["specVersion"] == "1.5"
    root = b["metadata"]["component"]
    assert root["name"] == "tarsus" and root["version"] == "9.9.9"
    assert "PolyForm" in root["licenses"][0]["expression"]


def test_npm_components_versions_licences_purls_hashes(tmp_path):
    comps = {c["name"]: c for c in sbom(tmp_path)["components"]}
    sdk = comps["@modelcontextprotocol/sdk"]
    assert sdk["version"] == "1.32.1" and sdk["licenses"] == [{"expression": "MIT"}]
    assert sdk["purl"] == "pkg:npm/%40modelcontextprotocol/sdk@1.32.1"
    assert sdk["hashes"][0]["alg"] == "SHA-512" and len(sdk["hashes"][0]["content"]) == 128
    assert comps["fsevents"]["scope"] == "optional" and comps["zod"]["scope"] == "required"


def test_external_tools_and_optional_python_listed(tmp_path):
    comps = {c["name"]: c for c in sbom(tmp_path)["components"]}
    for tool in ("bash", "patch", "git", "ripgrep", "python3", "node"):
        assert comps[tool]["scope"] == "required" and "not bundled" in comps[tool]["description"]
    assert comps["sentence-transformers"]["scope"] == "optional"


def test_serial_is_stable_for_the_same_lock_and_version(tmp_path):
    assert sbom(tmp_path)["serialNumber"] == sbom(tmp_path)["serialNumber"]
