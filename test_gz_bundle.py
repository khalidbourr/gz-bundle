#!/usr/bin/env python3
"""Tests for gz_bundle — URI resolution, SDF crawling, and rewriting."""

import os
import tempfile
from pathlib import Path

import pytest

from gz_bundle import (
    find_project_root,
    get_resource_paths,
    resolve_file_uri,
    resolve_plugin,
    rewrite_sdf,
    SDFCrawler,
)


@pytest.fixture
def project(tmp_path):
    """Create a minimal project tree with a .git marker and model layout."""
    (tmp_path / ".git").mkdir()
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    sdf = worlds / "test.sdf"
    sdf.write_text("<sdf/>")

    models = tmp_path / "models"
    models.mkdir()
    car = models / "car"
    car.mkdir()
    (car / "model.sdf").write_text("<sdf/>")
    meshes = car / "meshes"
    meshes.mkdir()
    (meshes / "chassis.dae").write_text("")

    return tmp_path, sdf


# --- find_project_root -----------------------------------------------------

class TestFindProjectRoot:
    def test_finds_git_root(self, project):
        root, sdf = project
        assert find_project_root(sdf) == root

    def test_finds_package_xml(self, tmp_path):
        (tmp_path / "package.xml").write_text("<package/>")
        sdf = tmp_path / "src" / "worlds" / "w.sdf"
        sdf.parent.mkdir(parents=True)
        sdf.write_text("<sdf/>")
        assert find_project_root(sdf) == tmp_path

    def test_falls_back_to_parent(self, tmp_path):
        sdf = tmp_path / "lonely" / "world.sdf"
        sdf.parent.mkdir()
        sdf.write_text("<sdf/>")
        assert find_project_root(sdf) == sdf.parent


# --- get_resource_paths -----------------------------------------------------

class TestGetResourcePaths:
    def test_discovers_models_dir(self, project):
        root, sdf = project
        paths = get_resource_paths(sdf)
        assert root / "models" in paths

    def test_discovers_model_collection(self, tmp_path):
        """A dir whose children contain model.sdf should be discovered."""
        (tmp_path / ".git").mkdir()
        collection = tmp_path / "my_robots"
        bot = collection / "bot1"
        bot.mkdir(parents=True)
        (bot / "model.sdf").write_text("<sdf/>")
        sdf = tmp_path / "w.sdf"
        sdf.write_text("<sdf/>")
        paths = get_resource_paths(sdf)
        assert collection in paths

    def test_skips_build_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        build_models = tmp_path / "build" / "models"
        build_models.mkdir(parents=True)
        sdf = tmp_path / "w.sdf"
        sdf.write_text("<sdf/>")
        paths = get_resource_paths(sdf)
        assert build_models not in paths

    def test_honours_env_var(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra_models"
        extra.mkdir()
        monkeypatch.setenv("GZ_SIM_RESOURCE_PATH", str(extra))
        paths = get_resource_paths()
        assert extra in paths

    def test_no_duplicates(self, project, monkeypatch):
        root, sdf = project
        monkeypatch.setenv("GZ_SIM_RESOURCE_PATH", str(root / "models"))
        paths = get_resource_paths(sdf)
        models_entries = [p for p in paths if p == root / "models"]
        assert len(models_entries) == 1


# --- resolve_file_uri ------------------------------------------------------

class TestResolveFileUri:
    def test_relative_path(self, tmp_path):
        f = tmp_path / "meshes" / "box.dae"
        f.parent.mkdir()
        f.write_text("")
        result = resolve_file_uri("meshes/box.dae", tmp_path)
        assert result == f

    def test_file_scheme(self, tmp_path):
        f = tmp_path / "mesh.obj"
        f.write_text("")
        result = resolve_file_uri(f"file://{f}", tmp_path)
        assert result == f

    def test_http_returns_none(self, tmp_path):
        assert resolve_file_uri("https://fuel.gazebosim.org/model.sdf", tmp_path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert resolve_file_uri("no_such_file.dae", tmp_path) is None

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "m.dae"
        f.write_text("")
        assert resolve_file_uri("  m.dae  ", tmp_path) == f


# --- resolve_plugin --------------------------------------------------------

class TestResolvePlugin:
    def test_exact_filename(self, tmp_path):
        so = tmp_path / "libMyPlugin.so"
        so.write_text("")
        assert resolve_plugin("libMyPlugin.so", [tmp_path]) == so

    def test_adds_lib_prefix_and_so(self, tmp_path):
        so = tmp_path / "libMyPlugin.so"
        so.write_text("")
        assert resolve_plugin("MyPlugin", [tmp_path]) == so

    def test_adds_so_suffix(self, tmp_path):
        so = tmp_path / "MyPlugin.so"
        so.write_text("")
        assert resolve_plugin("MyPlugin", [tmp_path]) == so

    def test_not_found(self, tmp_path):
        assert resolve_plugin("ghost", [tmp_path]) is None

    def test_searches_multiple_paths(self, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        so = d2 / "libFoo.so"
        so.write_text("")
        assert resolve_plugin("Foo", [d1, d2]) == so


# --- SDFCrawler ------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    """Prevent host env vars from leaking into crawler tests."""
    monkeypatch.delenv("GZ_SIM_RESOURCE_PATH", raising=False)
    monkeypatch.delenv("IGN_GAZEBO_RESOURCE_PATH", raising=False)
    monkeypatch.delenv("GZ_SIM_SYSTEM_PLUGIN_PATH", raising=False)
    monkeypatch.delenv("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", raising=False)


@pytest.fixture
def crawl_project(tmp_path, clean_env):
    """Build a project with world.sdf that includes a model, mesh, plugin, and PBR textures."""
    (tmp_path / ".git").mkdir()

    # models/robot with model.sdf and a mesh
    robot = tmp_path / "models" / "robot"
    robot.mkdir(parents=True)
    (robot / "model.sdf").write_text("<sdf/>")
    mesh_dir = robot / "meshes"
    mesh_dir.mkdir()
    (mesh_dir / "body.dae").write_text("")

    # a standalone mesh referenced by relative path
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    (worlds / "floor.obj").write_text("")

    # PBR textures (relative to worlds/ dir)
    textures = tmp_path / "materials" / "textures"
    textures.mkdir(parents=True)
    (textures / "brick_albedo.png").write_text("")
    (textures / "brick_normal.png").write_text("")

    # plugin
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "libSensor.so").write_text("")

    # world SDF referencing everything via relative paths
    world_sdf = worlds / "test_world.sdf"
    world_sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="test">
    <include>
      <uri>model://robot</uri>
    </include>
    <model name="ground">
      <link name="link">
        <visual name="v">
          <geometry>
            <mesh><uri>floor.obj</uri></mesh>
          </geometry>
          <material>
            <pbr>
              <metal>
                <albedo_map>../materials/textures/brick_albedo.png</albedo_map>
                <normal_map>../materials/textures/brick_normal.png</normal_map>
              </metal>
            </pbr>
          </material>
        </visual>
      </link>
    </model>
    <plugin filename="Sensor" name="sensor_sys"/>
  </world>
</sdf>
""")

    return tmp_path, world_sdf


class TestSDFCrawlerInclude:
    def test_collects_included_model_directory(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        dest_paths = list(crawler.assets.keys())
        assert any("robot" in p and "model.sdf" in p for p in dest_paths)
        assert any("body.dae" in p for p in dest_paths)

    def test_rewrites_model_uri(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert "model://robot" in crawler.rewrites


class TestSDFCrawlerUri:
    def test_collects_relative_mesh(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert any("floor.obj" in p for p in crawler.assets)

    def test_skips_http_uris_in_mesh(self, tmp_path, clean_env):
        (tmp_path / ".git").mkdir()
        sdf = tmp_path / "w.sdf"
        sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="w">
    <model name="m"><link name="l"><visual name="v"><geometry>
      <mesh><uri>https://fuel.gazebosim.org/mesh.dae</uri></mesh>
    </geometry></visual></link></model>
  </world>
</sdf>
""")
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert len(crawler.assets) == 0
        assert len(crawler.unresolved) == 0


class TestSDFCrawlerPBR:
    def test_collects_pbr_textures_via_relative_path(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        tex_assets = [p for p in crawler.assets if "textures" in p]
        assert any("brick_albedo.png" in p for p in tex_assets)
        assert any("brick_normal.png" in p for p in tex_assets)


class TestSDFCrawlerPlugin:
    def test_collects_plugin(self, crawl_project, monkeypatch):
        root, sdf = crawl_project
        monkeypatch.setenv("GZ_SIM_SYSTEM_PLUGIN_PATH", str(root / "plugins"))
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert any("libSensor.so" in p for p in crawler.assets)

    def test_unresolved_plugin_warned(self, tmp_path, clean_env):
        (tmp_path / ".git").mkdir()
        sdf = tmp_path / "w.sdf"
        sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="w">
    <plugin filename="GhostPlugin" name="ghost"/>
  </world>
</sdf>
""")
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert any("GhostPlugin" in u for u in crawler.unresolved)


class TestSDFCrawlerUnresolved:
    def test_unresolved_include_tracked(self, tmp_path, clean_env):
        (tmp_path / ".git").mkdir()
        sdf = tmp_path / "w.sdf"
        sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="w">
    <include>
      <uri>model://nonexistent_model</uri>
    </include>
  </world>
</sdf>
""")
        crawler = SDFCrawler(sdf)
        crawler.collect()
        assert "model://nonexistent_model" in crawler.unresolved


# --- rewrite_sdf -----------------------------------------------------------

class TestRewriteSDF:
    def test_rewrites_uris_in_output(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        output = rewrite_sdf(sdf, crawler.rewrites)
        # every rewritten URI should appear in the output
        for new_uri in crawler.rewrites.values():
            assert new_uri in output
        # original relative texture paths should be replaced
        assert "../materials/textures/brick_albedo.png" not in output

    def test_rewrites_relative_mesh_path(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        output = rewrite_sdf(sdf, crawler.rewrites)
        assert "floor.obj" in output  # filename still present
        # but original relative texture paths should be rewritten
        assert "../materials/textures/brick_albedo.png" not in output

    def test_preserves_non_uri_content(self, crawl_project):
        root, sdf = crawl_project
        crawler = SDFCrawler(sdf)
        crawler.collect()
        output = rewrite_sdf(sdf, crawler.rewrites)
        assert '<world name="test">' in output
        assert '<plugin filename=' in output


# --- Asset name collision --------------------------------------------------

class TestAssetNameCollision:
    def test_two_meshes_same_name_get_distinct_keys(self, tmp_path, clean_env):
        """Two models each with mesh.dae should both appear in assets."""
        (tmp_path / ".git").mkdir()

        for model_name in ["alpha", "beta"]:
            model_dir = tmp_path / "models" / model_name
            meshes = model_dir / "meshes"
            meshes.mkdir(parents=True)
            (model_dir / "model.sdf").write_text("<sdf/>")
            (meshes / "mesh.dae").write_text(f"{model_name} mesh data")

        sdf = tmp_path / "w.sdf"
        sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="w">
    <include><uri>model://alpha</uri></include>
    <include><uri>model://beta</uri></include>
  </world>
</sdf>
""")
        crawler = SDFCrawler(sdf)
        crawler.collect()

        # Both mesh.dae files must be present (under distinct keys)
        mesh_keys = [k for k in crawler.assets if "mesh.dae" in k]
        assert len(mesh_keys) == 2
        # They must map to different source files
        sources = [crawler.assets[k] for k in mesh_keys]
        assert sources[0] != sources[1]


# --- Nested include --------------------------------------------------------

class TestNestedInclude:
    @pytest.mark.xfail(reason="crawler does not yet recurse into model.sdf inside directory includes")
    def test_recursive_include_collects_inner_model(self, tmp_path, clean_env):
        """Model A's model.sdf includes model B — crawler should collect both."""
        (tmp_path / ".git").mkdir()

        # Model B — a simple model with a mesh
        model_b = tmp_path / "models" / "wheel"
        (model_b / "meshes").mkdir(parents=True)
        (model_b / "model.sdf").write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <model name="wheel">
    <link name="link">
      <visual name="v"><geometry>
        <mesh><uri>meshes/rim.dae</uri></mesh>
      </geometry></visual>
    </link>
  </model>
</sdf>
""")
        (model_b / "meshes" / "rim.dae").write_text("")

        # Model A — includes model B
        model_a = tmp_path / "models" / "car"
        (model_a / "meshes").mkdir(parents=True)
        (model_a / "model.sdf").write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <model name="car">
    <include><uri>model://wheel</uri></include>
    <link name="body">
      <visual name="v"><geometry>
        <mesh><uri>meshes/chassis.dae</uri></mesh>
      </geometry></visual>
    </link>
  </model>
</sdf>
""")
        (model_a / "meshes" / "chassis.dae").write_text("")

        # World includes model A
        sdf = tmp_path / "world.sdf"
        sdf.write_text("""\
<?xml version="1.0"?>
<sdf version="1.9">
  <world name="w">
    <include><uri>model://car</uri></include>
  </world>
</sdf>
""")

        crawler = SDFCrawler(sdf)
        crawler.collect()

        # Both models should be collected
        keys = list(crawler.assets.keys())
        assert any("car" in k and "chassis.dae" in k for k in keys)
        assert any("wheel" in k for k in keys)
