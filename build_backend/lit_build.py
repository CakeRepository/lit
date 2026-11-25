"""In-tree PEP 517/660 build backend for lit, using only the standard library.

Project settings come from pyproject.toml: [project] plus an optional
[tool.lit_build] table with `package` (default: the normalized project name's
import package) and `package-data` (globs relative to the package directory).
PEP 517 runs the backend with the source tree as the working directory; that
directory is the project root (falling back to the parent of build_backend/).
"""

import base64
import hashlib
import io
import os
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

GENERATOR = "lit_build"
# Fixed fallback so identical sources give identical archives.
DEFAULT_EPOCH = 315532800  # 1980-01-01, the earliest zip timestamp


def _root():
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file():
        return cwd.resolve()
    return Path(__file__).resolve().parent.parent


def _epoch():
    value = int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH))
    return max(value, DEFAULT_EPOCH)


def _config():
    with (_root() / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    project = data["project"]
    tool = data.get("tool", {}).get("lit_build", {})
    name = project["name"]
    cfg = dict(
        name=name,
        norm=re.sub(r"[-_.]+", "_", name).lower(),
        version=project["version"],
        summary=project.get("description", ""),
        readme=project.get("readme"),
        requires_python=project.get("requires-python"),
        license=project.get("license"),
        scripts=project.get("scripts", {}),
        dependencies=project.get("dependencies", []),
        package=tool.get("package", "concurrent_merge"),
        package_data=tool.get("package-data", []),
    )
    return cfg


def _dist_info(cfg):
    return f"{cfg['norm']}-{cfg['version']}.dist-info"


def _license_files(cfg):
    names = []
    lic = cfg["license"]
    if isinstance(lic, dict) and "file" in lic:
        names.append(lic["file"])
    if (_root() / "NOTICE").is_file():
        names.append("NOTICE")
    return names


def _readme(cfg):
    readme = cfg["readme"]
    if isinstance(readme, dict):
        readme = readme.get("file")
    if not readme:
        return None, None
    path = _root() / readme
    text = path.read_text(encoding="utf-8")
    kind = "text/markdown" if path.suffix.lower() in (".md", ".markdown") else "text/plain"
    return text, kind


def _metadata(cfg):
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {cfg['name']}",
        f"Version: {cfg['version']}",
    ]
    if cfg["summary"]:
        lines.append(f"Summary: {cfg['summary']}")
    if cfg["requires_python"]:
        lines.append(f"Requires-Python: {cfg['requires_python']}")
    if isinstance(cfg["license"], dict) and "text" in cfg["license"]:
        lines.append(f"License: {cfg['license']['text']}")
    for name in _license_files(cfg):
        lines.append(f"License-File: {name}")
    for dep in cfg["dependencies"]:
        lines.append(f"Requires-Dist: {dep}")
    body, kind = _readme(cfg)
    if kind:
        lines.append(f"Description-Content-Type: {kind}")
    text = "\n".join(lines) + "\n"
    if body is not None:
        text += "\n" + body
    return text


def _wheel_file():
    return (
        "Wheel-Version: 1.0\n"
        f"Generator: {GENERATOR}\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )


def _entry_points(cfg):
    if not cfg["scripts"]:
        return None
    lines = ["[console_scripts]"]
    lines += [f"{k} = {v}" for k, v in cfg["scripts"].items()]
    return "\n".join(lines) + "\n"


def _package_files(cfg):
    """Relative posix paths: every .py module plus the declared package data."""
    pkg = _root() / cfg["package"]
    found = {p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts}
    for pattern in cfg["package_data"]:
        found.update(p for p in pkg.glob(pattern) if p.is_file())
    return sorted(p.relative_to(_root()).as_posix() for p in found)


def _hash(data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode()}"


def _zipinfo(name, epoch, mode=0o644):
    import time

    t = time.gmtime(epoch)[:6]
    info = zipfile.ZipInfo(name, date_time=t)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (mode | 0o100000) << 16
    return info


def _write_dist_info(cfg, add):
    info = _dist_info(cfg)
    add(f"{info}/METADATA", _metadata(cfg).encode())
    add(f"{info}/WHEEL", _wheel_file().encode())
    eps = _entry_points(cfg)
    if eps:
        add(f"{info}/entry_points.txt", eps.encode())
    for name in _license_files(cfg):
        add(f"{info}/licenses/{name}", (_root() / name).read_bytes())


def _build_wheel_zip(cfg, directory, extra=None, tag_suffix=""):
    """Write a wheel; `extra` maps archive names to bytes (used for editable)."""
    wheel_name = f"{cfg['norm']}-{cfg['version']}-py3-none-any.whl"
    path = Path(directory) / wheel_name
    epoch = _epoch()
    records = []
    with zipfile.ZipFile(path, "w") as zf:

        def add(name, data):
            zf.writestr(_zipinfo(name, epoch), data)
            records.append(f"{name},{_hash(data)},{len(data)}")

        if extra is None:
            for rel in _package_files(cfg):
                add(rel, (_root() / rel).read_bytes())
        else:
            for name, data in extra.items():
                add(name, data)
        _write_dist_info(cfg, add)
        record_name = f"{_dist_info(cfg)}/RECORD"
        records.append(f"{record_name},,")
        zf.writestr(_zipinfo(record_name, epoch), "\n".join(records) + "\n")
    return wheel_name


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_wheel_zip(_config(), wheel_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    cfg = _config()
    pth = f"{_root()}\n".encode()
    return _build_wheel_zip(cfg, wheel_directory, extra={f"__editable__.{cfg['norm']}.pth": pth})


def _prepare_metadata(metadata_directory):
    cfg = _config()
    target = Path(metadata_directory) / _dist_info(cfg)
    target.mkdir(parents=True, exist_ok=True)

    def add(name, data):
        out = Path(metadata_directory) / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)

    _write_dist_info(cfg, add)
    return _dist_info(cfg)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _prepare_metadata(metadata_directory)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _prepare_metadata(metadata_directory)


def _sdist_files(cfg):
    files = ["pyproject.toml", "README.md", "LICENSE", "NOTICE"]
    files = [f for f in files if (_root() / f).is_file()]
    skip = {"__pycache__", ".pytest_cache"}
    for top in ("build_backend", cfg["package"], "tests"):
        base = _root() / top
        if base.is_dir():
            files += [
                p.relative_to(_root()).as_posix()
                for p in base.rglob("*")
                if p.is_file() and not skip & set(p.parts) and p.suffix != ".pyc"
            ]
    return sorted(set(files))


def build_sdist(sdist_directory, config_settings=None):
    cfg = _config()
    base = f"{cfg['norm']}-{cfg['version']}"
    name = f"{base}.tar.gz"
    epoch = _epoch()

    def add(tar, arcname, data, mode=0o644):
        info = tarfile.TarInfo(f"{base}/{arcname}")
        info.size = len(data)
        info.mtime = epoch
        info.mode = mode
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(data))

    import gzip

    with open(Path(sdist_directory) / name, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel in _sdist_files(cfg):
                    add(tar, rel, (_root() / rel).read_bytes())
                pkg_info = _metadata(cfg).encode()
                add(tar, "PKG-INFO", pkg_info)
    return name
