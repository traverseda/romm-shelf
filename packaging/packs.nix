# Per-emulator OCI images (notes-ghcr-emulator-packs.md).
#
# Each image is a transport for one emulator bundle and its Nix store closure. The AppImage and the
# streaming image pull the same images from GHCR; the pack manager reads the labels below and binds
# the extracted store at /nix under bwrap.
{ lib, dockerTools, emulatorsFor, version }:

let
  names = [ "retroarch" "dolphin" "pcsx2" "ppsspp" "rpcs3" "azahar" "cemu" "eden" ];
  mkPack = name:
    let
      bundle = emulatorsFor [ name ];
      packVersion = bundle.versions.${name};
      image = dockerTools.buildLayeredImage {
        name = "romm-shelf-pack-${name}";
        tag = packVersion;
        contents = [ bundle ];
        maxLayers = 120;
        config.Labels = {
          "org.opencontainers.image.source" = "https://github.com/traverseda/romm-shelf";
          "org.opencontainers.image.title" = "romm-shelf ${name} emulator pack";
          "org.romm-shelf.pack" = name;
          "org.romm-shelf.emulators" = "${bundle}";
          "org.romm-shelf.version" = packVersion;
          "org.romm-shelf.min-core" = version;
        };
      };
    in image // {
      pack = { inherit name; version = packVersion; minCore = version; };
    };
in
lib.genAttrs names mkPack
