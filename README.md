# gz-bundle

**Portable Gazebo SDF bundler. One file, zero setup.**

Pack a Gazebo SDF world or model with all its assets into a single `.sdfz` file.

```bash
# Pack
python3 gz_bundle.py world.sdf -o forest3d.sdfz
python3 gz_bundle.py models/robot/model.sdf -o robot.sdfz

# Run
python3 forest3d.sdfz
```

## World bundling


https://github.com/user-attachments/assets/1abb0b45-c482-4da3-a052-a6ad87a5d15e



## Model bundling


https://github.com/user-attachments/assets/3190fa71-d172-4a46-b2b3-1e9bc9fb9515


 

## Requirements

- Python 3.6+ (stdlib only, no pip packages)
- Gazebo installed
- `fuse-zip` (optional — enables zero-copy mount instead of /tmp extraction)

```bash
sudo apt install fuse-zip   # optional
```

## Usage

```bash
# Pack a world
python3 gz_bundle.py worlds/my_world.sdf -o my_world.sdfz

# Pack a model

https://github.com/user-attachments/assets/4ecbef38-d5dd-434e-bbe9-952eaea58563


python3 gz_bundle.py models/tree/model.sdf -o tree.sdfz

# Run (self-executing, no gz_bundle.py needed)
python3 my_world.sdfz

# Verbose (shows every asset packed)
python3 gz_bundle.py worlds/my_world.sdf -o my_world.sdfz -v

# Pass extra args to gz sim
python3 my_world.sdfz -- -v
```

## What gets bundled

```
my_world.sdfz  (ZIP_STORED, uncompressed)
├── world.sdf            ← rewritten SDF (URIs → relative)
├── __main__.py           ← self-run bootstrap
├── gz_bundle.py          ← bundler embedded for repacking
├── manifest.json
├── models/
│   ├── ground/
│   │   ├── model.sdf
│   │   └── mesh/terrain.obj
│   └── Tree/
│       ├── model.sdf
│       └── mesh/Tree.glb
├── materials/textures/
│   ├── bark_albedo.png
│   └── bark_normal.png
└── plugins/
    └── libgz_terramechanics.so
```

- Meshes (`.dae`, `.obj`, `.stl`, `.glb`, `.gltf`)
- PBR textures (albedo, normal, roughness, metalness, emissive)
- Nested models (full directory trees, recursive)
- Custom plugins (`.so`, platform-specific)
- Model bundles generate a preview world automatically when run

## Auto-discovery

No `export GZ_SIM_RESOURCE_PATH` needed. The bundler finds models by walking up to the project root (`.git`, `package.xml`, `CMakeLists.txt`) and scanning the tree. Works with flat projects, ROS2 packages, and multi-package workspaces.

## Known limitations

- **Plugins are platform-specific**, which means `.so` won't work elsewhere (to be arranged)
- **Fuel URIs** (`https://fuel.gazebosim.org/...`) are left as-is and fetched by gz-sim at runtime. 
- **Built-in plugins** (`gz-sim-physics-system`, etc.) ship with gazebo

## Roadmap

- [x] ~~Rename `.gzworld` to `.sdfz`~~
- [x] ~~ZIP_STORED uncompressed; fuse-zip mount~~
- [x] World + model bundling
- [x] ~~URI rewriting in root SDF~~
- [ ] Heightmap support (`<heightmap><uri>`)
- [ ] Actor animation support (`<actor><animation><filename>`)
- [ ] `package://` URI support (ROS2)
- [ ] Native `gz sim world.sdfz` (upstream PR)

## Contributing

Issues and PRs welcome.

## License

MIT
