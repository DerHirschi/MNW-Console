#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deploy ship_probe.py into a Modern Naval Warfare install.

Port of deploy_director.py with a distinct piggyback marker so the probe
coexists with the director (and other piggybacks) in the same package.

Deployment modes (auto-detected, --inject/--execute/--package to force):

1) INJECT mode (default on real game installs):
   The game loads/extracts its scripts from the .kyt ZIP package in
   Var/Scripts/Packages/ (e.g. hal_9025.kyt). We add _Source/ship_probe.py
   + config directly into that package and update its manifest.json.
   A SECOND .kyt in that folder BREAKS the game's script extraction
   ("An item with the same key has already been added" + loading screen
   hang), so a separate package must NOT be used.

2) EXECUTE mode (default when Var/Scripts/Execute/_Source exists):
   Loose copy of ship_probe.py into Execute/_Source/ + manifest.json
   entry (for extracted/dev copies of the install).

3) PACKAGE mode (legacy, avoid): builds ship_probe.kyt as a second
   package. Known to hang the game on real installs - only for testing.

Usage:
  python3 deploy.py --game-root <MNW_DIR> [--backup] [--no-config] [--inject|--execute|--package]
  python3 deploy.py --game-root <MNW_DIR> --verify
  python3 deploy.py --game-root <MNW_DIR> --remove
  python3 deploy.py --game-root <MNW_DIR> --purge-execute

<MNW_DIR> is the game directory that contains the Var/ folder.
On Linux/Proton this is typically:
  <SteamLibrary>/steamapps/common/Modern Naval Warfare/
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile

SCRIPT_NAME = "ship_probe.py"
CONFIG_NAME = "ship_probe_config.json"
MANIFEST_NAME = "manifest.json"
LOCK_NAME = "ship_probe.lock"
EXECUTE_REL = os.path.join("Var", "Scripts", "Execute")
PACKAGES_REL = os.path.join("Var", "Scripts", "Packages")
PKG_NAME = "ship_probe.kyt"

# Same provably-executed element scripts the director piggybacks onto.
# We append a guarded `import ship_probe; ship_probe.ship_probe_tick(globals())`
# into their _random_tick_ bodies so the probe runs even though standalone
# scripts are never instantiated by the game.
PIGGYBACK_TARGETS = [
    "_Source/General_Behaviour_logic.py",
    "_Source/General_Behaviour_logic_boat.py",
    "_Source/General_Behaviour_logic_civ.py",
    "_Source/General_Behaviour_logic_helicopter.py",
    "_Source/General_Behaviour_logic_helicopter_test.py",
    "_Source/General_Behaviour_logic_plane.py",
    "_Source/General_Behaviour_logic_submarine.py",
    "_Source/operational_ai.py",
]
PIGGYBACK_MARKER = "# ship_probe piggyback v1"


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def _nl_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def _piggyback_block(indent, nl):
    lines = [
        "# ship_probe piggyback v1",
        "try:",
        "    import ship_probe",
        "    ship_probe.ship_probe_tick(globals())",
        "except Exception:",
        "    pass",
    ]
    return "".join(indent + ln + nl for ln in lines)


def _has_current_block(text):
    return re.search(r"ship_probe\.ship_probe_tick\(\s*globals\(\)\s*\)", text) is not None


def patch_script(data):
    """Append/upgrade the guarded ship_probe_tick(globals()) call in _random_tick_ (idempotent).

    Older injected blocks (without globals()) are detected by marker and
    replaced with the current form, so stale loose copies get upgraded too.
    """
    text = data.decode("utf-8")
    if PIGGYBACK_MARKER in text:
        if _has_current_block(text):
            return data
        stripped = strip_patch(data)
        if stripped != data:
            text = stripped.decode("utf-8")
        else:
            return data
    m = re.search(r"^def _random_tick_\(\):\r?\n", text, re.MULTILINE)
    if m is None:
        return data
    body_start = m.end()
    nxt = re.search(r"^def ", text[body_start:], re.MULTILINE)
    body_end = body_start + nxt.start() if nxt else len(text)
    body = text[body_start:body_end]
    indent = ""
    for ln in body.splitlines():
        if ln.strip():
            indent = ln[: len(ln) - len(ln.lstrip())]
            break
    if not indent:
        indent = "    "
    block = _piggyback_block(indent, _nl_of(text))
    body_text = body.rstrip("\r\n")
    tail = body[len(body_text):]
    sep = 2 if tail.startswith("\r\n") else 1
    insert_at = body_start + len(body_text) + sep
    return (text[:insert_at] + block + text[insert_at:]).encode("utf-8")


def strip_patch(data):
    """Remove the exact piggyback block (idempotent). Matches any ship_probe_tick(...) call form."""
    text = data.decode("utf-8")
    if PIGGYBACK_MARKER not in text:
        return data
    pat = re.compile(
        r"(?m)^[ \t]*# ship_probe piggyback v1\r?\n"
        r"[ \t]*try:\r?\n"
        r"[ \t]+import ship_probe\r?\n"
        r"[ \t]+ship_probe\.ship_probe_tick\(.*?\)\r?\n"
        r"[ \t]*except Exception:\r?\n"
        r"[ \t]*pass\r?\n"
    )
    out, n = pat.subn("", text)
    return out.encode("utf-8") if n else data


def upsert_entry(content, arc, digest):
    for i, e in enumerate(content):
        if e.get("path") == arc:
            content[i] = {"path": arc, "hash": digest}
            return
    content.append({"path": arc, "hash": digest})


def load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def source_paths():
    here = script_dir()
    return {
        "script": os.path.join(here, SCRIPT_NAME),
        "config": os.path.join(here, CONFIG_NAME),
    }


def detect_mode(game_root):
    if os.path.isdir(os.path.join(game_root, EXECUTE_REL, "_Source")):
        return "execute"
    if find_scripting_package(os.path.join(game_root, PACKAGES_REL)):
        return "inject"
    if os.path.isdir(os.path.join(game_root, PACKAGES_REL)):
        return "package"
    return None


def find_scripting_package(pkgs_dir):
    """Return the .kyt that is the scripting components package (has _Source/_MNW entries)."""
    if not os.path.isdir(pkgs_dir):
        return None
    for name in sorted(os.listdir(pkgs_dir)):
        if not name.lower().endswith(".kyt"):
            continue
        p = os.path.join(pkgs_dir, name)
        try:
            with zipfile.ZipFile(p) as zf:
                if MANIFEST_NAME not in zf.namelist():
                    continue
                m = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
                paths = [e.get("path", "") for e in m.get("content", [])]
                if any(x.startswith("_Source/") for x in paths) or any(x.startswith("_MNW/") for x in paths):
                    return p
        except Exception:
            continue
    return None


def _package_files(write_config):
    src = source_paths()
    files = [("_Source/" + SCRIPT_NAME, src["script"])]
    if write_config:
        files.append(("_Source/" + CONFIG_NAME, src["config"]))
    return files


def inject_package(game_root, write_config=True, backup=True):
    src = source_paths()
    pkgs_dir = os.path.join(game_root, PACKAGES_REL)
    kyt = find_scripting_package(pkgs_dir)
    if kyt is None:
        print("ERROR: no scripting package (kyt with _Source/_MNW entries) in %s" % pkgs_dir)
        sys.exit(1)
    if not os.path.isfile(src["script"]):
        print("ERROR: %s not found next to this script" % src["script"])
        sys.exit(1)

    if backup:
        bak = kyt + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(kyt, bak)
            print("backup package -> %s" % bak)

    files = _package_files(write_config)
    file_arcs = set(arc for arc, _ in files)
    patched_hashes = {}
    tmp = kyt + ".tmp"
    with zipfile.ZipFile(kyt) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        manifest = None
        for info in zin.infolist():
            if info.filename == MANIFEST_NAME:
                manifest = json.loads(zin.read(info.filename).decode("utf-8"))
                continue
            if info.filename in file_arcs:
                continue
            if info.filename.endswith("/"):
                zi = zipfile.ZipInfo(info.filename, info.date_time)
                zi.compress_type = zipfile.ZIP_STORED
                zi.external_attr = info.external_attr
                zout.writestr(zi, b"")
                continue
            data = zin.read(info.filename)
            if info.filename in PIGGYBACK_TARGETS:
                new = patch_script(data)
                if new != data:
                    patched_hashes[info.filename] = md5_bytes(new)
                    data = new
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
        for arc, fp in files:
            zout.write(fp, arc)
        content = manifest.setdefault("content", [])
        for arc, fp in files:
            upsert_entry(content, arc, md5(fp))
        for arc, digest in patched_hashes.items():
            upsert_entry(content, arc, digest)
        zout.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    os.replace(tmp, kyt)
    print("injected into %s (%d files, %d manifest entries, %d piggyback-patched)" % (
        kyt, len(files), len(content), len(patched_hashes)))
    for arc in sorted(patched_hashes):
        print("  patched %s" % arc)
    print("syncing loose Execute tree (game does not reliably overwrite extracted files) ...")
    sync_execute_tree(game_root, backup=backup, write_config=write_config)
    print("Next: fully exit MNW, start it, load ANY mission, wait ~60 s.")
    print("Probe output appears in the log dir (see ship_probe_log.txt).")


def sync_execute_tree(game_root, backup=True, write_config=True):
    """Idempotently sync the loose Execute tree: ship_probe.py + config overwritten,
    piggyback blocks patched/upgraded, manifest entries updated.

    The game extracts the .kyt package at startup but does not reliably
    overwrite already-extracted loose files, so stale copies (old ship_probe.py,
    old piggyback blocks) can keep running. This sync makes the loose tree
    match the repo regardless of extraction behaviour. Safe to re-run.
    """
    src = source_paths()
    exe_dir = os.path.join(game_root, EXECUTE_REL)
    src_dir = os.path.join(exe_dir, "_Source")
    manifest_path = os.path.join(exe_dir, MANIFEST_NAME)

    if not os.path.isdir(src_dir):
        print("WARNING: %s missing - skipping execute-tree sync" % src_dir)
        return
    if not os.path.isfile(src["script"]):
        print("ERROR: %s not found next to this script" % src["script"])
        sys.exit(1)

    if backup and os.path.isfile(manifest_path):
        bak = manifest_path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(manifest_path, bak)
            print("backup manifest -> %s" % bak)

    lock_path = os.path.join(src_dir, LOCK_NAME)
    if os.path.isfile(lock_path):
        try:
            os.remove(lock_path)
            print("removed stale %s (previous run left it behind)" % lock_path)
        except Exception as e:
            print("WARNING: could not remove stale lock %s: %s" % (lock_path, e))
    else:
        print("no stale lock present (%s)" % lock_path)

    dest = os.path.join(src_dir, SCRIPT_NAME)
    shutil.copy2(src["script"], dest)
    print("synced %s -> %s" % (SCRIPT_NAME, dest))

    if write_config:
        cfg_dest = os.path.join(src_dir, CONFIG_NAME)
        shutil.copy2(src["config"], cfg_dest)
        print("synced %s -> %s" % (CONFIG_NAME, cfg_dest))

    patched = []
    for arc in PIGGYBACK_TARGETS:
        fpath = os.path.join(src_dir, arc.replace("_Source/", ""))
        if not os.path.isfile(fpath):
            continue
        with open(fpath, "rb") as f:
            data = f.read()
        new = patch_script(data)
        if new != data:
            with open(fpath, "wb") as f:
                f.write(new)
            patched.append(arc)
            print("piggyback patched/upgraded %s" % fpath)

    manifest = load_json(manifest_path) if os.path.isfile(manifest_path) else {"content": []}
    content = manifest.setdefault("content", [])
    upsert_entry(content, "_Source/" + SCRIPT_NAME, md5(dest))
    if write_config:
        cfg_dest = os.path.join(src_dir, CONFIG_NAME)
        if os.path.isfile(cfg_dest):
            upsert_entry(content, "_Source/" + CONFIG_NAME, md5(cfg_dest))
    for arc in patched:
        upsert_entry(content, arc, md5(os.path.join(src_dir, arc.replace("_Source/", ""))))
    save_json(manifest_path, manifest)
    print("manifest updated (%s entries) -> %s" % (len(content), manifest_path))


def deploy_execute(game_root, backup=True, write_config=True):
    sync_execute_tree(game_root, backup=backup, write_config=write_config)
    print()
    print("Next: start MNW, load ANY mission (player mission), wait ~60 s.")
    print("Probe output will appear in the log dir (see ship_probe_log.txt).")
    print("Player.log on Proton: <steam>/steamapps/compatdata/<APPID>/pfx/drive_c/users/steamuser/AppData/LocalLow/WaveOps/ModernNavalWarfare/Player.log")


def build_package(game_root, write_config=True, backup=False):
    src = source_paths()
    pkgs_dir = os.path.join(game_root, PACKAGES_REL)
    if not os.path.isdir(pkgs_dir):
        print("ERROR: not a MNW install (missing %s)" % pkgs_dir)
        sys.exit(1)
    if not os.path.isfile(src["script"]):
        print("ERROR: %s not found next to this script" % src["script"])
        sys.exit(1)

    pkg_path = os.path.join(pkgs_dir, PKG_NAME)
    files = _package_files(write_config)
    content = [{"path": arc, "hash": md5(fp)} for arc, fp in files]

    if backup and os.path.isfile(pkg_path):
        bak = pkg_path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(pkg_path, bak)
            print("backup package -> %s" % bak)

    with zipfile.ZipFile(pkg_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, fp in files:
            zf.write(fp, arc)
        zf.writestr(MANIFEST_NAME, json.dumps({"content": content}, indent=2))
    print("built %s (%d files)" % (pkg_path, len(files)))
    print("WARNING: a second .kyt in Scripts/Packages is known to break the")
    print("game's script extraction (loading screen hang). Prefer --inject.")


def deploy(game_root, backup=True, write_config=True, force_mode=None):
    mode = force_mode or detect_mode(game_root)
    if mode is None:
        print("ERROR: not a MNW install (missing %s and %s)" % (EXECUTE_REL, PACKAGES_REL))
        sys.exit(1)
    if mode == "execute":
        deploy_execute(game_root, backup=backup, write_config=write_config)
    elif mode == "inject":
        inject_package(game_root, write_config=write_config, backup=backup)
    else:
        build_package(game_root, write_config=write_config, backup=backup)


def files_for_check():
    src = source_paths()
    out = [("_Source/" + SCRIPT_NAME, src["script"])]
    if os.path.isfile(src["config"]):
        out.append(("_Source/" + CONFIG_NAME, src["config"]))
    return out


def verify_execute_tree(game_root):
    """Verify the loose Execute tree: script/config copies, piggyback blocks, manifest.

    Piggyback blocks are only required when the probe script itself is
    deployed (i.e. not after --remove).
    """
    src = source_paths()
    exe_dir = os.path.join(game_root, EXECUTE_REL)
    src_dir = os.path.join(exe_dir, "_Source")
    manifest_path = os.path.join(exe_dir, MANIFEST_NAME)
    ok = True
    deployed = os.path.isfile(os.path.join(src_dir, SCRIPT_NAME))

    checks = files_for_check()
    manifest = load_json(manifest_path) if os.path.isfile(manifest_path) else {"content": []}
    for arc, built in checks:
        fp = os.path.join(exe_dir, arc)
        if not os.path.isfile(fp):
            if not deployed:
                print("not deployed: %s absent (informational)" % fp)
            else:
                print("MISSING IN EXECUTE: %s" % fp)
                ok = False
            continue
        if not deployed:
            print("not deployed: %s present but ship_probe absent (informational)" % fp)
            continue
        h = md5(fp)
        if h != md5(built):
            print("MISMATCH IN EXECUTE: %s differs from built file" % fp)
            ok = False
        else:
            print("EXECUTE OK: %s (%s)" % (arc, h))
        entry = next((e for e in manifest.get("content", []) if e.get("path") == arc), None)
        if entry is None or entry.get("hash") != h:
            print("MISMATCH: manifest entry for %s" % arc)
            ok = False

    for arc in PIGGYBACK_TARGETS:
        fpath = os.path.join(src_dir, arc.replace("_Source/", ""))
        if not os.path.isfile(fpath):
            continue
        with open(fpath, "rb") as f:
            data = f.read()
        entry = next((e for e in manifest.get("content", []) if e.get("path") == arc), None)
        if PIGGYBACK_MARKER not in data.decode("utf-8"):
            if deployed:
                print("MISSING PIGGYBACK: %s has no ship_probe hook" % fpath)
                ok = False
            else:
                print("not deployed: %s has no ship_probe hook (informational)" % fpath)
        elif not _has_current_block(data.decode("utf-8")):
            print("STALE PIGGYBACK: %s has old ship_probe block (no globals())" % fpath)
            ok = False
        elif patch_script(data) != data:
            print("MISMATCH: %s not idempotent (unexpected block)" % fpath)
            ok = False
        elif entry is None or entry.get("hash") != md5_bytes(data):
            print("MISMATCH: manifest entry for patched %s" % arc)
            ok = False
        else:
            print("PIGGYBACK OK: %s" % arc)

    print("checking remaining manifest entries against files ...")
    mism = 0
    for e in manifest.get("content", []):
        p = e.get("path")
        if not p:
            continue
        if p == MANIFEST_NAME:
            print("  self-entry manifest.json present (game ships it stale; informational)")
            continue
        fp = os.path.join(exe_dir, p)
        if not os.path.isfile(fp):
            print("  MISSING FILE: %s" % p)
            mism += 1
            continue
        if e.get("hash") != md5(fp):
            print("  HASH MISMATCH: %s" % p)
            mism += 1
    if mism:
        print("%d other manifest mismatch(es) - the game may have rewritten or updated its scripts" % mism)
        ok = False
    else:
        print("all other manifest entries match")

    return ok


def verify_execute(game_root):
    return 0 if verify_execute_tree(game_root) else 1


def verify_inject(game_root):
    src = source_paths()
    pkgs_dir = os.path.join(game_root, PACKAGES_REL)
    kyt = find_scripting_package(pkgs_dir)
    if kyt is None:
        print("MISSING: no scripting package found in %s" % pkgs_dir)
        return 1
    ok = True

    with zipfile.ZipFile(kyt) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        for arc, fp in files_for_check():
            if arc not in names:
                print("MISSING IN PKG: %s" % arc)
                ok = False
                continue
            h = md5(fp)
            if md5_bytes(zf.read(arc)) != h:
                print("MISMATCH: %s differs from built file" % arc)
                ok = False
            else:
                print("OK: %s (%s)" % (arc, h))
            entry = next((e for e in manifest.get("content", []) if e.get("path") == arc), None)
            if entry is None or entry.get("hash") != h:
                print("MISMATCH: manifest entry for %s" % arc)
                ok = False
        for arc in PIGGYBACK_TARGETS:
            if arc not in names:
                continue
            data = zf.read(arc)
            entry = next((e for e in manifest.get("content", []) if e.get("path") == arc), None)
            if PIGGYBACK_MARKER not in data.decode("utf-8"):
                print("MISSING PIGGYBACK: %s has no ship_probe hook" % arc)
                ok = False
            elif patch_script(data) != data:
                print("MISMATCH: %s not idempotent (unexpected block)" % arc)
                ok = False
            elif entry is None or entry.get("hash") != md5_bytes(data):
                print("MISMATCH: manifest entry for patched %s" % arc)
                ok = False
            else:
                print("PIGGYBACK OK: %s" % arc)
    if ok:
        print("injected entries OK")
    bak = kyt + ".bak"
    if os.path.isfile(bak):
        print("backup present: %s" % bak)
    print("checking loose Execute tree ...")
    tree_ok = verify_execute_tree(game_root)
    return 0 if (ok and tree_ok) else 1


def verify_package(game_root):
    pkgs_dir = os.path.join(game_root, PACKAGES_REL)
    pkg_path = os.path.join(pkgs_dir, PKG_NAME)
    ok = True

    if not os.path.isfile(pkg_path):
        print("MISSING: %s is not deployed" % pkg_path)
        return 1

    try:
        with zipfile.ZipFile(pkg_path) as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            names = set(zf.namelist())
            for e in manifest.get("content", []):
                p = e.get("path")
                if not p:
                    continue
                if p == MANIFEST_NAME:
                    print("  self-entry manifest.json present (informational)")
                    continue
                if p not in names:
                    print("  MISSING FILE IN PKG: %s" % p)
                    ok = False
                    continue
                if e.get("hash") != md5_bytes(zf.read(p)):
                    print("  HASH MISMATCH: %s" % p)
                    ok = False
    except Exception as ex:
        print("ERROR reading %s: %s" % (pkg_path, ex))
        return 1

    if ok:
        print("package manifest entries OK")
    return 0 if ok else 1


def verify(game_root, force_mode=None):
    mode = force_mode or detect_mode(game_root)
    if mode is None:
        print("ERROR: not a MNW install (missing %s and %s)" % (EXECUTE_REL, PACKAGES_REL))
        return 1
    if mode == "execute":
        return verify_execute(game_root)
    if mode == "inject":
        return verify_inject(game_root)
    return verify_package(game_root)


def remove_inject(game_root):
    pkgs_dir = os.path.join(game_root, PACKAGES_REL)
    kyt = find_scripting_package(pkgs_dir)
    if kyt is None:
        print("no scripting package found in %s" % pkgs_dir)
        return
    bak = kyt + ".bak"
    if os.path.isfile(bak):
        shutil.copy2(bak, kyt)
        os.remove(bak)
        print("restored %s from backup" % kyt)
        return
    tmp = kyt + ".tmp"
    stripped_hashes = {}
    with zipfile.ZipFile(kyt) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        manifest = None
        for info in zin.infolist():
            if info.filename == MANIFEST_NAME:
                manifest = json.loads(zin.read(info.filename).decode("utf-8"))
                continue
            if info.filename.endswith("/"):
                zi = zipfile.ZipInfo(info.filename, info.date_time)
                zi.compress_type = zipfile.ZIP_STORED
                zi.external_attr = info.external_attr
                zout.writestr(zi, b"")
                continue
            if info.filename in ("_Source/" + SCRIPT_NAME, "_Source/" + CONFIG_NAME):
                continue
            data = zin.read(info.filename)
            if info.filename in PIGGYBACK_TARGETS:
                new = strip_patch(data)
                if new != data:
                    stripped_hashes[info.filename] = md5_bytes(new)
                    data = new
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
        content = [e for e in manifest.get("content", [])
                   if e.get("path") not in ("_Source/" + SCRIPT_NAME, "_Source/" + CONFIG_NAME)]
        for arc, digest in stripped_hashes.items():
            upsert_entry(content, arc, digest)
        manifest["content"] = content
        zout.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    os.replace(tmp, kyt)
    print("stripped injected entries from %s (no backup found; %d piggyback blocks removed)" % (
        kyt, len(stripped_hashes)))
    print("cleaning loose Execute tree ...")
    clean_execute_tree(game_root)


def clean_execute_tree(game_root):
    """Remove ship_probe files + piggyback blocks from the loose Execute tree."""
    exe_dir = os.path.join(game_root, EXECUTE_REL)
    src_dir = os.path.join(exe_dir, "_Source")
    manifest_path = os.path.join(exe_dir, MANIFEST_NAME)

    for name in (SCRIPT_NAME, CONFIG_NAME):
        p = os.path.join(src_dir, name)
        if os.path.isfile(p):
            os.remove(p)
            print("removed %s" % p)

    stripped = []
    for arc in PIGGYBACK_TARGETS:
        fpath = os.path.join(src_dir, arc.replace("_Source/", ""))
        if not os.path.isfile(fpath):
            continue
        with open(fpath, "rb") as f:
            data = f.read()
        new = strip_patch(data)
        if new != data:
            with open(fpath, "wb") as f:
                f.write(new)
            stripped.append(arc)
            print("piggyback block removed from %s" % fpath)

    if os.path.isfile(manifest_path):
        bak = manifest_path + ".bak"
        if os.path.isfile(bak):
            shutil.copy2(bak, manifest_path)
            print("restored manifest from backup")
        else:
            manifest = load_json(manifest_path)
            content = [e for e in manifest.setdefault("content", [])
                       if e.get("path") not in ("_Source/" + SCRIPT_NAME, "_Source/" + CONFIG_NAME)]
            for arc in stripped:
                upsert_entry(content, arc, md5(os.path.join(src_dir, arc.replace("_Source/", ""))))
            manifest["content"] = content
            save_json(manifest_path, manifest)
            print("dropped manifest entries, restored %d piggyback hash(es)" % len(stripped))


def purge_execute(game_root):
    """Delete the managed loose files in Execute/ so the game re-extracts fresh
    copies from the (patched) package on next start. Use when stale loose files
    keep running even after --inject/--remove."""
    exe_dir = os.path.join(game_root, EXECUTE_REL)
    src_dir = os.path.join(exe_dir, "_Source")
    targets = [os.path.join(src_dir, SCRIPT_NAME),
               os.path.join(src_dir, CONFIG_NAME)] + \
              [os.path.join(src_dir, arc.replace("_Source/", "")) for arc in PIGGYBACK_TARGETS]
    gone = 0
    for p in targets:
        if os.path.isfile(p):
            os.remove(p)
            print("purged %s" % p)
            gone += 1
    if gone == 0:
        print("nothing to purge (managed files already absent)")
    print("Next: start MNW once so it re-extracts fresh files from the package,")
    print("then re-run deploy (or --inject) to keep the tree in sync.")


def remove(game_root, force_mode=None):
    mode = force_mode or detect_mode(game_root)
    if mode is None:
        print("ERROR: not a MNW install (missing %s and %s)" % (EXECUTE_REL, PACKAGES_REL))
        return
    if mode == "package":
        pkg_path = os.path.join(game_root, PACKAGES_REL, PKG_NAME)
        if os.path.isfile(pkg_path):
            os.remove(pkg_path)
            print("removed %s" % pkg_path)
        else:
            print("no %s deployed" % PKG_NAME)
        clean_execute_tree(game_root)
        return
    # The game extracts its scripts from the scripting .kyt into Execute/_Source
    # on every launch. detect_mode() reports "execute" as soon as _Source exists,
    # but the injected ship_probe lives in the package too. So --remove must strip
    # BOTH the package AND the loose tree, or the probe is re-extracted on the
    # next start.
    remove_inject(game_root)
    clean_execute_tree(game_root)


def main():
    ap = argparse.ArgumentParser(description="Deploy MNW ship data probe")
    ap.add_argument("--game-root", required=True, help="MNW install dir containing Var/")
    ap.add_argument("--backup", action="store_true", help="backup manifest/package before editing")
    ap.add_argument("--no-config", action="store_true", help="do not write sample config")
    ap.add_argument("--verify", action="store_true", help="verify deployment + manifest hashes")
    ap.add_argument("--remove", action="store_true", help="remove probe + manifest entry")
    ap.add_argument("--purge-execute", action="store_true", help="delete managed loose files in Execute/ (force re-extraction)")
    ap.add_argument("--package", action="store_true", help="force package mode (build ship_probe.kyt)")
    ap.add_argument("--execute", action="store_true", help="force execute mode (Execute/_Source tree)")
    ap.add_argument("--inject", action="store_true", help="force inject mode (into scripting kyt)")
    args = ap.parse_args()

    if sum([args.package, args.execute, args.inject, args.purge_execute]) > 1:
        print("ERROR: --package, --execute, --inject and --purge-execute are mutually exclusive")
        sys.exit(2)
    force = "package" if args.package else ("execute" if args.execute else ("inject" if args.inject else None))

    if args.verify:
        sys.exit(verify(args.game_root, force_mode=force))
    if args.remove:
        remove(args.game_root, force_mode=force)
        return
    if args.purge_execute:
        purge_execute(args.game_root)
        return
    deploy(args.game_root, backup=args.backup, write_config=not args.no_config, force_mode=force)


if __name__ == "__main__":
    main()
