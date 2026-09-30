from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from .build_config import BuildOptions
from .models import InitOptions, Profile, ProfileDocument, RepoSpec, SetupOptions, validate_profile


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "MiniGameRepoTool"


SUGGESTED_SOURCES = ("D:/MiniGame", "D:/AIMiniGame", "D:/MiniGameProfiler")


def suggested_sources() -> list[str]:
    return [path for path in SUGGESTED_SOURCES if Path(path).is_dir()]


def default_profile() -> Profile:
    client = "git@client-gitlab.miniworldplus.com:miniwan/"
    yw = "git@yw-gitlab.miniworldplus.com:"
    rows = [
        ("main", "MiniGame", ".", client + "MiniGame.git", "game"),
        ("assets", "AssetRuntime", "AssetRuntime", client + "assetruntimenew.git", "game"),
        ("common", "CommonResource", "AssetRuntime/CommonResource", client + "CommonResource.git", "game"),
        ("source", "Source", "Source", client + "MiniGameCode.git", "game"),
        ("sandbox", "SandboxEngine", "Source/SandboxEngine", yw + "minitech/sandboxengine.git", "game"),
        ("aigameplay", "AIGamePlay", "Source/AIGamePlay", yw + "engine/aigameplay.git", "game"),
        ("rainbow", "RainbowEngine", "Source/Core/RainbowEngine", yw + "minitech/engine.git", "engine"),
        ("editor", "Editor", "Source/Editor", yw + "minitech/editor.git", "engine"),
    ]
    return Profile("minigame-e", "MiniGame 开发", "E:/MiniGame", {
        "game": "miniw/release/v1.55.50_ai_framework_V2",
        "engine": "Engine/Release_v6.2.9_ai_framework_V2",
    }, [RepoSpec(*row) for row in rows], SetupOptions(False, [row[0] for row in rows]),
        init=InitOptions("auto", suggested_sources()))


def parse_document(data: dict) -> ProfileDocument:
    try:
        if data.get("schema_version") != 1:
            raise ValueError("配置 schema_version 必须为 1")
        profiles = []
        for item in data["profiles"]:
            repos = [RepoSpec(**r) for r in item["repositories"]]
            setup = SetupOptions(**item.get("setup", {}))
            build = BuildOptions(**item.get("build", {}))
            init = InitOptions(**item.get("init", {}))
            profile = Profile(item["id"], item["name"], item["root"], item["branch_groups"], repos, setup, build, init)
            validate_profile(profile)
            profiles.append(profile)
        ids = [p.id for p in profiles]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("方案 ID 不能为空或重复")
        active = data.get("active_profile_id", ids[0])
        if active not in ids:
            raise ValueError("当前方案 ID 不存在")
        return ProfileDocument(1, active, profiles)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"配置结构无效：{error}") from error


def atomic_json(path: Path, data: dict, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if backup and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ProfileStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "profiles.json"

    def load(self) -> ProfileDocument:
        if not self.path.exists():
            p = default_profile()
            document = ProfileDocument(1, p.id, [p])
            self.save(document)
            return document
        return self.read(self.path)

    @staticmethod
    def read(path: Path) -> ProfileDocument:
        try:
            return parse_document(json.loads(Path(path).read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取配置 {path}：{error}") from error

    def save(self, document: ProfileDocument) -> None:
        payload = asdict(document)
        parse_document(payload)
        atomic_json(self.path, payload, backup=True)

    @staticmethod
    def export(document: ProfileDocument, path: Path) -> None:
        payload = asdict(document)
        parse_document(payload)
        atomic_json(Path(path), payload)
