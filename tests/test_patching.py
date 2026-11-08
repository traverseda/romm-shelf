import pytest

from romm_shelf import patching
from romm_shelf.patching import AltEmulator, Block, Blocked, File, Link, Setting, plan
from tests.conftest import touch


@pytest.fixture(autouse=True)
def nothing_running(monkeypatch):
    monkeypatch.setattr(patching, "running", lambda process: False)


def roundtrip(tasks):
    """Install, check it's idempotent, remove, check nothing of ours is left."""
    patching.apply(plan(tasks, "present"))
    assert plan(tasks, "present") == []
    patching.apply(plan(tasks, "absent"))
    assert plan(tasks, "absent") == []


def test_block_goes_in_before_the_closing_tag_and_comes_out_exactly(tmp_path):
    f = tmp_path / "rules.xml"
    original = '<?xml version="1.0"?>\n<ruleList>\n  <!-- theirs -->\n</ruleList>\n'
    f.write_text(original)
    block = Block(f, "eden", "  <emulator/>", "rule", comment=("<!--", "-->"), before="</ruleList>")
    patching.apply(plan([block], "present"))
    assert f.read_text() == ('<?xml version="1.0"?>\n<ruleList>\n  <!-- theirs -->\n'
                             "<!-- BEGIN ROMM-SHELF MANAGED BLOCK eden -->\n  <emulator/>\n"
                             "<!-- END ROMM-SHELF MANAGED BLOCK eden -->\n</ruleList>\n")
    roundtrip([block])
    assert f.read_text() == original


def test_block_is_replaced_not_duplicated_when_it_changes(tmp_path):
    f = tmp_path / "x.conf"
    f.write_text("a=1\n")
    patching.apply(plan([Block(f, "p", "b=2", "x")], "present"))
    patching.apply(plan([Block(f, "p", "b=3", "x")], "present"))
    assert f.read_text().count("BEGIN ROMM-SHELF MANAGED BLOCK p") == 1 and "b=3" in f.read_text()


def test_file_is_never_overwritten_or_removed_unless_marked(tmp_path):
    f = tmp_path / "hook.sh"
    f.write_text("#!/bin/sh\necho theirs\n")
    ours = File(f, f"#!/bin/sh\n{patching.marker_line('p')}\n", "hook", mode=0o755)
    with pytest.raises(Blocked, match="isn't romm-shelf's"):
        plan([ours], "present")
    assert plan([ours], "absent") == []
    f.unlink()
    roundtrip([ours])
    assert not f.exists()


def test_setting_declares_its_off_value(tmp_path):
    f = tmp_path / "es_settings.xml"
    f.write_text('<bool name="X" value="false" />\n<bool name="Y" value="false" />\n')
    s = Setting(f, r'^<bool name="X" value="(?P<value>\w+)" />$', "true", "false", "X")
    patching.apply(plan([s], "present"))
    assert f.read_text() == '<bool name="X" value="true" />\n<bool name="Y" value="false" />\n'
    roundtrip([s])
    assert 'name="X" value="false"' in f.read_text()
    kept = Setting(f, r"^<bool name=\"Y\" value=\"(?P<value>\w+)\" />$", "true", None, "Y")
    patching.apply(plan([kept], "present"))
    assert plan([kept], "absent") == []  # None: removal leaves it


def test_link_never_shadows_data(tmp_path):
    target = tmp_path / "saves"
    link = Link(tmp_path / "nand", target, "saves")
    touch(tmp_path / "nand/save.dat")
    with pytest.raises(Blocked, match="already has data"):
        plan([link], "present")
    (tmp_path / "nand/save.dat").unlink()
    roundtrip([link])
    assert target.is_dir() and not (tmp_path / "nand").exists()


def test_alt_emulator_is_reset_only_while_it_is_ours(tmp_path):
    g = tmp_path / "gamelist.xml"
    g.write_text('<?xml version="1.0"?>\n<gameList>\n</gameList>\n')
    alt = AltEmulator(g, "Eden (Standalone)", "switch")
    patching.apply(plan([alt], "present"))
    assert patching.chosen_emulator(g) == "Eden (Standalone)"
    patching.apply(plan([AltEmulator(g, "Ryubing (Standalone)", "switch")], "present"))  # the user picked another
    assert plan([alt], "absent") == []


def test_steps_wait_for_the_program_that_owns_the_file(tmp_path, monkeypatch):
    f = tmp_path / "s.xml"
    f.write_text('<bool name="X" value="false" />\n')
    monkeypatch.setattr(patching, "running", lambda process: process == "es-de")
    with pytest.raises(SystemExit, match="es-de is running"):
        patching.apply(plan([Setting(f, r'value="(?P<value>\w+)"', "true", "false", "X", closed="es-de")], "present"))
    assert "false" in f.read_text()


def test_apply_reports_each_step_and_stops_at_the_first_failure(tmp_path):
    def boom():
        raise OSError("disk full")
    steps = [patching.Step("a", "x", "", lambda: None), patching.Step("b", "y", "", boom),
             patching.Step("c", "z", "", lambda: None)]
    seen = []
    with pytest.raises(OSError):
        patching.apply(steps, lambda s, e: seen.append((s.action, e)))
    assert seen == [("a", None), ("b", "disk full")]
