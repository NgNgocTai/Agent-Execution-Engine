"""Agent Skill frontmatter and explicit, bounded script run specifications."""

import json
import re
import yaml
from services.skill_validation import Manifest, ScriptSpec


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    values = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in values:
            raise ValueError("Frontmatter keys must be unique strings")
        values[key] = loader.construct_object(value_node)
    return values


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def markdown_manifest(files):
    content = files.get("SKILL.md", "")
    match = re.fullmatch(r"---\s*\r?\n(.{1,8192}?)\r?\n---\s*\r?\n(.+)", content, re.S)
    if not match:
        raise ValueError("SKILL.md requires YAML frontmatter and Markdown instructions")
    try:
        tokens = list(yaml.scan(match[1]))
        if len(tokens) > 1000 or any(
            isinstance(
                token,
                (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken),
            )
            for token in tokens
        ):
            raise ValueError("Frontmatter aliases, anchors and tags are unsupported")
        metadata = yaml.load(match[1], Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    allowed = {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
    }
    if not isinstance(metadata, dict) or set(metadata) - allowed:
        raise ValueError("Unsupported Agent Skill frontmatter fields")
    name, description = metadata.get("name"), metadata.get("description")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name)
        or len(name) > 55
    ):
        raise ValueError("Agent Skill name must use lowercase hyphen-separated words")
    if not isinstance(description, str) or not 10 <= len(description) <= 1000:
        raise ValueError("Skill description must contain 10–1000 characters")
    for field in ("license", "compatibility", "allowed-tools"):
        if field in metadata and (
            not isinstance(metadata[field], str) or len(metadata[field]) > 1000
        ):
            raise ValueError("Invalid optional frontmatter field")
    attributes = metadata.get("metadata", {})
    if not isinstance(attributes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in attributes.items()
    ):
        raise ValueError("metadata must contain string values")
    scripts = sorted(
        name for name in files if name.startswith("scripts/") and name.endswith(".py")
    )
    specs = [ScriptSpec(entrypoint=path) for path in scripts]
    if "runtime.json" in files:
        raw = json.loads(files["runtime.json"])
        if (
            not isinstance(raw, dict)
            or set(raw) != {"scripts"}
            or not isinstance(raw["scripts"], list)
        ):
            raise ValueError("runtime.json must declare a scripts array")
        specs = [ScriptSpec.model_validate(value) for value in raw["scripts"]]
        if {spec.entrypoint for spec in specs} != set(scripts):
            raise ValueError(
                "runtime.json must cover every bundled Python script under scripts/"
            )
    return Manifest(
        name="custom_" + name.replace("-", "_"),
        version=attributes.get("version", "1.0.0"),
        description=description,
        instructions=match[2].strip(),
        package_format="agent_skill",
        scripts=specs,
    )
