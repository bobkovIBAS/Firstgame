#!/usr/bin/env python3
"""Initialize missing Roblox project documentation; preserve existing content."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = ".roblox-standards.json"
AGENTS_MARKER = "<!-- roblox-standards:agents -->"
README_MARKER = "<!-- roblox-standards:readme -->"


def project_path(root, relative):
    if not isinstance(relative, str) or not relative or any(c in relative for c in "\n\r`\\"):
        raise ValueError("Expected a relative project path using forward slashes")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Project paths must stay inside the selected project")
    destination = root / path
    if not destination.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes the project through a symlink: {relative}")
    for item in (destination, *destination.parents):
        if item == root:
            break
        if item.is_symlink() and not item.exists():
            raise ValueError(f"Dangling symlink in project path: {relative}")
    return destination


def read_config(root):
    path = project_path(root, CONFIG)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{CONFIG} must be a file")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError(f"{CONFIG}: expected schema_version 1")
    if not isinstance(config.get("auto_init", True), bool):
        raise ValueError(f"{CONFIG}: auto_init must be true or false")
    project_path(root, config.get("design_profile"))
    return config


def choose_profile(root, config, explicit=None):
    if explicit:
        if config and explicit != config["design_profile"]:
            raise ValueError("A different design_profile is already configured; update the config explicitly")
        return project_path(root, explicit)
    if config:
        return project_path(root, config["design_profile"])
    candidates = ("docs/design/README.md", "docs/style/README.md", "docs/STYLE_GUIDE.md", "STYLE_GUIDE.md")
    for relative in candidates:
        path = project_path(root, relative)
        if path.is_file():
            return path
    return project_path(root, candidates[0])


def agents_block(profile):
    template = (PACKAGE / "templates/project-AGENTS.md").read_text(encoding="utf-8")
    match = re.search(r"```markdown\n(.*?)\n```", template, re.S)
    if not match:
        raise ValueError("The packaged AGENTS template has no markdown fragment")
    components = (Path(profile).parent / "components").as_posix() + "/"
    return AGENTS_MARKER + "\n" + match.group(1).replace(
        "docs/design/README.md", profile
    ).replace("docs/design/components/", components) + "\n"


def initialize(project, check=False, design_profile=None):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("The selected project directory must already exist")
    if root.is_relative_to(PACKAGE):
        raise ValueError("Select a game project, not the installed plugin directory")
    config = read_config(root)
    profile = choose_profile(root, config, design_profile)
    relative = profile.relative_to(root).as_posix()
    components = profile.parent / "components/README.md"
    active_agents = root / "AGENTS.md"
    override = project_path(root, "AGENTS.override.md")
    if override.is_file() and override.read_text(encoding="utf-8").strip():
        active_agents = override
    agents_content = agents_block(relative)
    readme_content = (
        README_MARKER + "\n## Документация Roblox-проекта\n\n"
        f"Профиль оформления и разработки: [открыть]({quote(relative, safe='/')}).\n"
        "Общие паттерны доступны в установленном плагине Roblox Standards.\n"
        "Команда `roblox-init` проверяет и дополняет общие файлы проекта.\n"
    )
    profile_template = (PACKAGE / "templates/style-profile.md").read_text(encoding="utf-8")
    # The profile is a starting document, not an invented approved art direction.
    profile_template = profile_template.replace("`docs/design/README.md`", f"`{relative}`")
    profile_link = "../" + quote(profile.name)
    components_content = (
        "# Компоненты проекта\n\n"
        f"Общие визуальные решения описаны в [профиле]({profile_link}).\n\n"
        "Добавляйте сюда карточки повторяемых окон, построек и эффектов.\n"
        "В карточке указывайте назначение, путь в Studio или исходниках,\n"
        "применённые токены, состояния, жизненный цикл и фактические проверки.\n"
        "Шаблон карточки входит в установленный плагин Roblox Standards.\n"
    )
    config_content = json.dumps(
        {"schema_version": 1, "design_profile": relative, "auto_init": True},
        ensure_ascii=False, indent=2,
    ) + "\n"
    # Plan all writes first so a known path conflict cannot cause partial setup.
    targets = [
        (root / CONFIG, config_content, None),
        (profile, profile_template, None),
        (components, components_content, None),
        (active_agents, agents_content, AGENTS_MARKER),
        (root / "README.md", readme_content, README_MARKER),
    ]
    plan = []
    for path, content, marker in targets:
        path = project_path(root, path.relative_to(root).as_posix())
        for parent in path.parents:
            if parent == root:
                break
            if parent.exists() and not parent.is_dir():
                raise ValueError(f"Parent is not a directory: {parent.relative_to(root)}")
        if path.exists() and not path.is_file():
            raise ValueError(f"Expected a file: {path.relative_to(root)}")
        if not path.exists():
            action = "create"
        elif marker:
            existing = path.read_text(encoding="utf-8")
            present = marker in existing
            if path == active_agents:
                present = present or "## Roblox: общие стандарты и стиль проекта" in existing
            else:
                present = present or relative in existing or quote(relative, safe='/') in existing
            action = "keep" if present else "append"
        else:
            action = "keep"
        plan.append((path, content, action))
    changed = []
    if not check:
        for path, content, action in plan:
            if action == "keep":
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if action == "create":
                try:
                    with path.open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(content)
                except FileExistsError:
                    # Another initializer may have just created this file. Never overwrite it.
                    continue
            else:
                existing = path.read_text(encoding="utf-8")
                marker = AGENTS_MARKER if path == active_agents else README_MARKER
                if marker in existing:
                    continue
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write("\n\n" + content)
            changed.append(path.relative_to(root).as_posix())
    pending = [path.relative_to(root).as_posix() for path, _, action in plan if action != "keep"]
    return {
        "project": str(root), "design_profile": relative,
        "actions": [{"path": path.relative_to(root).as_posix(), "action": action} for path, _, action in plan],
        "changed": changed, "pending": pending if check else [], "ready": not pending if check else True,
    }


def discover_project(cwd):
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir() or path.is_relative_to(PACKAGE):
        return None
    for directory in (path, *path.parents):
        markers = ((directory / CONFIG).is_file() or any(
            item.is_file() for pattern in ("*.rbxl", "*.rbxlx", "*.project.json")
            for item in directory.glob(pattern)
        ))
        if markers:
            return directory
        if (directory / ".git").exists() or directory == Path.home():
            break
    return None


def hook_result(context):
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def session_start(payload):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        raise ValueError("SessionStart requires an absolute project cwd")
    root = discover_project(cwd)
    if root is None:
        return None
    config = read_config(root)
    if config and not config.get("auto_init", True):
        return None
    check = payload.get("permission_mode") == "plan"
    report = initialize(root, check=check)
    if report["pending"]:
        summary = "Проверка без записи в Plan: недостающие файлы: " + ", ".join(report["pending"])
    elif report["changed"]:
        summary = "Инициализированы общие файлы: " + ", ".join(report["changed"])
    else:
        summary = "Общие файлы уже существуют; изменений нет."
    return hook_result(
        "Roblox Standards. Проект: " + str(root) + ". " + summary + "\n"
        "Перед соответствующими правками прочитай профиль " + report["design_profile"]
        + " и активный AGENTS.md/AGENTS.override.md. Недостающие файлы можно создать командой roblox-init."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--design-profile", help="Relative path to an existing or new design profile")
    parser.add_argument("--check", action="store_true", help="Report missing files without writing; exit 1 if incomplete")
    parser.add_argument("--json", action="store_true", help="Print a structured initialization report")
    parser.add_argument("--hook", action="store_true", help="Handle SessionStart JSON on stdin")
    args = parser.parse_args()
    try:
        if args.hook:
            output = session_start(json.load(sys.stdin))
            if output:
                print(json.dumps(output, ensure_ascii=False))
            return 0
        report = initialize(args.project, check=args.check, design_profile=args.design_profile)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("Project: " + report["project"])
            for item in report["actions"]:
                print(item["action"].upper() + ": " + item["path"])
            print("Check only" if args.check else "Created missing files; existing content preserved")
        return 1 if args.check and not report["ready"] else 0
    except (OSError, ValueError) as error:
        if args.hook:
            print(json.dumps(hook_result("Roblox Standards: инициализация не завершена: " + str(error)), ensure_ascii=False))
            return 0
        print("ERROR: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
