# romm-shelf as one AppImage for the Deck (design.md § Distribution).
#
# A plain AppImage: python-build-standalone's relocatable Python, the PyPI wheels uv.lock pins,
# and romm-shelf's own wheel. No /nix inside and no user-namespace chroot (what nix-appimage and
# nix-bundle do), because romm-shelf starts host programs that need one of their own: Flatpak
# Firefox's bwrap gets EPERM from unshare() inside a chroot.
#
# Emulators are not inside: the pack manager pulls them from GHCR (notes-ghcr-emulator-packs.md).
# The static tools here are for native/Proton games, which need bwrap, squashfuse and mksquashfs
# on the host; they are copied in because a no-chroot AppImage cannot use /nix/store symlinks.
{ lib, stdenvNoCC, fetchurl, python3, squashfsTools, romm-shelf, lockFile, pkgsStatic }:

let
  pyVersion = "3.13";
  python = fetchurl {
    url = "https://releases.astral.sh/github/python-build-standalone/releases/download/20260602/cpython-3.13.13%2B20260602-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz";
    hash = "sha256-wknSuWr2kSw5EPI0uWqo0g0w6LDEbQWE+pVtencDbqk=";
  };
  runtime = fetchurl {
    url = "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64";
    hash = "sha256-L8qLRDySUQ8Ug6iD9gBhrQm0a5eLJjHIB82HOkfsJg0=";
  };

  # Wheels straight from uv.lock: the packages romm-shelf needs at run time (not the dev group),
  # following each package's dependencies. Only dependencies for another OS are left out; anything
  # else a marker might exclude is harmless to ship.
  lock = builtins.fromTOML (builtins.readFile lockFile);
  byName = builtins.listToAttrs (map (p: { name = p.name; value = p; }) lock.package);
  root = byName.${romm-shelf.pname};
  otherOs = d: d ? marker && builtins.match ".*(win32|darwin|Windows|emscripten).*" d.marker != null;
  deps = p: map (d: d.name) (builtins.filter (d: !otherOs d) (p.dependencies or [ ]));
  closure = map (x: x.key) (builtins.genericClosure {
    startSet = map (n: { key = n; }) (deps root);
    operator = x: map (n: { key = n; }) (deps byName.${x.key});
  });

  fits = w: let f = baseNameOf w.url; in
    lib.hasSuffix "-none-any.whl" f
    || ((lib.hasInfix "-cp313-cp313-" f || lib.hasInfix "-abi3-" f)
        && lib.hasInfix "manylinux" f && lib.hasSuffix "_x86_64.whl" f);
  wheel = name:
    let
      p = byName.${name};
      fitting = builtins.filter fits (p.wheels or [ ]);
      # The oldest glibc baseline when there's a choice; SteamOS is newer than all of them.
      w = lib.findFirst (w: lib.hasInfix "manylinux_2_17" w.url || lib.hasInfix "manylinux2014" w.url)
            (if fitting == [ ] then throw "${name} ${p.version}: no wheel for CPython ${pyVersion} on x86_64 Linux in uv.lock" else builtins.head fitting)
            fitting;
    in fetchurl { inherit (w) url hash; name = baseNameOf w.url; };
  wheels = map wheel closure;

  appRun = ''
    #!/bin/sh
    # sys.argv[0] is only the name in --help; units and shortcuts record $APPIMAGE (config.program()).
    here="''${APPDIR:-$(dirname "$(readlink -f "$0")")}"
    # Native/Proton games use these instead of an all-in-one emulator bundle. Emulators themselves
    # come from installed packs (packs.py), which bind their store at /nix inside bwrap.
    export ROMM_SHELF_TOOLS="$here/bin"
    # squashfuse finds fusermount3 on PATH; bwrap and the squashfs tools are the AppImage's too.
    export PATH="$here/bin:$PATH"
    exec "$here/python/bin/python3" -I -c 'import sys; sys.argv[0] = "romm-shelf"; from romm_shelf.cli import app; app()' "$@"
  '';
in
stdenvNoCC.mkDerivation {
  pname = "romm-shelf-appimage";
  inherit (romm-shelf) version;
  dontUnpack = true;
  nativeBuildInputs = [ python3 python3.pkgs.pip squashfsTools ];

  buildPhase = ''
    runHook preBuild
    mkdir AppDir
    tar -xzf ${python} -C AppDir   # python/
    # Static tools, copied out of the store: the AppImage must not need /nix on the host.
    mkdir -p AppDir/bin
    cp ${lib.getExe pkgsStatic.bubblewrap} AppDir/bin/bwrap
    cp ${lib.getExe' pkgsStatic.squashfuse "squashfuse_ll"} AppDir/bin/squashfuse_ll
    cp ${lib.getExe' pkgsStatic.squashfsTools "unsquashfs"} AppDir/bin/unsquashfs
    cp ${lib.getExe' pkgsStatic.squashfsTools "mksquashfs"} AppDir/bin/mksquashfs
    cp ${lib.getExe' pkgsStatic.fuse3 "fusermount3"} AppDir/bin/fusermount3
    site=AppDir/python/lib/python${pyVersion}/site-packages
    # pip reads the tags from the file name, which a store path prefixes with its hash.
    mkdir wheels
    ${lib.concatMapStrings (w: "ln -s ${w} wheels/${w.name}\n") wheels}
    ln -s ${romm-shelf.dist}/*.whl wheels/
    # Installed with Nix's pip for the standalone Python's tags; nothing is run from the wheels.
    HOME=$TMPDIR python3 -m pip install --quiet --no-index --no-deps --no-compile --disable-pip-version-check \
      --target "$site" --implementation cp --python-version ${pyVersion} --abi cp313 \
      --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 \
      --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
      --only-binary=:all: wheels/*.whl
    rm -rf "$site/bin"
    # Same minor version, so Nix's Python writes bytecode the standalone one accepts.
    python3 -m compileall -q -j 0 --invalidation-mode unchecked-hash "$site"
    cat > AppDir/AppRun <<'EOF'
    ${appRun}
    EOF
    sed -i 's/^    //' AppDir/AppRun
    chmod +x AppDir/AppRun
    mksquashfs AppDir app.squashfs -root-owned -noappend -comp zstd -quiet  # timestamps from SOURCE_DATE_EPOCH
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cat ${runtime} app.squashfs > $out/romm-shelf-$version-x86_64.AppImage
    chmod 755 $out/romm-shelf-$version-x86_64.AppImage
    runHook postInstall
  '';

  passthru = { inherit wheels closure; };
  meta.platforms = [ "x86_64-linux" ];
}
