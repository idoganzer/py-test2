"""Generate CODEOWNERS."""
from __future__ import annotations

from .model import Config, Integration

BASE = """
# This file is generated by script/hassfest/codeowners.py
# People marked here will be automatically requested for a review
# when the code that they own is touched.
# https://github.com/blog/2392-introducing-code-owners
# https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners

# Home Assistant Core
setup.cfg @home-assistant/core
/homeassistant/*.py @home-assistant/core
/homeassistant/helpers/ @home-assistant/core
/homeassistant/util/ @home-assistant/core

# Home Assistant Supervisor
build.json @home-assistant/supervisor
/machine/ @home-assistant/supervisor
/rootfs/ @home-assistant/supervisor
/Dockerfile @home-assistant/supervisor

# Other code
/homeassistant/scripts/check_config.py @kellerza

# Integrations
""".strip()

INDIVIDUAL_FILES = """
# Individual files
/homeassistant/components/demo/weather.py @fabaff
"""

REMOVE_CODEOWNERS = """
# Remove codeowners from files
/homeassistant/components/*/translations/
"""


def generate_and_validate(integrations: dict[str, Integration], config: Config):
    """Generate CODEOWNERS."""
    parts = [BASE]

    for domain in sorted(integrations):
        integration = integrations[domain]

        if not integration.manifest:
            continue

        codeowners = integration.manifest["codeowners"]

        if not codeowners:
            continue

        for owner in codeowners:
            if not owner.startswith("@"):
                integration.add_error(
                    "codeowners", "Code owners need to be valid GitHub handles."
                )

        parts.append(f"/homeassistant/components/{domain}/ {' '.join(codeowners)}")

        if (config.root / "tests/components" / domain / "__init__.py").exists():
            parts.append(f"/tests/components/{domain}/ {' '.join(codeowners)}")

    parts.append(f"\n{INDIVIDUAL_FILES.strip()}")
    parts.append(f"\n{REMOVE_CODEOWNERS.strip()}")

    return "\n".join(parts)


def validate(integrations: dict[str, Integration], config: Config):
    """Validate CODEOWNERS."""
    codeowners_path = config.root / "CODEOWNERS"
    config.cache["codeowners"] = content = generate_and_validate(integrations, config)

    if config.specific_integrations:
        return

    with open(str(codeowners_path)) as fp:
        if fp.read().strip() != content:
            config.add_error(
                "codeowners",
                "File CODEOWNERS is not up to date. Run python3 -m script.hassfest",
                fixable=True,
            )
        return


def generate(integrations: dict[str, Integration], config: Config):
    """Generate CODEOWNERS."""
    codeowners_path = config.root / "CODEOWNERS"
    with open(str(codeowners_path), "w") as fp:
        fp.write(f"{config.cache['codeowners']}\n")