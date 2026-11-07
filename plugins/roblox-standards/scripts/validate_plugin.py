#!/usr/bin/env python3
"""Check package paths, JSON, flat skill metadata and local Markdown links.

Uses only the Python standard library. This is a structural check, not a
substitute for loading the package in Codex or exercising its workflows.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def inside(path, parent):
    return path.resolve().is_relative_to(parent.resolve())


def load_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def skill_fields(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, raw = line.partition(":")
        if not separator or key not in {"name", "description"}:
            continue
        if key in fields:
            raise ValueError(f"{path}: duplicate {key}")
        raw = raw.strip()
        # This package uses flat metadata: bare names and JSON-quoted strings.
        if raw.startswith('"'):
            value = json.loads(raw)
        elif re.match(r"^[a-z0-9][a-z0-9-]*$", raw):
            value = raw
        else:
            raise ValueError(f"{path}: use a plain name or JSON-quoted {key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: empty or invalid {key}")
        fields[key] = value
    return fields


def validate(root, marketplace=None):
    errors = []
    manifest_path = root / "plugin.json"
    try:
        manifest = load_object(manifest_path)
    except (OSError, ValueError) as error:
        return [str(error)], 0, 0

    name = manifest.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        errors.append("plugin.json: invalid name")
    version = manifest.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version
    ):
        errors.append("plugin.json: expected a semantic version")
    if not isinstance(manifest.get("description"), str) or not manifest["description"].strip():
        errors.append("plugin.json: missing description")
    if manifest.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json":
        errors.append("plugin.json: expected the portable Agent Plugins schema")

    skills = sorted((root / "skills").glob("*/SKILL.md"))
    if not skills:
        errors.append("No skills found in skills/*/SKILL.md")
    names = set()
    for path in skills:
        try:
            if not inside(path, root):
                raise ValueError(f"{path}: skill escapes package root")
            fields = skill_fields(path)
            skill_name = fields.get("name", "")
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_name) or len(skill_name) > 64:
                raise ValueError(f"{path}: invalid skill name")
            if skill_name != path.parent.name or skill_name in names:
                raise ValueError(f"{path}: duplicate name or folder/name mismatch")
            names.add(skill_name)
            description = fields.get("description", "")
            if not description or len(description) > 1024:
                raise ValueError(f"{path}: missing or overly long description")
        except (OSError, ValueError) as error:
            errors.append(str(error))

    extension = manifest.get("extensions", {}).get("com.openai", {})
    onboarding = extension.get("onboardingSkill")
    if onboarding:
        path = root / onboarding
        if not onboarding.startswith("./") or not inside(path, root) or path not in skills:
            errors.append("plugin.json: onboardingSkill must resolve to a packaged SKILL.md")

    hook_reference = extension.get("hooks")
    if hook_reference:
        try:
            if not isinstance(hook_reference, str) or not hook_reference.startswith("./"):
                raise ValueError("plugin.json: expected a ./-prefixed hooks file")
            hook_path = root / hook_reference
            if not inside(hook_path, root):
                raise ValueError("plugin.json: hooks file escapes package root")
            hooks = load_object(hook_path).get("hooks", {})
            handlers = [handler for group in hooks.get("SessionStart", []) for handler in group.get("hooks", [])]
            if not handlers or any(handler.get("type") != "command" or not handler.get("command") for handler in handlers):
                raise ValueError("hooks: expected a SessionStart command handler")
            for handler in handlers:
                for relative in re.findall(r"\$\{PLUGIN_ROOT\}/([^\"\s]+)", handler["command"]):
                    target = root / relative
                    if not inside(target, root) or not target.is_file():
                        raise ValueError("hooks: command references a missing or external script")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append(str(error))

    links = 0
    for path in sorted(root.rglob("*.md")):
        for target in re.findall(r"\]\(([^\s)]+)\)", path.read_text(encoding="utf-8")):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            links += 1
            destination = path.parent / unquote(parsed.path)
            if not inside(destination, root):
                errors.append(f"{path.relative_to(root)}: link escapes installed package: {target}")
            elif not destination.exists():
                errors.append(f"{path.relative_to(root)}: broken link: {target}")

    if marketplace:
        try:
            catalog = load_object(marketplace)
            entries = catalog.get("plugins", [])
            matches = [entry for entry in entries if entry.get("name") == name]
            if len(matches) != 1:
                raise ValueError("marketplace: expected exactly one entry for this plugin")
            entry = matches[0]
            source = entry.get("source", {})
            relative = source.get("path", "")
            catalog_root = marketplace.parents[2]
            resolved = (catalog_root / relative).resolve()
            if not relative.startswith("./") or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("marketplace: expected a ./-prefixed path inside the source repository")
            if source.get("source") == "local":
                if not inside(resolved, catalog_root) or resolved != root.resolve():
                    raise ValueError("marketplace: local source path does not resolve to this plugin")
            elif source.get("source") == "git-subdir":
                remote = urlsplit(source.get("url", ""))
                if remote.scheme != "https" or not remote.hostname or remote.username or remote.password:
                    raise ValueError("marketplace: expected an HTTPS Git URL without embedded credentials")
                if source["url"].removesuffix(".git").rstrip("/") != manifest.get("repository", "").removesuffix(".git").rstrip("/"):
                    raise ValueError("marketplace: Git source does not match the plugin repository")
                if Path(relative).name != root.name:
                    raise ValueError("marketplace: Git subdirectory does not match the plugin folder")
            else:
                raise ValueError("marketplace: expected a local or git-subdir source")
            policy = entry.get("policy", {})
            if not policy.get("installation") or not policy.get("authentication") or not entry.get("category"):
                raise ValueError("marketplace: missing installation/authentication policy or category")
            if not catalog.get("name"):
                raise ValueError("marketplace: missing name")
        except (OSError, ValueError, TypeError, AttributeError, IndexError) as error:
            errors.append(str(error))
    return errors, len(skills), links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--marketplace", type=Path)
    args = parser.parse_args()
    root = args.plugin_dir.resolve()
    marketplace = args.marketplace
    if marketplace is None:
        candidate = root.parents[1] / ".agents/plugins/marketplace.json"
        marketplace = candidate if candidate.is_file() else None
    errors, skills, links = validate(root, marketplace.resolve() if marketplace else None)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"Valid package: {skills} skills, {links} local links; "
          f"marketplace {'checked' if marketplace else 'not included'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
