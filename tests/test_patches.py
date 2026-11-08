"""Patches are Ansible playbooks (design.md § Patches): where they come from, plan = check mode, apply.

These run the real ansible-core against files under tmp_path; nothing outside it is touched.
"""
import pytest

from romm_shelf import patches
from romm_shelf.config import Settings

LINE_PATCH = """# Sets greeting in the test file.
- name: Greeting
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.lineinfile:
        path: "{{ config_home }}/hello.ini"
        regexp: '^greeting='
        line: greeting=hello
        create: true
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "config/romm-shelf"


def user_patch(home, name: str, text: str):
    (home / "patches").mkdir(parents=True, exist_ok=True)
    (home / "patches" / f"{name}.yml").write_text(text)


def test_builtins_and_the_users_own(home):
    user_patch(home, "hello", LINE_PATCH)
    found = patches.sources()
    assert {"background-sync", "retroarch", "settings", "hello"} <= found.keys()
    assert found["hello"].parent == home / "patches"
    assert found["retroarch"].parent == patches.BUILTIN
    assert found["settings"].parent == patches.BUILTIN


def test_romm_shelfs_own_settings_reach_every_patch(home, tmp_path):
    """The central `settings` patch: every other playbook gets its values as extra vars."""
    user_patch(home, "uses-central", """- name: Uses a central setting
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Write the emulator choice
      ansible.builtin.copy:
        dest: "{{ config_home }}/emulator.txt"
        content: "{{ retroarch }}\\n"
""")
    patches.save_settings(patches.SETTINGS_PATCH, {"retroarch": "external"})
    patches.apply(Settings(root=tmp_path / "root"), "uses-central")
    assert (home / "emulator.txt").read_text() == "external\n"


def test_a_users_patch_replaces_a_builtin_of_the_same_name(home):
    user_patch(home, "retroarch", LINE_PATCH)
    assert patches.sources()["retroarch"] == home / "patches/retroarch.yml"


def test_plan_is_check_mode_and_changes_nothing(home, tmp_path):
    user_patch(home, "hello", LINE_PATCH)
    p = patches.check(Settings(root=tmp_path / "root"), "hello", patches.find("hello"))
    assert (p.state, p.title, p.builtin) == ("pending", "Greeting", False)
    assert p.description == "Sets greeting in the test file."
    assert p.plan and "Say hello" in p.plan[0]
    assert not (home / "hello.ini").exists()


def test_apply_converges(home, tmp_path):
    settings = Settings(root=tmp_path / "root")
    user_patch(home, "hello", LINE_PATCH)
    results = patches.apply(settings, "hello")
    assert [r.changed for r in results] == [True]
    assert (home / "hello.ini").read_text() == "greeting=hello\n"
    assert patches.check(settings, "hello", patches.find("hello")).state == "applied"
    assert [r.changed for r in patches.apply(settings, "hello")] == [False]


def test_a_failing_task_fails_the_patch(home, tmp_path):
    user_patch(home, "broken", LINE_PATCH + """    - name: Refuse
      ansible.builtin.fail:
        msg: not on this machine
""")
    p = patches.check(Settings(root=tmp_path / "root"), "broken", patches.find("broken"))
    assert p.state == "failed" and "not on this machine" in p.error


def test_bad_yaml_is_an_error_not_a_crash(home, tmp_path):
    user_patch(home, "bad", "- name: [unclosed\n")
    p = patches.check(Settings(root=tmp_path / "root"), "bad", patches.find("bad"))
    assert p.state == "failed" and p.error


FAILING_PATCH = """- name: Fails
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Refuse
      ansible.builtin.fail:
        msg: not on this machine
"""


def _only_these(home, monkeypatch, *names):
    """Only these user patches, in this order, as the Patches tab's system patches."""
    monkeypatch.setattr(patches, "_system_sources",
                        lambda: [(n, home / "patches" / f"{n}.yml", None) for n in names])


def test_the_system_patches_are_checked_in_one_ansible_playbook(home, tmp_path, monkeypatch):
    for name in ("one", "two", "three"):
        user_patch(home, name, LINE_PATCH.replace("hello.ini", f"{name}.ini"))
    _only_these(home, monkeypatch, "one", "two", "three")
    runs = []
    real = patches.run_playbook
    monkeypatch.setattr(patches, "run_playbook", lambda *a, **k: runs.append(a[0]) or real(*a, **k))
    found = patches.status(Settings(root=tmp_path / "root"))
    assert [(p.name, p.state) for p in found] == [("one", "pending"), ("two", "pending"), ("three", "pending")]
    assert all("Say hello" in p.plan[0] for p in found)
    assert len(runs) == 1


def test_a_failing_patch_doesnt_stop_the_ones_after_it(home, tmp_path, monkeypatch):
    """Ansible abandons a run once a play's every host has failed: the rest go again."""
    user_patch(home, "fails", FAILING_PATCH)
    user_patch(home, "hello", LINE_PATCH)
    _only_these(home, monkeypatch, "fails", "hello")
    found = {p.name: p for p in patches.status(Settings(root=tmp_path / "root"))}
    assert found["fails"].state == "failed" and "not on this machine" in found["fails"].error
    assert found["hello"].state == "pending" and "Say hello" in found["hello"].plan[0]


def test_a_patch_that_cant_load_is_the_only_one_failed(home, tmp_path, monkeypatch):
    user_patch(home, "unknown-module", LINE_PATCH.replace("ansible.builtin.lineinfile", "no.such.module"))
    user_patch(home, "hello", LINE_PATCH)
    _only_these(home, monkeypatch, "unknown-module", "hello")
    found = {p.name: p for p in patches.status(Settings(root=tmp_path / "root"))}
    assert found["unknown-module"].state == "failed" and "no.such.module" in found["unknown-module"].error
    assert found["hello"].state == "pending"


def test_a_setting_cant_share_a_name_with_a_play_var(home):
    user_patch(home, "clash", """- name: Clash
  hosts: localhost
  gather_facts: false
  vars:
    shade: dark
    x_settings:
      shade: {default: light}
  tasks: []
""")
    with pytest.raises(patches.PatchError, match="also one of the play's vars"):
        patches.declared(patches.find("clash"))


def test_unknown_patch():
    with pytest.raises(KeyError, match="unknown patch"):
        patches.find("no-such-patch")


def test_background_sync_plans_the_unit(home, tmp_path):
    p = patches.check(Settings(root=tmp_path / "root"), "background-sync", patches.find("background-sync"))
    assert p.state == "pending" and any("romm-shelf user unit" in line for line in p.plan)
    assert not (tmp_path / "config/systemd").exists()


# Per emulator and per game (design.md § Patches, Per game).

GAME_PATCH = """# Writes one line per game.
- name: Game greeting
  hosts: all
  gather_facts: false
  vars:
    x_scope: game
    x_emulators: [mesen, genesis-plus-gx]
    x_systems: [nes]
    x_settings:
      shader:
        title: Shader
        choices:
          plain: Plain
          fancy: Fancy
        default: plain
  tasks:
    - name: Write this game
      ansible.builtin.copy:
        dest: "{{ config_home }}/{{ game.system }}-{{ game.name }}.txt"
        content: "{{ shader }} here={{ x_here.get('shader', false) }}\\n"
"""


def a_game(tmp_path, system: str = "nes", name: str = "Game.nes"):
    path = tmp_path / "root/roms" / system / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dummy")
    return tmp_path


def test_a_game_patch_layers_and_writes_only_what_is_set_here(home, tmp_path):
    a_game(tmp_path)
    user_patch(home, "game", GAME_PATCH)
    settings = Settings(root=tmp_path / "root")
    target = patches.Target("mesen", "nes/Game.nes")
    assert {"game", "crt-shader-game"} <= {p.name for p in patches.for_target(settings, target) if p.scope == "game"}
    assert patches.settings("game", target=target)[0].here is False  # default: inherited

    patches.save_settings("game", {"shader": "fancy"}, target)
    p = patches.check(settings, "game", patches.find("game"), target)
    assert (p.scope, p.target.game, p.settings[0].value, p.settings[0].here) == ("game", "nes/Game.nes", "fancy", True)
    patches.apply(settings, "game", target=target)
    assert (home / "nes-Game.txt").read_text() == "fancy here=True\n"

    patches.save_settings("game", {"shader": None}, target)  # back to inherited
    p = patches.check(settings, "game", patches.find("game"), target)
    assert (p.settings[0].value, p.settings[0].here) == ("plain", False)
    patches.apply(settings, "game", target=target)
    assert (home / "nes-Game.txt").read_text() == "plain here=False\n"


def test_a_game_patch_is_set_per_game_only(home, tmp_path):
    a_game(tmp_path)
    user_patch(home, "game", GAME_PATCH)
    # Everywhere would look like a default, but a game-scoped patch only writes at game level.
    with pytest.raises(ValueError, match="per game"):
        patches.save_settings("game", {"shader": "fancy"})
    with pytest.raises(ValueError, match="per game"):
        patches.save_settings("game", {"shader": "fancy"}, patches.Target("mesen"))


def test_game_scoped_patches_stay_out_of_the_patches_tab_and_apply_all(home, tmp_path):
    a_game(tmp_path)
    user_patch(home, "game", GAME_PATCH)
    settings = Settings(root=tmp_path / "root")
    assert "game" not in [p.name for p in patches.status(settings)]
    assert "game" not in [p.name for p in patches.listing(settings)]
    with pytest.raises(patches.PatchError, match="per game"):
        patches.apply(settings, "game")


def test_listing_draws_patches_without_running_them(home, tmp_path):
    """The UI draws every patch before checking it: describing one is YAML only."""
    user_patch(home, "hello", LINE_PATCH)
    user_patch(home, "broken", LINE_PATCH + """    - name: Refuse
      ansible.builtin.fail:
        msg: not on this machine
""")
    listed = {p.name: p for p in patches.listing(Settings(root=tmp_path / "root"))}
    assert (listed["hello"].state, listed["hello"].title) == ("checking", "Greeting")
    assert listed["hello"].plan == []
    assert listed["broken"].state == "checking"  # checking it would fail; drawing it does not run it
    assert not (home / "hello.ini").exists()


def test_checks_run_in_parallel_without_sharing_vars(home, tmp_path, monkeypatch):
    """status() checks several patches at once; each run's extra vars must be its own."""
    monkeypatch.setattr(patches, "BUILTIN", tmp_path / "no-builtins")  # only these two
    for word in ("one", "two"):
        user_patch(home, word, f"""- name: {word}
  hosts: localhost
  gather_facts: false
  vars:
    x_settings:
      word:
        default: {word}
  tasks:
    - name: {word} got its own value
      ansible.builtin.assert:
        that: word == "{word}"
""")
    found = {p.name: p for p in patches.status(Settings(root=tmp_path / "root"))}
    assert found["one"].state == found["two"].state == "applied"


def test_x_systems_limits_a_game_patch(home, tmp_path):
    a_game(tmp_path, "gamegear", "Game.gg")
    user_patch(home, "game", GAME_PATCH)
    target = patches.Target("genesis-plus-gx", "gamegear/Game.gg")
    assert not patches.applies(patches.find("game"), target)
    assert "game" not in [p.name for p in patches.for_target(Settings(root=tmp_path / "root"), target)]


EMULATOR_PATCH = GAME_PATCH.replace("x_scope: game", "x_scope: emulator").replace(
    'dest: "{{ config_home }}/{{ game.system }}-{{ game.name }}.txt"',
    'dest: "{{ config_home }}/{{ emulator.name }}.txt"')


def test_a_games_settings_are_only_its_own(home, tmp_path):
    """Owner, 2026-11-07: per-emulator patches are set on the Patches tab, not from a game. Applied
    for a game, one still runs for the game's emulator."""
    a_game(tmp_path)
    user_patch(home, "emu", EMULATOR_PATCH)
    settings = Settings(root=tmp_path / "root")
    game = patches.Target("mesen", "nes/Game.nes")
    found = {p.name: p for p in patches.for_target(settings, game)}
    assert "emu" not in found and found["crt-shader-game"].target == game
    assert "emu" not in [p.name for p in patches.listing_for_target(settings, game)]

    target = patches.target_of(patches.find("emu"), game)
    patches.save_settings("emu", {"shader": "fancy"}, target)
    patches.apply(settings, "emu", target=game)
    assert (home / "mesen.txt").read_text() == "fancy here=True\n"
    assert patches.settings("emu", target=patches.Target("genesis-plus-gx"))[0].value == "plain"


def test_the_patches_tab_has_a_per_emulator_patch_once_per_emulator(home, tmp_path):
    """Set at each emulator's level there; one for several emulators also comes without a target,
    for what they all inherit. The game-scoped ones are left out."""
    user_patch(home, "emu", EMULATOR_PATCH)
    settings = Settings(root=tmp_path / "root")
    entries = [(p.name, p.target) for p in patches.listing(settings)]
    assert ("dolphin", patches.Target("dolphin")) in entries and ("dolphin", None) not in entries
    emu = [t for n, t in entries if n == "emu"]
    assert emu[0] is None and patches.Target("mesen") in emu and len(emu) > 2
    assert not [n for n, _ in entries if n in ("crt-shader-game", "dolphin-game")]


def test_a_game_patch_is_not_for_an_emulator_alone(home, tmp_path):
    a_game(tmp_path)
    user_patch(home, "game", GAME_PATCH)
    settings = Settings(root=tmp_path / "root")
    assert "game" not in [p.name for p in patches.listing_for_target(settings, patches.Target("mesen"))]
    with pytest.raises(patches.PatchError, match="per game"):
        patches.apply(settings, "game", target=patches.Target("mesen"))


def test_a_setting_can_say_what_inheriting_means(home):
    s = {x.key: x for x in patches.settings("crt-shader-game", target=patches.Target("mesen", "nes/Game.nes"))}
    assert s["crt_shader"].inherit == "the console's shader" and not s["crt_shader"].here


def test_cli_sets_a_per_emulator_value(home, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from romm_shelf.cli import app
    user_patch(home, "emu", EMULATOR_PATCH)
    monkeypatch.setenv("ROMM_SHELF_ROOT", str(tmp_path / "root"))
    run = CliRunner().invoke(app, ["patches", "set", "emu", "shader=fancy", "--emulator", "mesen"])
    assert run.exit_code == 0, run.output
    assert "--emulator mesen" in run.output
    assert patches.settings("emu", target=patches.Target("mesen"))[0].here
    bad = CliRunner().invoke(app, ["patches", "set", "emu", "shader=fancy", "--emulator", "nope"])
    assert bad.exit_code != 0 and "unknown emulator" in bad.output


# From RomM (design.md § Patches, From RomM).

class FakeRomm:
    """Firmware and ROM files as RomM lists them; counts downloads."""

    def __init__(self, firmware=(), files=None):
        self.fw = list(firmware)
        self.files = files or {}
        self.downloads = 0

    def firmware(self):
        return [{"id": i, "file_name": n, "md5_hash": None, "updated_at": f"2026-11-0{i}"}
                for i, (n, _) in enumerate(self.fw, 1)]

    def download_firmware(self, firmware_id, file_name):
        self.downloads += 1
        return self.fw[firmware_id - 1][1].encode()

    def rom_file_bytes(self, rom_id, file_id, file_name):
        self.downloads += 1
        return self.files[file_id].encode()


def a_rom(*files):
    from romm_shelf.romm import Rom
    return Rom.model_validate({
        "id": 7, "platform_id": 1, "platform_fs_slug": "nes", "fs_name": "Game", "fs_name_no_ext": "Game",
        "fs_size_bytes": 1, "has_nested_single_file": True,
        "files": [{"file_name": "Game.nes", "file_path": "nes/Game", "is_top_level": True, "id": 1},
                  *[{"file_name": n, "file_path": "nes/Game/patch", "category": "patch", "id": i, "md5_hash": None}
                    for i, n in files]]})


def test_romm_firmware_playbooks_are_system_patches(home, tmp_path):
    client = FakeRomm([("hello-romm.yml", LINE_PATCH), ("scph1001.bin", "not a playbook")])
    patches.refresh_firmware(client)
    found = {p.name: p for p in patches.listing(Settings(root=tmp_path / "root"))}
    assert found["hello-romm"].origin == "romm" and found["hello-romm"].state == "checking"
    assert "scph1001" not in found
    # RomM no longer has it: the copy goes too.
    client.fw = []
    patches.refresh_firmware(client)
    assert "hello-romm" not in patches.sources()


def test_layers_builtin_then_romm_then_the_users(home):
    patches.refresh_firmware(FakeRomm([("retroarch.yml", LINE_PATCH)]))
    assert patches.origin(patches.find("retroarch")) == "romm"
    user_patch(home, "retroarch", LINE_PATCH)
    assert patches.origin(patches.find("retroarch")) == "user"


def test_a_games_own_playbook_shows_only_in_that_games_settings(home, tmp_path):
    a_game(tmp_path)
    settings = Settings(root=tmp_path / "root")
    client = FakeRomm(files={2: GAME_PATCH.replace("Game greeting", "Its own"), 3: "not yaml"})
    target = patches.Target("mesen", "nes/Game.nes")
    patches.refresh_game(client, a_rom((2, "own.yml"), (3, "readme.txt")), target.game)
    mine = {p.name: p for p in patches.listing_for_target(settings, target)}
    assert mine["own"].origin == "game" and mine["own"].title == "Its own"
    assert "own" not in {p.name for p in patches.listing_for_target(settings, patches.Target("mesen", "nes/Other.nes"))}
    assert "own" not in {p.name for p in patches.listing(settings)}
    patches.save_settings("own", {"shader": "fancy"}, target)
    patches.apply(settings, "own", target=target)
    assert (home / "nes-Game.txt").read_text() == "fancy here=True\n"


def test_a_games_own_playbook_must_be_per_game(home, tmp_path):
    a_game(tmp_path)
    target = patches.Target("mesen", "nes/Game.nes")
    patches.refresh_game(FakeRomm(files={2: LINE_PATCH}), a_rom((2, "sys.yml")), target.game)
    [p] = [p for p in patches.listing_for_target(Settings(root=tmp_path / "root"), target) if p.name == "sys"]
    assert p.state == "failed" and "x_scope: game" in p.error


def test_unchanged_copies_are_not_downloaded_again(home):
    import hashlib
    client = FakeRomm([("hello-romm.yml", LINE_PATCH)])
    md5 = hashlib.md5(LINE_PATCH.encode()).hexdigest()
    client.firmware = lambda: [{"id": 1, "file_name": "hello-romm.yml", "md5_hash": md5}]
    patches.refresh_firmware(client)
    patches.refresh_firmware(client)
    assert client.downloads == 1


def test_names_from_romm_cannot_escape_the_folder(home):
    patches.refresh_firmware(FakeRomm([(".hidden.yml", LINE_PATCH)]))
    assert list(patches.firmware_dir().iterdir()) == []


def test_retroarch_saves_state_on_exit_by_default(home, tmp_path):
    """design.md § Bundled emulators: a setting like the others, on by default."""
    cfg = home / "emulators/retroarch/retroarch.cfg"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('savestate_auto_save = "false"\nsavestate_auto_load = "false"\n')
    settings = Settings(root=tmp_path / "root")
    declared = {s.key: s for s in patches.declared(patches.find("retroarch"))}
    assert (declared["save_state_on_exit"].type, declared["save_state_on_exit"].default) == ("bool", True)

    patches.apply(settings, "retroarch")
    assert 'savestate_auto_save = "true"' in cfg.read_text()

    patches.save_settings("retroarch", {"save_state_on_exit": False})
    patches.apply(settings, "retroarch")
    assert 'savestate_auto_save = "false"' in cfg.read_text()
    assert patches.check(settings, "retroarch", patches.find("retroarch")).state == "applied"
