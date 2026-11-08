# The emulators romm-shelf bundles (design.md § Bundled emulators): $out/retroarch/, $out/dolphin/,
# $out/pcsx2/, $out/ppsspp/, $out/rpcs3/, $out/azahar/, $out/cemu/ and $out/eden/.
#
# RetroArch and its cores, Dolphin, PCSX2, PPSSPP, RPCS3, Azahar, Cemu and Eden from nixpkgs, for every
# target: a NixOS host runs them from the store, and the AppImage (nix-appimage) carries the same store
# paths. The layout is what emulators.py, romm_shelf_dolphin, romm_shelf_pcsx2 and romm_shelf_ppsspp
# expect: retroarch/{bin/retroarch, cores/,
# info/, assets/, autoconfig/, default.cfg}, dolphin/{bin/dolphin-emu, lib, default.ini},
# pcsx2/{bin/pcsx2-qt, lib, default.ini}, ppsspp/{bin/ppsspp, lib, default.ini},
# rpcs3/{bin/rpcs3, lib, default.yml, input/}, azahar/{bin/azahar, lib, default/qt-config.ini},
# cemu/{bin/cemu, lib, default/settings.xml, input/controllerProfiles/} and
# eden/{bin/eden, lib, default/qt-config.ini}.
{ lib, runCommand, writeShellScript, writeText, nixgl, libglvnd, retroarch-bare, libretro, libretro-core-info, retroarch-assets,
  retroarch-joypad-autoconfig, libretro-shaders-slang, dolphin-emu, pcsx2, ppsspp, rpcs3, azahar, cemu,
  eden, bubblewrap, squashfuse, squashfsTools, fuse3 }:

let
  # Exactly the cores romm_shelf_retroarch declares: file stem -> nixpkgs package. The systems
  # RetroDECK launches with a standalone emulator (gc, wii, ps2, psp, switch, n3ds, wiiu, ps3,
  # psvita, xbox) are not here; their standalone plugins own them.
  cores = {
    mesen_libretro = libretro.mesen;
    snes9x_libretro = libretro.snes9x;
    mesen-s_libretro = libretro.mesen-s;
    gambatte_libretro = libretro.gambatte;
    mgba_libretro = libretro.mgba;
    genesis_plus_gx_libretro = libretro.genesis-plus-gx;
    picodrive_libretro = libretro.picodrive;
    mupen64plus_next_libretro = libretro.mupen64plus;
    parallel_n64_libretro = libretro.parallel-n64;
    melonds_libretro = libretro.melonds;
    swanstation_libretro = libretro.swanstation;
    mednafen_saturn_libretro = libretro.beetle-saturn;
    mednafen_pce_libretro = libretro.beetle-pce;
    mednafen_pcfx_libretro = libretro.beetle-pcfx;
    mednafen_supergrafx_libretro = libretro.beetle-supergrafx;
    mednafen_vb_libretro = libretro.beetle-vb;
    mednafen_wswan_libretro = libretro.beetle-wswan;
    mednafen_ngp_libretro = libretro.beetle-ngp;
    flycast_libretro = libretro.flycast;
    fbneo_libretro = libretro.fbneo;
    fbalpha2012_libretro = libretro.fbalpha2012;
    neocd_libretro = libretro.neocd;
    mame_libretro = libretro.mame;
    opera_libretro = libretro.opera;
    puae_libretro = libretro.puae;
    stella_libretro = libretro.stella;
    prosystem_libretro = libretro.prosystem;
    atari800_libretro = libretro.atari800;
    virtualjaguar_libretro = libretro.virtualjaguar;
    handy_libretro = libretro.handy;
    hatari_libretro = libretro.hatari;
    o2em_libretro = libretro.o2em;
    freeintv_libretro = libretro.freeintv;
    bluemsx_libretro = libretro.bluemsx;
    vice_x64sc_libretro = libretro.vice-x64sc;
    vice_xplus4_libretro = libretro.vice-xplus4;
    vice_xvic_libretro = libretro.vice-xvic;
    dosbox_pure_libretro = libretro.dosbox-pure;
    easyrpg_libretro = libretro.easyrpg;
    np2kai_libretro = libretro.np2kai;
    same_cdi_libretro = libretro.same_cdi;
    scummvm_libretro = libretro.scummvm;
    tic80_libretro = libretro.tic80;
    vecx_libretro = libretro.vecx;
    fuse_libretro = libretro.fuse;
    "81_libretro" = libretro.eightyone;
  };
  # RetroArch's starting config, copied read-write on its first run; the `retroarch` patch goes on
  # top (design.md § Patches). Only what differs from RetroArch's built-in defaults.
  defaults = {
    menu_driver = "ozone";
    video_fullscreen = "true";
    pause_nonactive = "false";
    menu_show_online_updater = "false";
    menu_show_core_updater = "false";
    history_list_enable = "false";
    content_runtime_log = "false";
    # Straight into the game: no launch animation, no boot-time on-screen messages.
    menu_show_load_content_animation = "false";
    rgui_show_start_screen = "false";
    notification_show_autoconfig = "false";
    notification_show_autoconfig_fails = "false";
    notification_show_config_override_load = "false";
    notification_show_remap_load = "false";
    notification_show_refresh_rate = "false";
    notification_show_set_initial_disk = "false";
  };
  defaultCfg = builtins.toFile "retroarch-default.cfg"
    (lib.concatStrings (lib.mapAttrsToList (k: v: "${k} = \"${v}\"\n") defaults));
  # RetroArch runs under nixGL's Mesa GL and Vulkan wrappers. Nix's loader never looks in the
  # host's /usr/lib or /run/opengl-driver, so without them there's no libGL.so.1: RetroArch has no
  # GL, and Steam's overlay (LD_PRELOAD gameoverlayrenderer.so, which needs libGL.so.1) stops it
  # from starting at all when Steam launches it. Mesa covers AMD (the Deck), Intel and nouveau.
  retroarch = writeShellScript "retroarch" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe retroarch-bare} "$@"
  '';
  # Dolphin's starting Dolphin.ini, copied read-write on its first run; the `dolphin` patch goes on
  # top (saves, controllers). Only what differs from Dolphin's defaults, and nothing that asks a
  # question before the game starts.
  dolphinDefaults = {
    Analytics = { Enabled = "False"; PermissionAsked = "True"; };
    Core = { GFXBackend = "Vulkan"; };
    Display = { Fullscreen = "True"; };
    Interface = { ConfirmStop = "False"; PauseOnFocusLost = "False"; };
    General = { };
  };
  dolphinIni = builtins.toFile "dolphin-default.ini" (lib.generators.toINI { } dolphinDefaults);
  dolphin = writeShellScript "dolphin-emu" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe' dolphin-emu "dolphin-emu"} "$@"
  '';
  # PCSX2's starting PCSX2.ini, copied read-write on its first run; the `pcsx2` patch goes on top
  # (controllers, save folders). SettingsVersion is PCSX2's own; no setup wizard and no questions
  # before the game starts.
  pcsx2Ini = writeText "pcsx2-default.ini" ''
    [UI]
    SettingsVersion = 1
    SetupWizardIncomplete = false
    StartFullscreen = true
    ConfirmShutdown = false
    PauseOnFocusLoss = false
  '';
  pcsx2Wrapper = writeShellScript "pcsx2-qt" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe' pcsx2 "pcsx2-qt"} "$@"
  '';
  # PPSSPP's starting ppsspp.ini, copied read-write on its first run; the `ppsspp` patch goes on
  # top (saves, controllers). Vulkan and 3x PSP (RetroDECK's Deck choice); no first-run wizard and
  # no exit confirmation.
  ppssppIni = writeText "ppsspp-default.ini" ''
    [General]
    FirstRun = False
    AskForExitConfirmationAfterSeconds = 0
    PauseWhenMinimized = False
    AutoLoadSaveState = 0

    [Graphics]
    GraphicsBackend = VULKAN
    FullScreen = True
    InternalResolution = 3
    DisplayCropTo16x9 = True
    VerticalSync = True
  '';
  ppssppWrapper = writeShellScript "ppsspp" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe ppsspp} "$@"
  '';
  # RPCS3's starting config.yml, copied read-write on its first run; the `rpcs3` patch goes on top.
  # Vulkan, fullscreen, no pause on focus loss, and exit when the game does (no-gui mode).
  rpcs3Defaults = writeText "rpcs3-default.yml" ''
    Audio:
      Renderer: Cubeb
    Miscellaneous:
      Automatically start games after boot: true
      Exit RPCS3 when process finishes: true
      Pause emulation on RPCS3 focus loss: false
      Start games in fullscreen mode: true
    Video:
      Renderer: Vulkan
      Resolution Scale: 100
  '';
  # Player 1 is Steam's first virtual pad. Empty SteamVirtualGamepadInfo makes every virtual pad
  # "Steam Virtual Gamepad", numbered in SDL's order, so player 1 is "... 1" (design.md §
  # Controller rule). The names are RPCS3's own (Input/sdl_pad_handler.cpp).
  rpcs3Input = writeText "rpcs3-Default.yml" ''
    Player 1 Input:
      Handler: SDL
      Device: Steam Virtual Gamepad 1
      Config:
        Left Stick Left: LS X-
        Left Stick Down: LS Y-
        Left Stick Right: LS X+
        Left Stick Up: LS Y+
        Right Stick Left: RS X-
        Right Stick Down: RS Y-
        Right Stick Right: RS X+
        Right Stick Up: RS Y+
        Start: Start
        Select: Back
        PS Button: Guide
        Square: West
        Cross: South
        Circle: East
        Triangle: North
        Left: Left
        Down: Down
        Right: Right
        Up: Up
        R1: RB
        R2: RT
        R3: RS
        L1: LB
        L2: LT
        L3: LS
  '';
  rpcs3Profiles = writeText "rpcs3-active-profiles.yml" ''
    Active Profiles:
      global: Default
  '';
  rpcs3Configs = writeText "rpcs3-active-input-configurations.yml" ''
    Active Configurations:
      global: Default
  '';
  rpcs3Wrapper = writeShellScript "rpcs3" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe rpcs3} "$@"
  '';
  # Azahar's starting qt-config.ini, copied read-write on its first run; the `azahar` patch goes on
  # top (storage paths, renderer, resolution). The input profile is Steam's first virtual pad: the
  # SDL GUID is the Steam Virtual Gamepad's, and player 1 is port 0 (design.md § Controller rule).
  azaharIni = writeText "azahar-default.ini" ''
    [Data%20Storage]
    use_custom_storage=true
    use_virtual_sd=true
    [Renderer]
    graphics_api=2
    resolution_factor=2
    use_vsync=true
    [UI]
    firstStart=false
    fullscreen=true
    confirmClose=false
    [Controls]
    profile=0
    use_artic_base_controller=false
    profiles\size=1
    profiles\1\name=Default
    profiles\1\input_maptype=2
    profiles\1\button_a="button:1,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_b="button:0,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_x="button:3,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_y="button:2,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_up="direction:up,engine:sdl,guid:030079f6de280000ff11000001000000,hat:0,port:0"
    profiles\1\button_down="direction:down,engine:sdl,guid:030079f6de280000ff11000001000000,hat:0,port:0"
    profiles\1\button_left="direction:left,engine:sdl,guid:030079f6de280000ff11000001000000,hat:0,port:0"
    profiles\1\button_right="direction:right,engine:sdl,guid:030079f6de280000ff11000001000000,hat:0,port:0"
    profiles\1\button_l="axis:2,direction:+,engine:sdl,guid:030079f6de280000ff11000001000000,port:0,threshold:0.5"
    profiles\1\button_r="axis:5,direction:+,engine:sdl,guid:030079f6de280000ff11000001000000,port:0,threshold:0.5"
    profiles\1\button_zl="button:4,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_zr="button:5,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_start="button:7,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\button_select="button:6,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\circle_pad="axis_x:0,axis_y:1,deadzone:0.100000,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\c_stick="axis_x:3,axis_y:4,deadzone:0.100000,engine:sdl,guid:030079f6de280000ff11000001000000,port:0"
    profiles\1\touch_device="engine:emu_window"
    profiles\1\motion_device="engine:motion_emu,update_period:100,sensitivity:0.01,tilt_clamp:90.0"
  '';
  azaharWrapper = writeShellScript "azahar" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe azahar} "$@"
  '';
  # Cemu's starting settings.xml, copied read-write on its first run; the `cemu` patch goes on
  # top (MLC path, Vulkan, fullscreen, no update checks). Only what differs from Cemu's defaults.
  cemuSettings = writeText "cemu-default-settings.xml" ''
    <?xml version="1.0" encoding="UTF-8"?>
    <content>
        <mlc_path></mlc_path>
        <check_update>false</check_update>
        <use_discord_presence>false</use_discord_presence>
        <fullscreen>true</fullscreen>
        <disable_screensaver>true</disable_screensaver>
        <play_boot_sound>false</play_boot_sound>
        <Graphic>
            <api>1</api>
            <VSync>0</VSync>
            <GX2DrawdoneSync>true</GX2DrawdoneSync>
            <AsyncCompile>true</AsyncCompile>
            <vkAccurateBarriers>true</vkAccurateBarriers>
        </Graphic>
        <Account>
            <PersistentId>2147483649</PersistentId>
        </Account>
    </content>
  '';
  # Player N is Steam's virtual pad N (design.md § Controller rule). Its SDL GUID is the Steam
  # Virtual Gamepad's; the guid_index before the underscore tells Cemu which pad with that GUID.
  # Player 1 is a Wii U GamePad; players 2-4 are Pro Controllers, as Cemu supports two gamepads.
  cemuGamepadMappings = [
    [1 1] [2 0] [3 3] [4 2] [5 9] [6 10] [7 42] [8 43] [9 6] [10 4]
    [11 11] [12 12] [13 13] [14 14] [15 7] [16 8]
    [17 45] [18 39] [19 44] [20 38] [21 47] [22 41] [23 46] [24 40]
  ];
  cemuProMappings = [
    [1 1] [2 0] [3 3] [4 2] [5 9] [6 10] [7 42] [8 43] [9 6] [10 4]
    [12 11] [13 12] [14 13] [15 14] [16 7] [17 8]
    [18 45] [19 39] [20 44] [21 38] [22 47] [23 41] [24 46] [25 40]
  ];
  cemuController = { index, type, mappings }: writeText "cemu-controller${toString index}.xml" ''
    <?xml version="1.0" encoding="UTF-8"?>
    <emulated_controller>
        <type>${type}</type>
        <controller>
            <api>SDLController</api>
            <uuid>${toString index}_030079f6de280000ff11000001000000</uuid>
            <display_name>Steam Virtual Gamepad</display_name>
            <axis>
                <deadzone>0.25</deadzone>
                <range>1</range>
            </axis>
            <rotation>
                <deadzone>0.25</deadzone>
                <range>1</range>
            </rotation>
            <trigger>
                <deadzone>0.25</deadzone>
                <range>1</range>
            </trigger>
            <mappings>
    ${lib.concatMapStrings (m: "            <entry><mapping>${toString (builtins.elemAt m 0)}</mapping><button>${toString (builtins.elemAt m 1)}</button></entry>
") mappings}        </mappings>
        </controller>
    </emulated_controller>
  '';
  cemuController0 = cemuController { index = 0; type = "Wii U GamePad"; mappings = cemuGamepadMappings; };
  cemuController1 = cemuController { index = 1; type = "Wii U Pro Controller"; mappings = cemuProMappings; };
  cemuController2 = cemuController { index = 2; type = "Wii U Pro Controller"; mappings = cemuProMappings; };
  cemuController3 = cemuController { index = 3; type = "Wii U Pro Controller"; mappings = cemuProMappings; };
  cemuWrapper = writeShellScript "cemu" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe cemu} "$@"
  '';
  # Eden's starting qt-config.ini, copied read-write on its first run; the `eden` patch goes on top
  # (storage paths, Vulkan, fullscreen, the Switch content folder). Player N is Steam's virtual pad
  # N (design.md § Controller rule): the same SDL GUID and port N-1, with Eden's own face layout.
  edenGuid = "030079f6de280000ff11000001000000";
  edenButtons = [
    ["a" 1] ["b" 0] ["x" 3] ["y" 2]
    ["lstick" 7] ["rstick" 8] ["l" 9] ["r" 10]
    ["plus" 6] ["minus" 4]
    ["dleft" 13] ["dup" 11] ["dright" 14] ["ddown" 12]
    ["slleft" 9] ["srleft" 10] ["slright" 9] ["srright" 10]
    ["home" 5] ["screenshot" 15]
  ];
  edenPlayer = port: ''
    player_${toString port}_connected=true
    player_${toString port}_type=0
    ${lib.concatMapStrings (b: ''player_${toString port}_button_${builtins.elemAt b 0}="engine:sdl,guid:${edenGuid},port:${toString port},button:${toString (builtins.elemAt b 1)}"
    '') edenButtons}
    player_${toString port}_button_zl="engine:sdl,guid:${edenGuid},port:${toString port},axis:4,threshold:0.5,invert:+"
    player_${toString port}_button_zr="engine:sdl,guid:${edenGuid},port:${toString port},axis:5,threshold:0.5,invert:+"
    player_${toString port}_lstick="engine:sdl,guid:${edenGuid},port:${toString port},axis_x:0,axis_y:1,offset_x:0,offset_y:0,invert_x:+,invert_y:+"
    player_${toString port}_rstick="engine:sdl,guid:${edenGuid},port:${toString port},axis_x:2,axis_y:3,offset_x:0,offset_y:0,invert_x:+,invert_y:+"
  '';
  edenIni = writeText "eden-default.ini" ''
    [Controls]
    ${lib.concatMapStrings edenPlayer (lib.range 0 3)}
    [Renderer]
    backend=1
    resolution_setup=3
    use_vsync=2
    fullscreen_mode=1

    [System]
    use_docked_mode=1

    [Miscellaneous]
    first_launch=false

    [UI]
    fullscreen=true
    firstStart=false
    confirmStop=2
    check_for_updates=false
    enable_gamemode=false
    pauseWhenInBackground=false
    singleWindowMode=true
    hideInactiveMouse=true
  '';
  edenWrapper = writeShellScript "eden" ''
    exec ${lib.getExe nixgl.nixGLIntel} ${lib.getExe nixgl.nixVulkanIntel} ${lib.getExe eden} "$@"
  '';
in
runCommand "romm-shelf-emulators-${retroarch-bare.version}" { } ''
  # Tools for Windows native games (design.md § Native games, Windows games): the squashfs is
  # mounted read-only and bubblewrap overlays it with a writable layer. Bundled so the AppImage
  # doesn't depend on the host having them.
  mkdir -p $out/bin
  ln -s ${lib.getExe' bubblewrap "bwrap"} $out/bin/bwrap
  ln -s ${lib.getExe' squashfuse "squashfuse"} $out/bin/squashfuse
  ln -s ${lib.getExe' squashfsTools "unsquashfs"} $out/bin/unsquashfs
  ln -s ${lib.getExe' squashfsTools "mksquashfs"} $out/bin/mksquashfs
  ln -s ${lib.getExe' fuse3 "fusermount3"} $out/bin/fusermount3

  ra=$out/retroarch
  mkdir -p $ra/bin $ra/cores $ra/info
  ln -s ${retroarch} $ra/bin/retroarch
  # libGL.so.1 for Steam's overlay: Steam preloads gameoverlayrenderer.so into every process of a
  # game, and Nix's loader only finds its libGL dependency on LD_LIBRARY_PATH. romm-shelf puts this
  # first when it starts the emulator (emulators.py), so the wrapper, nixGL and RetroArch all load.
  [ -e ${libglvnd}/lib/libGL.so.1 ] || { echo "no libGL.so.1 in libglvnd"; exit 1; }
  ln -s ${libglvnd}/lib $ra/lib
  # Each core's .so and its .info: a missing one fails the build rather than a game later.
  ${lib.concatStrings (lib.mapAttrsToList (stem: pkg: ''
    so=$(find -L ${pkg} -name '${stem}.so' | head -1)
    info=$(find -L ${libretro-core-info} -name '${stem}.info' | head -1)
    [ -n "$so" ] && [ -n "$info" ] || { echo "${stem}: no .so in ${pkg} or no .info"; exit 1; }
    ln -s "$so" $ra/cores/
    ln -s "$info" $ra/info/
  '') cores)}
  assets=$(find -L ${retroarch-assets} -type d -name assets | head -1)
  autoconfig=$(find -L ${retroarch-joypad-autoconfig} -type d -name autoconfig | head -1)
  [ -n "$assets" ] && [ -n "$autoconfig" ] || { echo "no assets or autoconfig folder"; exit 1; }
  # Of the 400 MB of shared files, what a run needs: the Ozone menu, its fonts and sounds.
  # Wallpapers, the other menus' themes and the rest are left out.
  mkdir $ra/assets
  for d in ozone pkg sounds fonts; do
    [ -d "$assets/$d" ] || { echo "no assets/$d"; exit 1; }
    ln -s "$assets/$d" $ra/assets/$d
  done
  ln -s "$autoconfig" $ra/autoconfig
  # All of libretro's slang shaders (75 MB): the crt-shader patch's presets reference them, and
  # RetroArch's menu can pick any of them. Slang needs the vulkan or glcore driver.
  slang=${libretro-shaders-slang}/share/libretro/shaders/shaders_slang
  [ -f "$slang/crt/crt-guest-advanced-fast.slangp" ] && [ -f "$slang/stock.slang" ] || { echo "no slang shaders"; exit 1; }
  ln -s "$slang" $ra/shaders
  cp ${defaultCfg} $ra/default.cfg

  # Dolphin under the same GL and Vulkan wrappers, with libGL for Steam's overlay. Its Sys folder
  # is found by the store path it was built with.
  dol=$out/dolphin
  mkdir -p $dol/bin
  ln -s ${dolphin} $dol/bin/dolphin-emu
  ln -s ${libglvnd}/lib $dol/lib
  cp ${dolphinIni} $dol/default.ini

  # PCSX2 under the same wrappers. It finds its resources from the store path it was built with;
  # --datapath is given at launch, so only the program and libGL live here.
  ps2=$out/pcsx2
  mkdir -p $ps2/bin
  ln -s ${pcsx2Wrapper} $ps2/bin/pcsx2-qt
  ln -s ${libglvnd}/lib $ps2/lib
  cp ${pcsx2Ini} $ps2/default.ini

  # PPSSPP under the same wrappers. Its assets are found from the store path its own wrapper
  # execs; the memory stick is given through XDG_CONFIG_HOME at launch.
  psp=$out/ppsspp
  mkdir -p $psp/bin
  ln -s ${ppssppWrapper} $psp/bin/ppsspp
  ln -s ${libglvnd}/lib $psp/lib
  cp ${ppssppIni} $psp/default.ini

  # RPCS3 under the same wrappers. Its config and input seeds are copied on first run; the user
  # folder is given through XDG_CONFIG_HOME at launch.
  rpcs=$out/rpcs3
  mkdir -p $rpcs/bin $rpcs/input
  ln -s ${rpcs3Wrapper} $rpcs/bin/rpcs3
  ln -s ${libglvnd}/lib $rpcs/lib
  cp ${rpcs3Defaults} $rpcs/default.yml
  cp ${rpcs3Input} $rpcs/input/Default.yml
  cp ${rpcs3Profiles} $rpcs/input/active_profiles.yml
  cp ${rpcs3Configs} $rpcs/input/active_input_configurations.yml

  # Azahar under the same wrappers. Its config seed is copied on first run; storage paths and
  # renderer settings are patched after that.
  aza=$out/azahar
  mkdir -p $aza/bin $aza/default
  ln -s ${azaharWrapper} $aza/bin/azahar
  ln -s ${libglvnd}/lib $aza/lib
  cp ${azaharIni} $aza/default/qt-config.ini

  # Cemu under the same wrappers. Its settings and controller seeds are copied on first run; the
  # MLC folder is given through settings.xml at launch.
  cem=$out/cemu
  mkdir -p $cem/bin $cem/default $cem/input/controllerProfiles
  ln -s ${cemuWrapper} $cem/bin/cemu
  ln -s ${libglvnd}/lib $cem/lib
  cp ${cemuSettings} $cem/default/settings.xml
  cp ${cemuController0} $cem/input/controllerProfiles/controller0.xml
  cp ${cemuController1} $cem/input/controllerProfiles/controller1.xml
  cp ${cemuController2} $cem/input/controllerProfiles/controller2.xml
  cp ${cemuController3} $cem/input/controllerProfiles/controller3.xml

  # Eden under the same wrappers. Its config seed is copied on first run; storage paths, renderer
  # and the Switch updates/DLC folder are patched after that. Keys and firmware stay in bios/.
  edenOut=$out/eden
  mkdir -p $edenOut/bin $edenOut/default
  ln -s ${edenWrapper} $edenOut/bin/eden
  ln -s ${libglvnd}/lib $edenOut/lib
  cp ${edenIni} $edenOut/default/qt-config.ini

''
