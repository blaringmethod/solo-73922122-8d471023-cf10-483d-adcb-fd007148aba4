"""上传 manifest / 租约 / 保留规则的请求模型与校验。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ManifestFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    partitions: list[str] = Field(min_length=1)


class Manifest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=128)
    created_at: str = Field(min_length=1, max_length=64)
    status: Literal["COMPLETE", "IN_PROGRESS"] = "COMPLETE"
    files: list[ManifestFile] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def unique_paths(cls, v):
        paths = [f.path for f in v]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest 内文件路径重复")
        return v

    @field_validator("created_at")
    @classmethod
    def valid_ts(cls, v: str) -> str:
        from .planner import parse_ts
        if parse_ts(v) is None:
            raise ValueError("created_at 不是合法的 ISO-8601 时间")
        return v


class LeaseIn(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    required_partitions: list[str] = Field(min_length=1)

    @field_validator("required_partitions")
    @classmethod
    def dedupe(cls, v):
        out, seen = [], set()
        for p in v:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        if not out:
            raise ValueError("至少需要一个分区")
        return out


class RulesIn(BaseModel):
    min_snapshots: int = Field(ge=0, le=12)
    protect_days: int = Field(ge=0, le=3650)
