#!/usr/bin/env python3
"""
gz_bundle.py — Gazebo SDF World Bundler
========================================
Packs a .sdf world + all referenced assets (meshes, textures,
PBR maps, nested models, plugins) into a portable .gzworld zip archive.
URIs in the output SDF are rewritten to relative paths.

Usage:
    python3 gz_bundle.py world.sdf -o forest3d.gzworld
    python3 gz_bundle.py world.sdf -o forest3d.gzworld --verbose

Run a bundle:
    python3 gz_bundle.py --run forest3d.gzworld
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# URI resolution helpers
# ---------------------------------------------------------------------------

def find_project_root(sdf_path: Path) -> Path:
    """
    Walk up the directory tree to find the project root.
    Stops at the first directory containing a known project marker.
    Falls back to the SDF's parent directory.
    """
    markers = [
        ".git", "package.xml", "CMakeLists.txt",
        "setup.py", "colcon.pkg", ".ros2", "setup.cfg",
        "COLCON_IGNORE", "AMENT_IGNORE",
    ]
    current = sdf_path.resolve().parent
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return current
        current = current.parent
    return sdf_path.resolve().parent  # fallback


def get_resource_paths(sdf_path: Path = None) -> list:
    """
    Auto-discover ALL model/asset directories in the project.
    No GZ_SIM_RESOURCE_PATH export needed.

    Strategy:
      1. Find project root via marker files (.git, package.xml, CMakeLists.txt...)
      2. Scan the entire project tree for:
         - Directories named models/model/assets/meshes
         - Directories that directly contain model.sdf files (model collections)
      3. Honour GZ_SIM_RESOURCE_PATH / IGN_GAZEBO_RESOURCE_PATH as override
    """
    paths = []

    if sdf_path:
        root = find_project_root(sdf_path)

        # Known model directory names (case-insensitive)
        model_dir_names = {"models", "model", "assets", "meshes", "worlds", "resources"}

        for d in root.rglob("*"):
            if not d.is_dir():
                continue
            # Skip hidden dirs and common non-asset dirs
            if any(part.startswith(".") for part in d.parts):
                continue
            if any(part in {"build", "install", "log", "__pycache__", ".git"}
                   for part in d.parts):
                continue

            # Match by directory name
            if d.name.lower() in model_dir_names:
                if d not in paths:
                    paths.append(d)
                continue

            # Match by content: any dir that directly contains model.sdf files
            # means it's a model collection root (parent of model dirs)
            try:
                subdirs = [x for x in d.iterdir() if x.is_dir()]
                if any((sub / "model.sdf").exists() for sub in subdirs):
                    if d not in paths:
                        paths.append(d)
            except PermissionError:
                pass

    # Always honour explicit env vars as additional override
    for var in ["GZ_SIM_RESOURCE_PATH", "IGN_GAZEBO_RESOURCE_PATH"]:
        raw = os.environ.get(var, "")
        for p in raw.split(":"):
            if p:
                pp = Path(p)
                if pp not in paths:
                    paths.append(pp)

    return paths


def get_plugin_paths():
    """Return list of directories from GZ_SIM_SYSTEM_PLUGIN_PATH."""
    raw = os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "")
    paths = [Path(p) for p in raw.split(":") if p]
    raw2 = os.environ.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", "")
    paths += [Path(p) for p in raw2.split(":") if p]
    # Also LD_LIBRARY_PATH as fallback
    raw3 = os.environ.get("LD_LIBRARY_PATH", "")
    paths += [Path(p) for p in raw3.split(":") if p]
    return paths


def resolve_model_uri(uri: str, resource_paths: list, sdf_dir: Path):
    """
    Resolve a model:// URI to an absolute path.
    model://ModelName/meshes/foo.dae
    → search resource_paths for a directory named ModelName
    """
    uri = uri.strip()
    if uri.startswith("model://"):
        rel = uri[len("model://"):]          # e.g. "archimede/meshes/chassis.dae"
        model_name = rel.split("/")[0]
        sub_path   = "/".join(rel.split("/")[1:])  # e.g. "meshes/chassis.dae"
        for rp in resource_paths:
            candidate = rp / model_name / sub_path
            if candidate.exists():
                return candidate
            # some layouts have models/ sub-dir
            candidate2 = rp / "models" / model_name / sub_path
            if candidate2.exists():
                return candidate2
    return None


def resolve_file_uri(uri: str, sdf_dir: Path):
    """Resolve a file:// or relative URI to an absolute path."""
    uri = uri.strip()
    if uri.startswith("file://"):
        p = Path(uri[len("file://"):])
    elif uri.startswith("http://") or uri.startswith("https://"):
        return None  # Fuel / remote — skip
    else:
        # plain relative path
        p = Path(uri)
    if not p.is_absolute():
        p = sdf_dir / p
    return p if p.exists() else None


def resolve_plugin(filename: str, plugin_paths: list):
    """Find a plugin .so by filename."""
    filename = filename.strip()
    # Strip lib prefix / .so suffix ambiguity
    candidates = [filename, f"lib{filename}.so", f"{filename}.so"]
    for pp in plugin_paths:
        for c in candidates:
            candidate = pp / c
            if candidate.exists():
                return candidate
    return None


# ---------------------------------------------------------------------------
# SDF crawler
# ---------------------------------------------------------------------------

class SDFCrawler:
    def __init__(self, sdf_path: Path, verbose=False):
        self.root_sdf   = sdf_path.resolve()
        self.sdf_dir    = self.root_sdf.parent
        self.resource_paths = get_resource_paths(sdf_path)
        self.plugin_paths   = get_plugin_paths()
        self.verbose        = verbose

        # collected assets: { dest_relative_path: absolute_source_path }
        self.assets = {}
        # URI rewrites: { original_uri: new_relative_uri }
        self.rewrites = {}
        # warnings
        self.unresolved = []

    def log(self, msg):
        if self.verbose:
            print(f"  [bundle] {msg}")

    def warn(self, msg):
        print(f"  [WARN]   {msg}", file=sys.stderr)

    def collect(self):
        """Entry point — crawl the root SDF and all included SDFs."""
        self._crawl_sdf(self.root_sdf)

    def _crawl_sdf(self, sdf_path: Path):
        self.log(f"Crawling {sdf_path}")
        try:
            tree = ET.parse(sdf_path)
        except ET.ParseError as e:
            self.warn(f"Could not parse {sdf_path}: {e}")
            return

        root = tree.getroot()
        sdf_dir = sdf_path.parent

        # --- <include> tags (nested model SDFs) ---
        for inc in root.iter("include"):
            uri_el = inc.find("uri")
            if uri_el is not None and uri_el.text:
                uri = uri_el.text.strip()
                resolved = self._resolve_any_uri(uri, sdf_dir)
                if resolved:
                    # If URI points to a model directory, grab the whole dir
                    if resolved.is_dir():
                        self._add_directory(resolved, f"models/{resolved.name}")
                        new_uri = f"model://{resolved.name}"
                        self._register_rewrite(uri, new_uri)
                        # NOTE: do NOT crawl model.sdf — _add_directory already
                        # packed everything including meshes with correct relative paths
                    else:
                        dest = self._add_asset(resolved, "models")
                        self._register_rewrite(uri, dest)
                        self._crawl_sdf(resolved)
                else:
                    self.warn(f"Unresolved include URI: {uri}")
                    self.unresolved.append(uri)

        # --- <uri> tags (meshes, PBR textures) ---
        for uri_el in root.iter("uri"):
            if uri_el.text:
                uri = uri_el.text.strip()
                if uri.startswith("http"):
                    continue  # Fuel — leave as-is
                resolved = self._resolve_any_uri(uri, sdf_dir)
                if resolved:
                    if resolved.is_dir():
                        self._add_directory(resolved, f"models/{resolved.name}")
                        self._register_rewrite(uri, f"model://{resolved.name}")
                    else:
                        dest = self._add_asset(resolved, self._guess_subdir(resolved))
                        self._register_rewrite(uri, dest)
                else:
                    self.warn(f"Unresolved URI: {uri}")
                    self.unresolved.append(uri)

        # --- <plugin filename="..."> ---
        for plugin_el in root.iter("plugin"):
            fn = plugin_el.get("filename")
            if fn:
                resolved = resolve_plugin(fn, self.plugin_paths)
                if resolved:
                    dest = self._add_asset(resolved, "plugins")
                    self._register_rewrite(fn, dest)
                    self.log(f"Plugin: {fn} → {resolved}")
                else:
                    self.warn(f"Plugin not found (platform-specific?): {fn}")
                    self.unresolved.append(f"plugin:{fn}")

        # --- PBR texture paths inside <pbr> ---
        pbr_tags = ["albedo_map", "normal_map", "roughness_map",
                    "metalness_map", "emissive_map", "light_map",
                    "roughness_metalness_map"]
        for tag in pbr_tags:
            for el in root.iter(tag):
                if el.text and el.text.strip():
                    uri = el.text.strip()
                    resolved = self._resolve_any_uri(uri, sdf_dir)
                    if resolved and resolved.is_file():
                        dest = self._add_asset(resolved, "materials/textures")
                        self._register_rewrite(uri, dest)
                    elif not uri.startswith("http"):
                        self.warn(f"Unresolved texture <{tag}>: {uri}")
                        self.unresolved.append(uri)

        # --- plain <texture> and <filename> inside <material> ---
        for tag in ["texture", "filename"]:
            for el in root.iter(tag):
                if el.text and el.text.strip():
                    uri = el.text.strip()
                    resolved = self._resolve_any_uri(uri, sdf_dir)
                    if resolved and resolved.is_file():
                        dest = self._add_asset(resolved, "materials/textures")
                        self._register_rewrite(uri, dest)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _resolve_any_uri(self, uri: str, sdf_dir: Path):
        """Try model://, file://, relative."""
        if uri.startswith("model://"):
            # model://ModelName  → directory
            model_name = uri[len("model://"):].split("/")[0]
            for rp in self.resource_paths:
                for subdir in ["", "models/"]:
                    d = rp / subdir / model_name if subdir else rp / model_name
                    if d.exists():
                        # full sub-path
                        sub = "/".join(uri[len("model://"):].split("/")[1:])
                        if sub:
                            full = d / sub
                            return full if full.exists() else None
                        return d
            return None
        return resolve_file_uri(uri, sdf_dir)

    def _guess_subdir(self, path: Path):
        """Guess destination subdirectory from file extension."""
        ext = path.suffix.lower()
        if ext in [".dae", ".obj", ".stl", ".fbx", ".gltf", ".glb"]:
            return "models/meshes"
        if ext in [".png", ".jpg", ".jpeg", ".tga", ".bmp", ".hdr", ".exr"]:
            return "materials/textures"
        if ext in [".sdf", ".config"]:
            return "models"
        if ext in [".so", ".dll"]:
            return "plugins"
        if ext in [".dem", ".tif", ".tiff", ".png"]:  # terrain
            return "terrain"
        return "assets"

    def _add_asset(self, src: Path, subdir: str) -> str:
        """Register a single file asset, return its relative dest path."""
        dest_rel = f"{subdir}/{src.name}"
        # avoid collisions: if same name different source, use hash prefix
        if dest_rel in self.assets and self.assets[dest_rel] != src:
            dest_rel = f"{subdir}/{src.parent.name}_{src.name}"
        self.assets[dest_rel] = src
        self.log(f"  + {dest_rel}")
        return dest_rel

    def _add_directory(self, src_dir: Path, dest_prefix: str):
        """Recursively register all files in a directory."""
        for f in src_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_dir.parent)
                dest_rel = f"{dest_prefix}/{'/'.join(rel.parts[1:])}"
                self.assets[dest_rel] = f
                self.log(f"  + {dest_rel}")

    def _register_rewrite(self, original_uri: str, new_rel: str):
        self.rewrites[original_uri] = new_rel


# ---------------------------------------------------------------------------
# SDF rewriter
# ---------------------------------------------------------------------------

def rewrite_sdf(sdf_path: Path, rewrites: dict) -> str:
    """Read SDF as text and apply all URI rewrites. Returns new XML string."""
    text = sdf_path.read_text(encoding="utf-8")
    for original, new_rel in rewrites.items():
        if original in text:
            text = text.replace(original, new_rel)
    return text


# ---------------------------------------------------------------------------
# Bundle writer
# ---------------------------------------------------------------------------

def write_bundle(sdf_path: Path, crawler: SDFCrawler, output: Path, verbose=False):
    """Write the .gzworld zip archive."""
    print(f"\nWriting bundle: {output}")

    # __main__.py — makes the .gzworld self-executable via: python3 forest3d.gzworld
    # Python adds the zip itself to sys.path when run as __main__, so we can
    # import gz_bundle directly from inside the archive.
    main_py = textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"
        Self-executing .gzworld bundle.
        Run with:  python3 forest3d.gzworld
        \"\"\"
        import sys, os, zipfile, tempfile, shutil, subprocess, json
        from pathlib import Path

        # The zip file being executed is sys.argv[0]
        gzworld_path = Path(sys.argv[0])

        print(f"gz_bundle — Gazebo World Bundle Runner")
        print(f"Bundle : {gzworld_path}")
        print(f"Unpacking ...")

        extract_dir = Path(tempfile.mkdtemp(prefix="gz_bundle_"))

        with zipfile.ZipFile(gzworld_path, "r") as zf:
            zf.extractall(extract_dir)

        manifest_path = extract_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        world_sdf   = extract_dir / manifest.get("world_sdf", "world.sdf")
        plugin_path = str(extract_dir / "plugins")
        # models/ subdir must be in resource path so model://X resolves to models/X/
        resource_path = f"{extract_dir}:{extract_dir / 'models'}"

        env = os.environ.copy()
        def prepend(key, val):
            env[key] = f"{val}:{env[key]}" if env.get(key) else val

        prepend("GZ_SIM_RESOURCE_PATH",         resource_path)
        prepend("IGN_GAZEBO_RESOURCE_PATH",      resource_path)
        prepend("GZ_SIM_SYSTEM_PLUGIN_PATH",     plugin_path)
        prepend("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", plugin_path)
        prepend("LD_LIBRARY_PATH",               plugin_path)

        # Kill any stale gz-sim processes (they cache resource paths in memory)
        import subprocess as _sp
        _sp.run(["pkill", "-f", "gz sim"], capture_output=True)
        _sp.run(["pkill", "-f", "ruby.*gz"], capture_output=True)

        # Clear gz-sim file cache BEFORE launching
        import shutil as _shutil
        for cache_dir in ["~/.gz/sim", "~/.ignition/gazebo"]:
            p = Path(cache_dir).expanduser()
            if p.exists():
                _shutil.rmtree(p, ignore_errors=True)
                print(f"Cleared cache : {p}")

        print(f"Extracted to  : {extract_dir}")
        print(f"Launching     : gz sim {world_sdf}\\n")

        extra = sys.argv[1:]   # forward any extra args to gz sim
        try:
            subprocess.run(["gz", "sim", str(world_sdf)] + extra, env=env)
        except FileNotFoundError:
            print("ERROR: 'gz' not found. Is Gazebo installed?", file=sys.stderr)
            sys.exit(1)
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
    """)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:

        # 0. __main__.py — enables: python3 forest3d.gzworld
        zf.writestr("__main__.py", main_py)
        print(f"  ✓ __main__.py (self-run bootstrap)")

        # 1. Embed gz_bundle.py itself — colleague can extract and reuse
        self_path = Path(__file__).resolve()
        zf.write(self_path, "gz_bundle.py")
        print(f"  ✓ gz_bundle.py (bundler script embedded)")

        # 2. Rewritten world.sdf
        rewritten_sdf = rewrite_sdf(sdf_path, crawler.rewrites)
        zf.writestr("world.sdf", rewritten_sdf)
        print(f"  ✓ world.sdf (rewritten, {len(rewritten_sdf)} bytes)")

        # 3. All collected assets
        ok = 0
        for dest_rel, src_path in crawler.assets.items():
            try:
                zf.write(src_path, dest_rel)
                if verbose:
                    size = src_path.stat().st_size
                    print(f"  ✓ {dest_rel} ({size//1024} KB)")
                ok += 1
            except Exception as e:
                print(f"  ✗ {dest_rel}: {e}", file=sys.stderr)

        # 4. Manifest
        import json
        manifest = {
            "gz_bundle_version": "1.0",
            "world_sdf": "world.sdf",
            "source_sdf": str(sdf_path),
            "assets": list(crawler.assets.keys()),
            "unresolved": crawler.unresolved,
            "rewrites": crawler.rewrites,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    total_size = output.stat().st_size
    print(f"\n  Assets packed : {ok}")
    print(f"  Unresolved    : {len(crawler.unresolved)}")
    print(f"  Bundle size   : {total_size // 1024} KB")
    if crawler.unresolved:
        print(f"\n  Unresolved URIs (manual check needed):")
        for u in crawler.unresolved:
            print(f"    - {u}")


# ---------------------------------------------------------------------------
# Bundle runner
# ---------------------------------------------------------------------------

def run_bundle(gzworld_path: Path, extra_args: list):
    """Extract .gzworld to a temp dir and launch gz sim."""
    import json
    import subprocess
    extract_dir = Path(tempfile.mkdtemp(prefix="gz_bundle_"))

    with zipfile.ZipFile(gzworld_path, "r") as zf:
        zf.extractall(extract_dir)

    manifest_path = extract_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    world_sdf = extract_dir / manifest.get("world_sdf", "world.sdf")

    # Set env vars
    env = os.environ.copy()
    # models/ subdir must be in resource path so model://X resolves to models/X/
    resource_path = f"{extract_dir}:{extract_dir / 'models'}"
    plugin_path   = str(extract_dir / "plugins")

    def prepend_env(key, val):
        existing = env.get(key, "")
        env[key] = f"{val}:{existing}" if existing else val

    prepend_env("GZ_SIM_RESOURCE_PATH",        resource_path)
    prepend_env("IGN_GAZEBO_RESOURCE_PATH",     resource_path)
    prepend_env("GZ_SIM_SYSTEM_PLUGIN_PATH",    plugin_path)
    prepend_env("IGN_GAZEBO_SYSTEM_PLUGIN_PATH",plugin_path)
    prepend_env("LD_LIBRARY_PATH",              plugin_path)

    import subprocess
    # Kill any stale gz-sim processes (they cache resource paths in memory)
    subprocess.run(["pkill", "-f", "gz sim"], capture_output=True)
    subprocess.run(["pkill", "-f", "ruby.*gz"], capture_output=True)

    # Clear gz-sim file cache BEFORE launching
    for cache_dir in ["~/.gz/sim", "~/.ignition/gazebo"]:
        p = Path(cache_dir).expanduser()
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"Cleared cache : {p}")

    print(f"Extracted to  : {extract_dir}")
    print(f"Launching     : gz sim {world_sdf}")
    print(f"Resource path : {resource_path}")
    print(f"Plugin path   : {plugin_path}\n")

    cmd = ["gz", "sim", str(world_sdf)] + extra_args
    try:
        subprocess.run(cmd, env=env)
    except FileNotFoundError:
        print("ERROR: 'gz' not found. Is Gazebo installed?", file=sys.stderr)
        sys.exit(1)
    finally:
        print(f"\nCleaning up {extract_dir}")
        shutil.rmtree(extract_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="gz_bundle — Pack a Gazebo SDF world into a portable .gzworld archive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Pack
  python3 gz_bundle.py world.sdf -o forest3d.gzworld
  python3 gz_bundle.py world.sdf -o forest3d.gzworld --verbose

  # Run
  python3 gz_bundle.py --run forest3d.gzworld
  python3 gz_bundle.py --run forest3d.gzworld -- -v   # pass extra args to gz sim
        """
    )

    parser.add_argument("sdf_or_bundle",
        help="Input .sdf file (pack mode) or .gzworld file (run mode)")
    parser.add_argument("-o", "--output", default=None,
        help="Output .gzworld path (pack mode, default: <sdf_name>.gzworld)")
    parser.add_argument("--run", action="store_true",
        help="Run mode: unpack .gzworld and launch gz sim")
    parser.add_argument("--verbose", "-v", action="store_true",
        help="Print every resolved asset")

    args, extra_unknown = parser.parse_known_args()
    args.extra = extra_unknown
    input_path = Path(args.sdf_or_bundle)

    # --- RUN MODE ---
    if args.run or input_path.suffix == ".gzworld":
        if not input_path.exists():
            print(f"ERROR: {input_path} not found", file=sys.stderr)
            sys.exit(1)
        extra = [a for a in args.extra if a != "--"]
        run_bundle(input_path, extra)
        return

    # --- PACK MODE ---
    if not input_path.exists():
        print(f"ERROR: {input_path} not found", file=sys.stderr)
        sys.exit(1)
    if input_path.suffix not in [".sdf", ".world"]:
        print(f"WARNING: {input_path} doesn't look like an SDF file", file=sys.stderr)

    output = Path(args.output).expanduser().resolve() if args.output else input_path.with_suffix(".gzworld")

    print(f"gz_bundle — Gazebo SDF World Bundler")
    print(f"Input  : {input_path.resolve()}")
    print(f"Output : {output}")

    discovered = get_resource_paths(input_path)
    if discovered:
        print(f"Auto-discovered resource paths:")
        for p in discovered:
            print(f"  → {p}")
    else:
        print(f"Resource paths: (none found — set GZ_SIM_RESOURCE_PATH if needed)")
    print()

    crawler = SDFCrawler(input_path, verbose=args.verbose)
    crawler.collect()
    write_bundle(input_path, crawler, output, verbose=args.verbose)

    print(f"\nDone. Share {output} — your colleague only needs ONE file:")
    print(f"  python3 {output}")


if __name__ == "__main__":
    main()
