"""Tests that documentation and Docker/Compose config actually match reality.

This project has a real history of this drifting: app/config.py's comment and
README.md both claimed humble_cli_key_path resolves under $HOME=/home/appuser
in Docker — the actual Dockerfile has never created that user at all and sets
HOME=/root explicitly (running as root). Neither a human re-reading the prose
nor the app's own test suite (which never touches the Dockerfile or README)
would catch that kind of drift.

These parse the real files rather than duplicate their content as string
literals, so a genuine future change to any of them doesn't fight the tests
for no reason — only an actual *inconsistency* between two of these sources
should ever fail one.
"""

import re
from pathlib import Path

import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def _dockerfile_text() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _readme_text() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _config_source() -> str:
    return (ROOT / "app" / "config.py").read_text(encoding="utf-8")


def _env_example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }


def _dockerfile_expose_port() -> str:
    match = re.search(r"^EXPOSE\s+(\d+)", _dockerfile_text(), re.MULTILINE)
    assert match, "Dockerfile has no EXPOSE line"
    return match.group(1)


# --- docker-compose.yml structure ---


def test_compose_app_service_exposes_the_documented_port():
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert "8010:8000" in app["ports"]


def test_compose_app_service_mounts_data_volume_and_env_file():
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert "./data:/data" in app["volumes"]
    assert ".env" in app["env_file"]


def test_compose_container_port_matches_dockerfile_expose():
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    container_port = app["ports"][0].split(":")[1]
    assert container_port == _dockerfile_expose_port()


def test_compose_observability_services_are_behind_a_profile_not_started_by_default():
    services = _load_yaml("docker-compose.yml")["services"]
    for name in ("prometheus", "grafana"):
        assert "observability" in services[name]["profiles"]


def test_compose_observability_services_mount_the_example_config_files():
    services = _load_yaml("docker-compose.yml")["services"]
    assert any("examples/observability/prometheus.yml" in v for v in services["prometheus"]["volumes"])
    assert any("examples/observability/grafana-datasource.yml" in v for v in services["grafana"]["volumes"])


# --- the observability example configs, cross-checked against each other ---


def test_example_prometheus_config_targets_the_apps_container_port_not_the_host_port():
    # Deliberately app:8000 (Compose service name + container port), not
    # localhost:8010 (the host-mapped port) — within the Compose network,
    # Prometheus reaches the app over the service name, not the host mapping.
    prom_config = _load_yaml("examples/observability/prometheus.yml")
    targets = prom_config["scrape_configs"][0]["static_configs"][0]["targets"]
    assert targets == [f"app:{_dockerfile_expose_port()}"]


def test_example_grafana_datasource_points_at_the_compose_prometheus_service():
    datasource = _load_yaml("examples/observability/grafana-datasource.yml")["datasources"][0]
    prom_port = _load_yaml("docker-compose.yml")["services"]["prometheus"]["ports"][0].split(":")[1]
    assert datasource["url"] == f"http://prometheus:{prom_port}"


# --- .env.example vs the real Settings model ---

# Deliberately absent from .env.example: both already resolve correctly inside
# the container without an override (see their own comments in app/config.py),
# and .env.example is the template a real deployment copies — documenting
# either there would invite exactly the local-dev footgun their own docstrings
# warn against (colliding with a real ~/.humble-cli-key on the host).
_INTENTIONALLY_UNDOCUMENTED_SETTINGS = {"humble_cli_path", "humble_cli_key_path"}


def test_env_example_documents_every_user_facing_setting():
    documented = _env_example_names()
    for field_name in Settings.model_fields:
        if field_name in _INTENTIONALLY_UNDOCUMENTED_SETTINGS:
            continue
        env_var = field_name.upper()
        assert env_var in documented, f"{env_var} (Settings.{field_name}) has no .env.example entry"


def test_env_example_has_no_stale_entries_for_settings_that_no_longer_exist():
    known_env_vars = {f.upper() for f in Settings.model_fields}
    for name in _env_example_names():
        assert name in known_env_vars, f".env.example documents {name}, which no Settings field defines"


# --- regression lock for the actual /home/appuser bug this file was written for ---


def test_dockerfile_home_value_matches_what_config_and_readme_document():
    match = re.search(r"^ENV HOME=(\S+)", _dockerfile_text(), re.MULTILINE)
    assert match, "Dockerfile has no ENV HOME= line"
    home = match.group(1)
    assert home in _config_source(), f"Dockerfile sets HOME={home}, but app/config.py's comment doesn't mention it"
    assert home in _readme_text(), f"Dockerfile sets HOME={home}, but README.md doesn't mention it"


def test_readme_and_config_do_not_reference_a_nonexistent_container_user():
    # The Dockerfile has never created an "appuser" or any non-root user at
    # all — it runs as root with HOME=/root set explicitly.
    assert "appuser" not in _readme_text()
    assert "appuser" not in _config_source()
