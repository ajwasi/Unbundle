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


def test_compose_app_service_mounts_data_volume():
    # DATA_DIR_HOST (not the bare default) so a Portainer stack can override
    # it with an absolute host path — see docker-compose.yml's own comment on
    # why the bare relative "./data" default is a real data-loss trap there.
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert "${DATA_DIR_HOST:-./data}:/data" in app["volumes"]


def test_data_dir_host_is_documented_in_readme():
    # Regression lock for the Portainer relative-bind-mount data-loss trap:
    # DATA_DIR_HOST must be discoverable from the README, not just the
    # compose file's own comment, since that's what a Portainer user actually
    # reads before filling in the stack's Environment variables section.
    assert "DATA_DIR_HOST" in _readme_text()


def test_compose_app_service_has_no_env_file_dependency():
    # env_file requires its referenced file to actually exist or Compose
    # refuses to start the service at all — fatal for a Portainer "web
    # editor" stack, which has no real .env file sitting next to the compose
    # file. Regression-locks the fix: real .env files still work fine, since
    # Compose auto-loads one for ${VAR} substitution regardless of env_file.
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert "env_file" not in app


def test_compose_app_service_bakes_in_docker_correct_path_defaults():
    # app/config.py's own Settings defaults for these are relative paths
    # meant for local, non-Docker development (e.g. "sqlite:///./data/
    # humble.db", resolving to /app/data/humble.db in the container — a
    # directory nothing ever creates, only /data does). Without an explicit
    # override the app crashes at `alembic upgrade head` before it even
    # starts — confirmed live, not theoretical. These must be correct with
    # zero environment configuration at all, not just when a real .env
    # happens to be present.
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    env = app["environment"]
    assert any(e.startswith("DATABASE_URL=") and "sqlite:////data/humble.db" in e for e in env)
    assert any(e.startswith("DATA_DIR=") and "/data" in e for e in env)
    assert any(e.startswith("DOWNLOADS_DIR=") and "/data/downloads" in e for e in env)


def test_dockerfile_bakes_in_the_same_docker_correct_path_defaults():
    # Regression lock for a real data-loss report: a deployer's own
    # hand-written compose file (not this repo's) mounted a named volume at
    # /data correctly, but never set DATABASE_URL/DATA_DIR — so the app fell
    # back to app/config.py's relative defaults, wrote its database into the
    # container's own throwaway layer instead of the mounted volume, and lost
    # everything on every redeploy. These three must be baked into the image
    # itself as ENV defaults, not rely solely on docker-compose.yml's own
    # explicit values, so *any* compose file gets a working database.
    text = _dockerfile_text()
    assert "ENV DATABASE_URL=sqlite:////data/humble.db" in text
    assert "ENV DATA_DIR=/data" in text
    assert "ENV DOWNLOADS_DIR=/data/downloads" in text


def test_compose_app_service_secrets_are_passthrough_not_hardcoded():
    # Bare (no "=value") entries pass through whatever a real .env file,
    # Portainer's own "Environment variables" stack UI, or the shell already
    # provides — and omit the variable entirely if none of those set it, so
    # app/config.py's own insecure-default-plus-loud-startup-warning
    # behavior still applies correctly. Must never gain a hardcoded value
    # here, which would either bake in a real secret or silently override
    # whatever the deployer actually configured.
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    for name in ("APP_SECRET_KEY", "APP_PASSWORD"):
        assert name in app["environment"]


def test_compose_app_service_trusts_proxy_headers_from_the_compose_network():
    # Not a Settings field (uvicorn reads this env var directly, see
    # app/deps.py's AuthMiddleware docstring / README's "Reverse proxy / HTTPS"
    # section) — checked here instead of the generic Settings-vs-.env.example
    # cross-check below, which only knows about real Settings fields.
    app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert "FORWARDED_ALLOW_IPS=*" in app["environment"]


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


def test_compose_mock_api_service_is_behind_a_profile_not_started_by_default():
    services = _load_yaml("docker-compose.yml")["services"]
    assert "demo" in services["mock-api"]["profiles"]
    assert "ports" not in services["mock-api"]  # never exposed to the host


def test_compose_mock_api_service_reuses_the_apps_own_image():
    services = _load_yaml("docker-compose.yml")["services"]
    assert services["mock-api"]["build"] == services["app"]["build"]


# --- docker-compose.postgres.yml structure (override fragment, not standalone) ---


def test_postgres_compose_app_database_url_uses_the_postgres_service_hostname_and_scheme():
    # Regression-locks the easy-to-mistype trap: SQLAlchemy's real backend
    # name is "postgresql" (trailing "ql"), and the DSN must use the
    # +psycopg driver suffix (bare postgresql:// resolves to psycopg2 by
    # default, which isn't installed) and the Compose *service* name
    # "postgres" as the hostname (only resolves inside the Compose network,
    # never "localhost").
    app = _load_yaml("docker-compose.postgres.yml")["services"]["app"]
    database_url = next(e for e in app["environment"] if e.startswith("DATABASE_URL="))
    assert "postgresql+psycopg://" in database_url
    assert "@postgres:5432/" in database_url


def test_postgres_compose_app_waits_for_postgres_to_be_healthy():
    app = _load_yaml("docker-compose.postgres.yml")["services"]["app"]
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_postgres_compose_postgres_service_has_a_healthcheck():
    postgres = _load_yaml("docker-compose.postgres.yml")["services"]["postgres"]
    assert "healthcheck" in postgres and postgres["healthcheck"]["test"]


def test_postgres_compose_does_not_redefine_services_from_the_base_file():
    # Encodes the confirmed "override fragment, not standalone stack"
    # decision as a real test rather than just an intention — mock-api/
    # prometheus/grafana must keep coming from docker-compose.yml alone.
    services = _load_yaml("docker-compose.postgres.yml")["services"]
    assert set(services) == {"app", "postgres"}


def test_postgres_compose_password_is_a_single_shared_token_not_duplicated_hardcoded_values():
    services = _load_yaml("docker-compose.postgres.yml")["services"]
    app_env = services["app"]["environment"]
    postgres_env = services["postgres"]["environment"]
    assert any("${POSTGRES_PASSWORD}" in e for e in app_env)
    assert any(e == "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" for e in postgres_env)


# --- docker-compose.image.yml structure (standalone, pulls instead of building) ---


def test_image_compose_app_service_uses_the_published_image_not_a_local_build():
    app = _load_yaml("docker-compose.image.yml")["services"]["app"]
    assert app["image"] == "ghcr.io/ajwasi/unbundle:latest"
    assert "build" not in app


def test_image_compose_app_service_matches_the_base_files_port_and_data_mount():
    # Same deployment shape as docker-compose.yml's own app service — only how
    # the image is obtained differs.
    image_app = _load_yaml("docker-compose.image.yml")["services"]["app"]
    base_app = _load_yaml("docker-compose.yml")["services"]["app"]
    assert image_app["ports"] == base_app["ports"]
    assert "${DATA_DIR_HOST:-./data}:/data" in image_app["volumes"]


def test_image_compose_app_service_has_no_env_file_dependency():
    # Same reasoning as the base file's own version of this test — this is
    # precisely the file a Portainer "web editor" stack (no real .env
    # present) would use, so it matters most here.
    app = _load_yaml("docker-compose.image.yml")["services"]["app"]
    assert "env_file" not in app


def test_image_compose_app_service_bakes_in_docker_correct_path_defaults():
    # Same reasoning as the base file's own version of this test — confirmed
    # live: without this, `alembic upgrade head` crashes on startup with no
    # environment configuration at all (app/config.py's own DATABASE_URL
    # default is a relative path meant for local dev, not this container).
    app = _load_yaml("docker-compose.image.yml")["services"]["app"]
    env = app["environment"]
    assert any(e.startswith("DATABASE_URL=") and "sqlite:////data/humble.db" in e for e in env)
    assert any(e.startswith("DATA_DIR=") and "/data" in e for e in env)
    assert any(e.startswith("DOWNLOADS_DIR=") and "/data/downloads" in e for e in env)


def test_image_compose_app_service_secrets_are_passthrough_not_hardcoded():
    app = _load_yaml("docker-compose.image.yml")["services"]["app"]
    for name in ("APP_SECRET_KEY", "APP_PASSWORD"):
        assert name in app["environment"]


def test_image_compose_has_no_demo_profile_service():
    # mock-api runs from a `build:`-produced image with a different command —
    # not reachable in a no-source, pull-only deployment (see the file's own
    # header comment), so it must not be offered here at all.
    services = _load_yaml("docker-compose.image.yml")["services"]
    assert "mock-api" not in services


def test_image_compose_observability_services_are_behind_a_profile_not_started_by_default():
    services = _load_yaml("docker-compose.image.yml")["services"]
    for name in ("prometheus", "grafana"):
        assert "observability" in services[name]["profiles"]


def test_publish_workflow_gates_the_image_build_on_tests_passing():
    # A tag whose tests fail must never publish an image under that tag — locks
    # in the `needs: test` dependency so a future edit can't silently drop it.
    jobs = _load_yaml(".github/workflows/docker-publish.yml")["jobs"]
    assert "test" in jobs
    assert jobs["build-and-push"]["needs"] == "test"


def test_image_compose_image_matches_the_tag_the_publish_workflow_pushes():
    # Regression-locks the two sources of truth this file already exists to
    # catch drift between: the compose file's own image reference, and the
    # workflow that actually publishes under that name. A rename on one side
    # with the other left stale would otherwise only surface as a confusing
    # "pull access denied" on someone's real homelab host.
    compose_image = _load_yaml("docker-compose.image.yml")["services"]["app"]["image"]
    workflow = _load_yaml(".github/workflows/docker-publish.yml")
    workflow_image_name = workflow["env"]["IMAGE_NAME"]
    assert compose_image == f"{workflow_image_name}:latest"


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

# The opposite direction of the exemption above: a real .env.example entry
# that intentionally has no matching Settings field. POSTGRES_PASSWORD is
# only ever consumed by docker-compose.postgres.yml's own ${POSTGRES_PASSWORD}
# substitution (feeds both the postgres service and app's interpolated
# DATABASE_URL) — the app itself never reads it directly, so it can't live
# in the Settings model the way FORWARDED_ALLOW_IPS's *absence* from
# .env.example solves the same kind of mismatch in the other direction (that
# one is a safe fixed value set directly in docker-compose.yml instead; a
# password can't be hardcoded into committed YAML the same way).
_ENV_ONLY_VARS_WITH_NO_SETTINGS_FIELD = {"POSTGRES_PASSWORD"}


def test_env_example_documents_every_user_facing_setting():
    documented = _env_example_names()
    for field_name in Settings.model_fields:
        if field_name in _INTENTIONALLY_UNDOCUMENTED_SETTINGS:
            continue
        env_var = field_name.upper()
        assert env_var in documented, f"{env_var} (Settings.{field_name}) has no .env.example entry"


def test_env_example_has_no_stale_entries_for_settings_that_no_longer_exist():
    known_env_vars = {f.upper() for f in Settings.model_fields} | _ENV_ONLY_VARS_WITH_NO_SETTINGS_FIELD
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
