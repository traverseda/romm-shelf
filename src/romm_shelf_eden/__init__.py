"""Eden, a Nintendo Switch emulator (a Yuzu fork), as an external emulator.

ES-DE already has an EDEN find rule and a commented-out Eden command for the
switch system, so only the location and the command are ours. The folder links
follow RetroDECK's old Ponzu setup for Yuzu, which Eden shares a layout with:
keys and firmware under bios/switch, NAND and SD card (the saves) under
saves/switch/eden.
"""

from romm_shelf.external import External

EDEN = External(
    name="eden",
    platform="switch",
    pattern="Eden-Linux-*.appimage",
    es_emulator="EDEN",
    es_label="Eden (Standalone)",
    es_args="-f -g %ROM%",
    links=(
        ("eden/keys", "$bios_path/switch/keys"),
        ("eden/nand", "$saves_path/switch/eden/nand"),
        ("eden/sdmc", "$saves_path/switch/eden/sdmc"),
        # RetroDECK keeps firmware as Ryujinx installs it: one folder per .nca.
        ("eden/nand/system/Contents/registered", "$bios_path/switch/firmware/registered"),
    ),
    # RomM is where updates come from; the AppImage's own updater stays off.
    env={"DISABLE_AUTO_UPDATES": "1"},
)
