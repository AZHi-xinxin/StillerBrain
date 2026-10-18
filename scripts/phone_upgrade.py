"""Explicit, restartable phone-source transition. Imported only by install_phone upgrade/rollback."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import secrets


def atomic(phone, path, value):
    phone.plain_path(path)
    temporary = phone.plain_path(path.with_name(path.name + ".upgrade-new"))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def guest(phone, ctx, action, operation):
    command = ["/usr/bin/python3.12", "-I", "-B", phone.GUEST_SOURCE + "/scripts/phone_update_guest.py",
               action, "--state", phone.GUEST_STATE, "--operation", operation]
    phone.execute(ctx, phone.guest_command(ctx, command))


def marker_values(phone, ctx, record, manifest):
    return [(ctx["state"] / "phone-owner.json", record),
            (ctx["state"] / "phone-prepared.json", {"identity": record}),
            (ctx["container"] / ".stiller-phone-owner.json", {"identity": record, "manifest_sha256": manifest})]


def verify_journal(phone, ctx, journal):
    if (journal.get("format") != "stiller-phone-upgrade/1" or journal.get("phase") not in ("preparing", "complete", "rolled_back")
            or not re.fullmatch(r"upgrade-[a-f0-9]{24}", str(journal.get("operation", "")))):
        raise phone.PhoneError("升级记录无效，未接管实例。")
    old, new = journal["old"], journal["new"]
    if old.get("owner") != new.get("owner") or not re.fullmatch(r"[a-f0-9]{64}", str(old.get("owner", ""))):
        raise phone.PhoneError("升级归属不一致，未接管实例。")
    for record in (old, new):
        source = phone.plain_path(record["source"])
        original_ctx = phone.layout(source, ctx["state"], ctx["home"], ctx["prefix"], ctx["name"])
        expected = phone.identity(original_ctx, phone.source_digest(original_ctx))
        if any(record.get(key) != value for key, value in expected.items()):
            raise phone.PhoneError("升级涉及的源码或实例已变更；保留两个源码目录，未继续操作。")
    if ctx["source"] != Path(new["source"]) or phone.manifest_digest(ctx) != journal["container_manifest"]:
        raise phone.PhoneError("请从本次新源码目录执行升级或回退，且保留原 Ubuntu 实例。")
    for (path, old_value), (_, new_value) in zip(marker_values(phone, ctx, old, journal["container_manifest"]),
                                               marker_values(phone, ctx, new, journal["container_manifest"])):
        if phone.read_json(path) not in (old_value, new_value):
            raise phone.PhoneError("安装标记不属于这次升级，未覆盖。")


def transition(phone, ctx, digest, action, previous_source=None):
    journal_file = ctx["state"] / "phone-upgrade.json"
    journal = phone.read_json(journal_file) if journal_file.exists() else None
    if action == "upgrade" and journal is not None and journal.get("phase") == "complete" and journal.get("new", {}).get("source") == str(ctx["source"]) and journal["new"].get("source_manifest_sha256") == digest:
        verify_journal(phone, ctx, journal)
        phone.state_record(ctx, digest)
        print("本次升级已经完成，无需重复安装。请使用 check/start。")
        return
    if action == "upgrade" and (journal is None or journal.get("phase") in ("complete", "rolled_back")):
        if previous_source is None:
            raise phone.PhoneError("upgrade 需要 --from-source 指定保留的旧源码目录。不要在旧目录直接 git pull。")
        old_ctx = phone.layout(previous_source, ctx["state"], ctx["home"], ctx["prefix"], ctx["name"])
        if old_ctx["source"] == ctx["source"]:
            raise phone.PhoneError("升级请使用独立的新源码目录；旧源码用于回退，不能原地替换。")
        old = phone.state_record(old_ctx, phone.source_digest(old_ctx))
        phone.owned_container(old_ctx, old)
        if phone.read_json(ctx["state"] / "phone-prepared.json") != {"identity": old}:
            raise phone.PhoneError("原安装尚未准备完整，未升级。")
        phone.verify_os(ctx); phone.verify_runtime(ctx); phone.verify_storage(ctx)
        new = {**phone.identity(ctx, digest), "owner": old["owner"]}
        journal = {"format": "stiller-phone-upgrade/1", "operation": "upgrade-" + secrets.token_hex(12),
                   "phase": "preparing", "old": old, "new": new, "container_manifest": phone.manifest_digest(ctx)}
        atomic(phone, journal_file, journal)
    if journal is None:
        raise phone.PhoneError("没有可回退的升级记录。")
    verify_journal(phone, ctx, journal)
    if action == "rollback":
        guest(phone, ctx, "rollback", journal["operation"])
        target = journal["old"]
        phase = "rolled_back"
    else:
        guest(phone, ctx, "stage", journal["operation"])
        command = ["/usr/bin/python3.12", "-B", phone.GUEST_SOURCE + "/scripts/install_stiller.py",
                   "--directory", phone.GUEST_STATE + "/private"]
        phone.execute(ctx, phone.guest_command(ctx, command))
        guest(phone, ctx, "verify-unchanged", journal["operation"])
        verify_journal(phone, ctx, journal)
        target = journal["new"]
        phase = "complete"
    # A pending journal prevents normal start while these three markers are being committed.
    for path, value in marker_values(phone, ctx, target, journal["container_manifest"]):
        atomic(phone, path, value)
    journal["phase"] = phase
    atomic(phone, journal_file, journal)
    phone.state_record({**ctx, "source": Path(target["source"])}, target["source_manifest_sha256"])
    print("升级检查通过，网关/MCP 密钥、端口、密码授权与记忆保留；服务未常驻启动。" if phase == "complete" else
          "已回到升级前实例；升级后的副本仍保存在 phone-upgrades 中，不会静默删除后来记录。")
    print("私有备份位置：" + str(ctx["state"] / "phone-upgrades" / journal["operation"]))
    print("以上目录含凭证和记忆，请勿公开或放入共享存储。后续请从" + str(target["source"]) + "运行 check/start。")
