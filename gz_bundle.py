#!/usr/bin/env python3
"""
gz_bundle — Gazebo SDF Bundler

Packs a .sdf world or model and all referenced assets (meshes, textures,
PBR maps, nested models, plugins) into a portable .sdfz archive
(ZIP_STORED, uncompressed).  URIs in the output SDF are rewritten to
relative paths.

Usage:
    python3 gz_bundle.py world.sdf -o forest3d.sdfz
    python3 gz_bundle.py models/robot/model.sdf -o robot.sdfz
    python3 gz_bundle.py --run forest3d.sdfz
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# --- URI resolution --------------------------------------------------------

def find_project_root(sdf_path: Path) -> Path:
    """Walk up from *sdf_path* to the first directory with a project marker."""
    markers = [
        ".git", "package.xml", "CMakeLists.txt",
        "setup.py", "colcon.pkg", ".ros2", "setup.cfg",
        "COLCON_IGNORE", "AMENT_IGNORE",
    ]
    current = sdf_path.resolve().parent
    while current != current.parent:
        if any((current / m).exists() for m in markers):
            return current
        current = current.parent
    return sdf_path.resolve().parent


def get_resource_paths(sdf_path: Path = None) -> list:
    """Auto-discover model/asset directories in the project tree.

    1. Find project root via marker files.
    2. Scan for directories named models/model/assets/meshes or containing
       model.sdf children.
    3. Honour GZ_SIM_RESOURCE_PATH / IGN_GAZEBO_RESOURCE_PATH as overrides.
    """
    paths = []

    if sdf_path:
        root = find_project_root(sdf_path)
        model_dir_names = {"models", "model", "assets", "meshes", "worlds", "resources"}
        skip = {"build", "install", "log", "__pycache__", ".git"}

        for d in root.rglob("*"):
            if not d.is_dir():
                continue
            if any(part.startswith(".") for part in d.parts):
                continue
            if any(part in skip for part in d.parts):
                continue

            if d.name.lower() in model_dir_names:
                if d not in paths:
                    paths.append(d)
                continue

            try:
                subdirs = [x for x in d.iterdir() if x.is_dir()]
                if any((sub / "model.sdf").exists() for sub in subdirs):
                    if d not in paths:
                        paths.append(d)
            except PermissionError:
                pass

    for var in ["GZ_SIM_RESOURCE_PATH", "IGN_GAZEBO_RESOURCE_PATH"]:
        for p in os.environ.get(var, "").split(":"):
            if p and Path(p) not in paths:
                paths.append(Path(p))

    return paths


def get_plugin_paths() -> list:
    """Collect plugin search directories from environment variables."""
    paths = []
    for var in ["GZ_SIM_SYSTEM_PLUGIN_PATH",
                "IGN_GAZEBO_SYSTEM_PLUGIN_PATH",
                "LD_LIBRARY_PATH"]:
        for p in os.environ.get(var, "").split(":"):
            if p:
                paths.append(Path(p))
    return paths


def resolve_file_uri(uri: str, sdf_dir: Path):
    """Resolve a file:// or relative URI to an absolute path."""
    uri = uri.strip()
    if uri.startswith("file://"):
        p = Path(uri[len("file://"):])
    elif uri.startswith(("http://", "https://")):
        return None
    else:
        p = Path(uri)
    if not p.is_absolute():
        p = sdf_dir / p
    return p if p.exists() else None


def resolve_plugin(filename: str, plugin_paths: list):
    """Find a plugin .so by filename across *plugin_paths*."""
    filename = filename.strip()
    candidates = [filename, f"lib{filename}.so", f"{filename}.so"]
    for pp in plugin_paths:
        for c in candidates:
            if (pp / c).exists():
                return pp / c
    return None


# --- SDF crawler -----------------------------------------------------------

class SDFCrawler:
    """Walk an SDF tree collecting every referenced asset."""

    def __init__(self, sdf_path: Path, verbose=False):
        self.root_sdf = sdf_path.resolve()
        self.sdf_dir = self.root_sdf.parent
        self.resource_paths = get_resource_paths(sdf_path)
        self.plugin_paths = get_plugin_paths()
        self.verbose = verbose

        self.assets = {}       # dest_relative_path -> absolute_source_path
        self.rewrites = {}     # original_uri -> new_relative_uri
        self.unresolved = []
        self._crawled = set()  # absolute paths already crawled (cycle guard)

    def log(self, msg):
        if self.verbose:
            print(f"  [bundle] {msg}")

    def warn(self, msg):
        print(f"  [WARN]   {msg}", file=sys.stderr)

    def collect(self):
        """Entry point — crawl the root SDF and all included SDFs."""
        self._crawl_sdf(self.root_sdf)

    def _crawl_sdf(self, sdf_path: Path):
        resolved = sdf_path.resolve()
        if resolved in self._crawled:
            return
        self._crawled.add(resolved)
        self.log(f"Crawling {sdf_path}")
        try:
            tree = ET.parse(sdf_path)
        except ET.ParseError as e:
            self.warn(f"Could not parse {sdf_path}: {e}")
            return

        root = tree.getroot()
        sdf_dir = sdf_path.parent

        # <include> tags — nested model SDFs
        for inc in root.iter("include"):
            uri_el = inc.find("uri")
            if uri_el is not None and uri_el.text:
                uri = uri_el.text.strip()
                resolved = self._resolve_any_uri(uri, sdf_dir)
                if resolved:
                    if resolved.is_dir():
                        self._add_directory(resolved, f"models/{resolved.name}")
                        self._register_rewrite(uri, f"model://{resolved.name}")
                    else:
                        dest = self._add_asset(resolved, "models")
                        self._register_rewrite(uri, dest)
                        self._crawl_sdf(resolved)
                else:
                    self.warn(f"Unresolved include URI: {uri}")
                    self.unresolved.append(uri)

        # <uri> tags — meshes, textures
        for uri_el in root.iter("uri"):
            if uri_el.text:
                uri = uri_el.text.strip()
                if uri.startswith("http"):
                    continue
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

        # <plugin filename="...">
        for plugin_el in root.iter("plugin"):
            fn = plugin_el.get("filename")
            if fn:
                # Skip gz-sim built-in plugins (ship with Gazebo)
                if fn.startswith(("gz-sim-", "ignition-gazebo-")):
                    self.log(f"Skipping built-in plugin: {fn}")
                    continue
                resolved = resolve_plugin(fn, self.plugin_paths)
                if resolved:
                    dest = self._add_asset(resolved, "plugins")
                    self._register_rewrite(fn, dest)
                    self.log(f"Plugin: {fn} -> {resolved}")
                else:
                    self.warn(f"Plugin not found (platform-specific?): {fn}")
                    self.unresolved.append(f"plugin:{fn}")

        # PBR texture maps
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

        # <texture> and <filename> inside <material>
        for tag in ["texture", "filename"]:
            for el in root.iter(tag):
                if el.text and el.text.strip():
                    uri = el.text.strip()
                    resolved = self._resolve_any_uri(uri, sdf_dir)
                    if resolved and resolved.is_file():
                        dest = self._add_asset(resolved, "materials/textures")
                        self._register_rewrite(uri, dest)

    def _resolve_any_uri(self, uri: str, sdf_dir: Path):
        """Try model://, file://, then relative."""
        if uri.startswith("model://"):
            model_name = uri[len("model://"):].split("/")[0]
            for rp in self.resource_paths:
                for prefix in [rp, rp / "models"]:
                    d = prefix / model_name
                    if d.exists():
                        sub = "/".join(uri[len("model://"):].split("/")[1:])
                        if sub:
                            full = d / sub
                            return full if full.exists() else None
                        return d
            return None
        return resolve_file_uri(uri, sdf_dir)

    def _guess_subdir(self, path: Path) -> str:
        """Pick a destination subdirectory based on file extension."""
        ext = path.suffix.lower()
        if ext in (".dae", ".obj", ".stl", ".fbx", ".gltf", ".glb"):
            return "models/meshes"
        if ext in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".hdr", ".exr"):
            return "materials/textures"
        if ext in (".sdf", ".config"):
            return "models"
        if ext in (".so", ".dll"):
            return "plugins"
        if ext in (".dem", ".tif", ".tiff"):
            return "terrain"
        return "assets"

    def _add_asset(self, src: Path, subdir: str) -> str:
        """Register a single file asset, return its relative dest path."""
        dest_rel = f"{subdir}/{src.name}"
        if dest_rel in self.assets and self.assets[dest_rel] != src:
            dest_rel = f"{subdir}/{src.parent.name}_{src.name}"
        self.assets[dest_rel] = src
        self.log(f"  + {dest_rel}")
        return dest_rel

    def _add_directory(self, src_dir: Path, dest_prefix: str):
        """Recursively register all files in a directory and crawl nested SDFs."""
        sdf_files = []
        for f in src_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_dir.parent)
                dest_rel = f"{dest_prefix}/{'/'.join(rel.parts[1:])}"
                self.assets[dest_rel] = f
                self.log(f"  + {dest_rel}")
                if f.suffix in (".sdf", ".world") and f != self.root_sdf:
                    sdf_files.append(f)
        for sdf_file in sdf_files:
            self._crawl_sdf(sdf_file)

    def _register_rewrite(self, original_uri: str, new_rel: str):
        self.rewrites[original_uri] = new_rel


def detect_sdf_type(sdf_path: Path) -> str:
    """Return 'model' if the SDF describes a model, otherwise 'world'."""
    try:
        tree = ET.parse(sdf_path)
        root = tree.getroot()
        if root.find("model") is not None and root.find("world") is None:
            return "model"
    except ET.ParseError:
        pass
    if sdf_path.name == "model.sdf":
        return "model"
    return "world"


def generate_wrapper_world(model_name: str) -> str:
    """Generate a minimal world SDF that includes a bundled model."""
    return textwrap.dedent(f"""\
        <?xml version="1.0"?>
        <sdf version="1.9">
          <world name="{model_name}_preview">
            <include>
              <uri>model://{model_name}</uri>
            </include>
            <light type="directional" name="sun">
              <cast_shadows>true</cast_shadows>
              <direction>-0.5 0.1 -0.9</direction>
            </light>
          </world>
        </sdf>
    """)


# --- SDF rewriter ----------------------------------------------------------

def rewrite_sdf(sdf_path: Path, rewrites: dict) -> str:
    """Read SDF as text and apply all URI rewrites."""
    text = sdf_path.read_text(encoding="utf-8")
    for original, new_rel in rewrites.items():
        text = text.replace(original, new_rel)
    return text


# --- Bundle writer ---------------------------------------------------------

def write_bundle(sdf_path: Path, crawler: SDFCrawler, output: Path,
                  bundle_type="world", model_name=None, verbose=False):
    """Write the .sdfz archive (ZIP_STORED, uncompressed)."""
    print(f"\nWriting bundle: {output}")

    main_py = textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Self-executing .sdfz bundle.  Run with: python3 <name>.sdfz\"\"\"
        import sys, os, shutil, subprocess, json, tempfile
        from pathlib import Path

        sdfz_path = Path(sys.argv[0]).resolve()
        print(f"gz_bundle — Gazebo Bundle Runner")
        print(f"Bundle : {sdfz_path}")

        mount_dir = Path(tempfile.mkdtemp(prefix="gz_bundle_"))
        used_fuse = False

        if shutil.which("fuse-zip"):
            rc = subprocess.run(
                ["fuse-zip", "-r", str(sdfz_path), str(mount_dir)],
                capture_output=True,
            ).returncode
            if rc == 0:
                used_fuse = True
                print(f"Mounted (fuse-zip, zero-copy) : {mount_dir}")

        if not used_fuse:
            import zipfile
            print("fuse-zip not found, extracting ...")
            with zipfile.ZipFile(sdfz_path, "r") as zf:
                zf.extractall(mount_dir)
            print(f"Extracted to : {mount_dir}")

        manifest_path = mount_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        bundle_type = manifest.get("type", "world")

        if bundle_type == "model":
            model_name = manifest.get("model_name", "model")
            world_sdf = mount_dir / "_preview_world.sdf"
            world_sdf.write_text(
                f'<?xml version="1.0"?>\\n'
                f'<sdf version="1.9">\\n'
                f'  <world name="{model_name}_preview">\\n'
                f'    <include><uri>model://{model_name}</uri></include>\\n'
                f'    <light type="directional" name="sun">\\n'
                f'      <cast_shadows>true</cast_shadows>\\n'
                f'      <direction>-0.5 0.1 -0.9</direction>\\n'
                f'    </light>\\n'
                f'  </world>\\n'
                f'</sdf>\\n'
            )
            print(f"Model bundle — generated preview world for '{model_name}'")
        else:
            world_sdf = mount_dir / manifest.get("world_sdf", "world.sdf")

        plugin_path   = str(mount_dir / "plugins")
        resource_path = f"{mount_dir}:{mount_dir / 'models'}"

        env = os.environ.copy()
        def prepend(key, val):
            env[key] = f"{val}:{env[key]}" if env.get(key) else val

        prepend("GZ_SIM_RESOURCE_PATH",         resource_path)
        prepend("IGN_GAZEBO_RESOURCE_PATH",      resource_path)
        prepend("GZ_SIM_SYSTEM_PLUGIN_PATH",     plugin_path)
        prepend("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", plugin_path)
        prepend("LD_LIBRARY_PATH",               plugin_path)

        subprocess.run(["pkill", "-f", "gz sim"], capture_output=True)
        subprocess.run(["pkill", "-f", "ruby.*gz"], capture_output=True)

        for cache_dir in ["~/.gz/sim", "~/.ignition/gazebo"]:
            p = Path(cache_dir).expanduser()
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                print(f"Cleared cache : {p}")

        print(f"Launching : gz sim {world_sdf}\\n")
        try:
            subprocess.run(["gz", "sim", str(world_sdf)] + sys.argv[1:], env=env)
        except FileNotFoundError:
            print("ERROR: 'gz' not found. Is Gazebo installed?", file=sys.stderr)
            sys.exit(1)
        finally:
            if used_fuse:
                subprocess.run(["fusermount", "-u", str(mount_dir)], capture_output=True)
            shutil.rmtree(mount_dir, ignore_errors=True)
    """)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("__main__.py", main_py)
        print(f"  + __main__.py (self-run bootstrap)")

        zf.write(Path(__file__).resolve(), "gz_bundle.py")
        print(f"  + gz_bundle.py (bundler embedded)")

        rewritten_sdf = rewrite_sdf(sdf_path, crawler.rewrites)
        if bundle_type == "model":
            sdf_entry = f"models/{model_name}/model.sdf"
            zf.writestr(sdf_entry, rewritten_sdf)
            print(f"  + {sdf_entry} ({len(rewritten_sdf)} bytes)")
        else:
            sdf_entry = "world.sdf"
            zf.writestr(sdf_entry, rewritten_sdf)
            print(f"  + world.sdf ({len(rewritten_sdf)} bytes)")

        ok = 0
        for dest_rel, src_path in crawler.assets.items():
            if dest_rel == sdf_entry:
                continue  # already written as rewritten SDF
            try:
                if src_path.suffix in (".sdf", ".world") and crawler.rewrites:
                    rewritten = rewrite_sdf(src_path, crawler.rewrites)
                    zf.writestr(dest_rel, rewritten)
                else:
                    zf.write(src_path, dest_rel)
                if verbose:
                    print(f"  + {dest_rel} ({src_path.stat().st_size // 1024} KB)")
                ok += 1
            except Exception as e:
                print(f"  ! {dest_rel}: {e}", file=sys.stderr)

        manifest = {
            "gz_bundle_version": "1.0",
            "type": bundle_type,
            "source_sdf": str(sdf_path),
            "assets": list(crawler.assets.keys()),
            "unresolved": crawler.unresolved,
            "rewrites": crawler.rewrites,
        }
        if bundle_type == "model":
            manifest["model_name"] = model_name
        else:
            manifest["world_sdf"] = "world.sdf"
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    total_size = output.stat().st_size
    print(f"\n  Assets packed : {ok}")
    print(f"  Unresolved    : {len(crawler.unresolved)}")
    print(f"  Bundle size   : {total_size // 1024} KB")
    if crawler.unresolved:
        print(f"\n  Unresolved URIs (manual check needed):")
        for u in crawler.unresolved:
            print(f"    - {u}")


# --- Bundle runner ---------------------------------------------------------

def run_bundle(sdfz_path: Path, extra_args: list):
    """Mount .sdfz via fuse-zip (zero-copy) or fall back to extraction."""
    mount_dir = Path(tempfile.mkdtemp(prefix="gz_bundle_"))
    used_fuse = False

    if shutil.which("fuse-zip"):
        rc = subprocess.run(
            ["fuse-zip", "-r", str(sdfz_path), str(mount_dir)],
            capture_output=True,
        ).returncode
        if rc == 0:
            used_fuse = True
            print(f"Mounted (fuse-zip, zero-copy) : {mount_dir}")

    if not used_fuse:
        print("fuse-zip not found, extracting ...")
        with zipfile.ZipFile(sdfz_path, "r") as zf:
            zf.extractall(mount_dir)
        print(f"Extracted to : {mount_dir}")

    manifest_path = mount_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    bundle_type = manifest.get("type", "world")

    if bundle_type == "model":
        model_name = manifest.get("model_name", "model")
        world_sdf = mount_dir / "_preview_world.sdf"
        world_sdf.write_text(generate_wrapper_world(model_name))
        print(f"Model bundle — generated preview world for '{model_name}'")
    else:
        world_sdf = mount_dir / manifest.get("world_sdf", "world.sdf")

    env = os.environ.copy()
    resource_path = f"{mount_dir}:{mount_dir / 'models'}"
    plugin_path = str(mount_dir / "plugins")

    def prepend_env(key, val):
        existing = env.get(key, "")
        env[key] = f"{val}:{existing}" if existing else val

    prepend_env("GZ_SIM_RESOURCE_PATH",         resource_path)
    prepend_env("IGN_GAZEBO_RESOURCE_PATH",      resource_path)
    prepend_env("GZ_SIM_SYSTEM_PLUGIN_PATH",     plugin_path)
    prepend_env("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", plugin_path)
    prepend_env("LD_LIBRARY_PATH",               plugin_path)

    subprocess.run(["pkill", "-f", "gz sim"], capture_output=True)
    subprocess.run(["pkill", "-f", "ruby.*gz"], capture_output=True)

    for cache_dir in ["~/.gz/sim", "~/.ignition/gazebo"]:
        p = Path(cache_dir).expanduser()
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"Cleared cache : {p}")

    print(f"Launching     : gz sim {world_sdf}")
    print(f"Resource path : {resource_path}")
    print(f"Plugin path   : {plugin_path}\n")

    try:
        subprocess.run(["gz", "sim", str(world_sdf)] + extra_args, env=env)
    except FileNotFoundError:
        print("ERROR: 'gz' not found. Is Gazebo installed?", file=sys.stderr)
        sys.exit(1)
    finally:
        if used_fuse:
            print(f"\nUnmounting {mount_dir}")
            subprocess.run(["fusermount", "-u", str(mount_dir)], capture_output=True)
        else:
            print(f"\nCleaning up {mount_dir}")
        shutil.rmtree(mount_dir, ignore_errors=True)


# --- CLI -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="gz_bundle — Pack a Gazebo SDF world or model into a portable .sdfz archive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 gz_bundle.py world.sdf -o forest3d.sdfz
              python3 gz_bundle.py models/robot/model.sdf -o robot.sdfz
              python3 gz_bundle.py --run forest3d.sdfz
              python3 gz_bundle.py --run robot.sdfz
        """),
    )
    parser.add_argument("sdf_or_bundle",
        help="Input .sdf file (pack mode) or .sdfz file (run mode)")
    parser.add_argument("-o", "--output", default=None,
        help="Output .sdfz path (default: <name>.sdfz)")
    parser.add_argument("--run", action="store_true",
        help="Run mode: mount/extract .sdfz and launch gz sim")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Print every resolved asset")

    args, extra = parser.parse_known_args()
    input_path = Path(args.sdf_or_bundle)

    # Run mode
    if args.run or input_path.suffix == ".sdfz":
        if not input_path.exists():
            print(f"ERROR: {input_path} not found", file=sys.stderr)
            sys.exit(1)
        run_bundle(input_path, [a for a in extra if a != "--"])
        return

    # Pack mode
    if not input_path.exists():
        print(f"ERROR: {input_path} not found", file=sys.stderr)
        sys.exit(1)
    if input_path.suffix not in (".sdf", ".world"):
        print(f"WARNING: {input_path} doesn't look like an SDF file", file=sys.stderr)

    bundle_type = detect_sdf_type(input_path)
    model_name = None

    if bundle_type == "model":
        model_name = input_path.resolve().parent.name
        default_output = input_path.resolve().parent.with_suffix(".sdfz")
    else:
        default_output = input_path.with_suffix(".sdfz")

    output = Path(args.output).expanduser().resolve() if args.output else default_output

    print(f"gz_bundle — Gazebo SDF Bundler")
    print(f"Type   : {bundle_type}")
    print(f"Input  : {input_path.resolve()}")
    if model_name:
        print(f"Model  : {model_name}")
    print(f"Output : {output}")

    discovered = get_resource_paths(input_path)
    if discovered:
        print(f"Resource paths:")
        for p in discovered:
            print(f"  {p}")
    else:
        print(f"Resource paths: (none found, set GZ_SIM_RESOURCE_PATH if needed)")
    print()

    crawler = SDFCrawler(input_path, verbose=args.verbose)
    crawler.collect()

    # For model bundles, also add the model's own directory
    if bundle_type == "model":
        model_dir = input_path.resolve().parent
        crawler._add_directory(model_dir, f"models/{model_name}")

    write_bundle(input_path, crawler, output,
                 bundle_type=bundle_type, model_name=model_name,
                 verbose=args.verbose)

    print(f"\nDone. Share {output.name} — run it with:")
    print(f"  python3 {output.name}")


if __name__ == "__main__":
    main()
