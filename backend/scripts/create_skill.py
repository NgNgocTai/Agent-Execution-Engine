"""Create a local Agent Skill starter; never overwrites an existing directory."""

import argparse
import json
from pathlib import Path
import re
import yaml


def create_skill(destination, name, description):
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 55:
        raise ValueError(
            "Name must use lowercase hyphen-separated words, up to 55 characters"
        )
    if not 10 <= len(description) <= 1000:
        raise ValueError("Description must contain 10–1000 characters")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "scripts").mkdir()
    frontmatter = yaml.safe_dump(
        {"name": name, "description": description, "metadata": {"version": "1.0.0"}},
        sort_keys=False,
    )
    (destination / "SKILL.md").write_text(
        "---\n"
        + frontmatter
        + "---\nSummarize supplied alarm text. Report limitations; do not claim live network observations.\n",
        encoding="utf-8",
    )
    (destination / "scripts" / "summarize.py").write_text(
        'import json, sys\narguments = json.load(sys.stdin)\ntext = arguments["text"]\n'
        'print(json.dumps({"status": "success", "data": {"summary": text[:200], "characters": len(text)}}))\n',
        encoding="utf-8",
    )
    runtime = {
        "scripts": [
            {
                "entrypoint": "scripts/summarize.py",
                "purpose": "Summarize supplied alarm text",
                "input_fields": [{"name": "text", "kind": "string", "required": True}],
                "sample_input": {"text": "Alarm at demo site"},
                "output_contract": "skill_result",
                "timeout_seconds": 10,
            }
        ]
    }
    (destination / "runtime.json").write_text(
        json.dumps(runtime, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Create a SKILL.md/scripts/runtime.json skill starter"
    )
    parser.add_argument("destination", type=Path)
    parser.add_argument("--name", default="alarm-summary")
    parser.add_argument(
        "--description", default="Summarize supplied telecom alarm observations"
    )
    args = parser.parse_args()
    create_skill(args.destination, args.name, args.description)
    print("Created skill starter. Inspect, package and review it before activation.")


if __name__ == "__main__":
    main()
