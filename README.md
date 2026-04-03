# gz-bundle

**Portable Gazebo world bundler — one file, zero setup.**

Pack any Gazebo SDF world + all its assets (meshes, PBR textures, models, plugins) into a single self-executable `.sdfz` file. Your colleague runs it with one command, no workspace setup required.

---

## The problem

Sharing a Gazebo world today means:
- Sending dozens of scattered files
- Setting `GZ_SIM_RESOURCE_PATH`, `GZ_SIM_SYSTEM_PLUGIN_PATH`
- Hoping the other person has the same directory layout

## The solution

```bash
# You — pack your world
python3 gz_bundle.py worlds/forest_world.world -o forest3d.sdfz

# Colleague — run it, zero setup
python3 forest3d.sdfz
```

That's it. One file. Any machine. Any workspace.

---

## What gets bundled

- SDF world file (URIs rewritten to relative paths)
- All referenced models (full directory trees)
- Meshes — `.dae`, `.obj`, `.stl`, `.glb`, `.gltf`
- PBR textures — albedo, normal, roughness, metalness, emissive maps
- Custom plugins — `.so` (best-effort, platform-specific)
- `gz_bundle.py` itself — embedded inside the bundle for repacking
- Manifest — `manifest.json` listing all assets and URI rewrites

---

## Requirements

**Packer (you):**
- Python 3.6+
- Gazebo installed (any version)

**Runner (colleague):**
- Python 3.6+
- Gazebo installed
- `fuse-zip` (optional, enables zero-copy mount instead of extraction)

No extra Python packages. Pure stdlib.

---

## Usage

### Pack a world

```bash
python3 gz_bundle.py worlds/my_world.sdf -o my_world.sdfz
```

Verbose mode (shows every asset packed):
```bash
python3 gz_bundle.py worlds/my_world.sdf -o my_world.sdfz --verbose
```

### Run a bundle

```bash
# Self-executing — no gz_bundle.py needed
python3 my_world.sdfz

# Or via gz_bundle.py
python3 gz_bundle.py --run my_world.sdfz
```

### Pass extra args to gz sim

```bash
python3 my_world.sdfz -- -v
```

---

## How it works

```
my_world.sdfz  (ZIP_STORED, uncompressed)
├── __main__.py          ← self-run bootstrap (python3 my_world.sdfz)
├── gz_bundle.py         ← bundler script embedded for repacking
├── world.sdf            ← rewritten SDF (all URIs → relative)
├── manifest.json        ← asset list + URI rewrite map
├── models/
│   ├── ground/
│   │   ├── model.sdf
│   │   └── mesh/
│   │       └── terrain.obj
│   └── Tree/
│       ├── model.sdf
│       └── mesh/
│           └── Tree.glb
├── materials/
│   └── textures/
│       ├── bark_albedo.png
│       └── bark_normal.png
└── plugins/
    └── libgz_terramechanics.so
```

The archive uses `ZIP_STORED` (uncompressed) so entries can be memory-mapped directly. At run time the bundle mounts via `fuse-zip` (zero-copy, read-only) or falls back to extraction if `fuse-zip` is not installed. Environment variables are set automatically and the mount/temp directory is cleaned up on exit.

---

## Auto-discovery

No `export GZ_SIM_RESOURCE_PATH` needed. The bundler automatically finds your models by:

1. Walking up from the SDF to find the project root (`.git`, `package.xml`, `CMakeLists.txt`, etc.)
2. Scanning the entire project tree for model collections
3. Honouring `GZ_SIM_RESOURCE_PATH` as an override if set

Works with any workspace layout — flat projects, ROS2 packages, multi-package workspaces.

---

## Known limitations

- **Plugins are platform-specific** — `.so` files compiled on Ubuntu x86_64 won't work on a different arch/distro. The recipient may need to recompile custom plugins.
- **Fuel `https://` URIs** are left as-is — Fuel models are fetched by gz-sim at runtime from the internet.
- **gz-sim built-in plugins** (`gz-sim-physics-system`, etc.) are not bundled — they ship with every Gazebo installation.

---

## Roadmap

### High priority
- [x] ~~Rename `.gzworld` to `.sdfz`~~
- [x] ~~ZIP_STORED uncompressed; fuse-zip mount, no `/tmp` extraction~~
- [ ] Accept world.sdf or model.sdf: same command, same tool
- [ ] Full URI rewrite on all SDF files inside the bundle
      (also fixes absolute paths from `generate_world_sdf` service)

### Coverage
- [ ] Heightmap support (`<heightmap><uri>` — `.png`, `.dem`, `.tif`)
- [ ] Actor animation support (`<actor><animation><filename>` — `.bvh`)
- [ ] Ogre `.material` script support

### Ecosystem
- [ ] `package://` URI support (ROS2 packages)
- [ ] Native `gz sim world.sdfz` support (upstream PR)

---

## Contributing

Issues and PRs welcome. This tool was born from a real research workflow pain point — if you hit edge cases with your world layout, open an issue with your SDF structure.

---

## License

MIT
