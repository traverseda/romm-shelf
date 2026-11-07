# The emulators romm-shelf bundles (design.md § Bundled emulators): $out/retroarch/ and $out/dolphin/.
#
# RetroArch and its cores, and Dolphin, from nixpkgs, for every target: a NixOS host runs them from
# the store, and the AppImage (nix-appimage) carries the same store paths. The layout is what
# emulators.py and romm_shelf_dolphin expect: retroarch/{bin/retroarch, cores/, info/, assets/,
# autoconfig/, default.cfg} and dolphin/{bin/dolphin-emu, lib, default.ini}.
{ lib, runCommand, writeShellScript, nixgl, libglvnd, retroarch-bare, libretro, libretro-core-info, retroarch-assets,
  retroarch-joypad-autoconfig, libretro-shaders-slang, dolphin-emu }:

let
  # Exactly the cores romm_shelf_retroarch declares: file stem -> nixpkgs package.
  cores = {
    mesen_libretro = libretro.mesen;
    snes9x_libretro = libretro.snes9x;
    gambatte_libretro = libretro.gambatte;
    mgba_libretro = libretro.mgba;
    genesis_plus_gx_libretro = libretro.genesis-plus-gx;
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
in
runCommand "romm-shelf-emulators-${retroarch-bare.version}" { } ''
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
''
