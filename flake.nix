{
  description = "romm-shelf: a controller-driven RomM client for RetroDECK";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  inputs.nix-appimage = {
    url = "github:ralismark/nix-appimage";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  # GL and Vulkan for the Nix-built emulators on any host (design.md § Bundled emulators). It
  # follows our nixpkgs, so its Mesa and the emulators share one glibc.
  inputs.nixgl = {
    url = "github:nix-community/nixGL";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nix-appimage, nixgl }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ nixgl.overlay ];
        # Non-commercial licences, the same cores libretro ships; nothing else unfree comes in.
        config.allowUnfreePredicate = p: builtins.elem (nixpkgs.lib.getName p) [ "libretro-snes9x" "libretro-genesis-plus-gx" ];
      };
      lib = pkgs.lib;
      py = pkgs.python313Packages;
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
      emulators = pkgs.callPackage ./packaging/emulators.nix { };

      # nixpkgs' ansible-core propagates every collection (the `ansible` package, 560 MB) and the
      # network libraries they use. Patches run on localhost with ansible.builtin, so only what
      # ansible-core itself requires.
      # Exact versions, the same as pyproject.toml's: patches run under Mitogen, which lags new
      # ansible-core releases, so neither moves when nixpkgs does. Change them together.
      ansible-core = py.ansible-core.overridePythonAttrs (old: rec {
        version = "2.21.1";
        src = py.fetchPypi { pname = "ansible_core"; inherit version; hash = "sha256-pVNuzpW+hN4VISs2RM276cvZ79YuTopUTNawsnoIMDk="; };
        dependencies = with py; [ jinja2 pyyaml cryptography packaging resolvelib ];
        propagatedBuildInputs = [ ];
      });
      mitogen = py.mitogen.overridePythonAttrs (old: rec {
        version = "0.3.54";
        src = py.fetchPypi { inherit (old) pname; inherit version; hash = "sha256-8TGYn1egR0ljGLn1JKFJg7HemKwLXqba25WijbbKt50="; };
      });

      romm-shelf = py.buildPythonApplication {
        pname = project.name;
        inherit (project) version;
        pyproject = true;
        src = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions [ ./pyproject.toml ./README.md ./src ./tests ];
        };
        build-system = [ py.hatchling ];
        # pyproject.toml's dependencies, from nixpkgs rather than uv.lock.
        dependencies = with py; [
          fastapi uvicorn httpx loguru pydantic pydantic-settings typer jeepney cryptography pyyaml
        ] ++ [ ansible-core mitogen ];
        # The bundled emulators (design.md § Bundled emulators), unless the environment names others.
        # A shell for Ansible's local connection, last on PATH so the host's tools still come first.
        makeWrapperArgs = [ "--set-default" "ROMM_SHELF_EMULATORS" "${emulators}"
                            "--suffix" "PATH" ":" "${pkgs.bash}/bin" ];
        # pgrep: the tests check whether ES-DE is running. At run time the host's is used, like
        # systemctl and steamos-add-to-steam.
        nativeCheckInputs = [ py.pytestCheckHook pkgs.procps ];
        meta.mainProgram = "romm-shelf";
      };
    in {
      packages.${system} = {
        inherit romm-shelf emulators;
        default = romm-shelf;
        # One file for the Deck (design.md § Distribution): the same store paths as a NixOS install.
        appimage = nix-appimage.lib.${system}.mkAppImage {
          program = lib.getExe romm-shelf;
          name = "romm-shelf-${romm-shelf.version}-x86_64.AppImage";
        };
      };
      # For testing from the working tree: `nix develop -c uv run romm-shelf serve --ui` runs the
      # source as it stands, with the same bundled emulators the package and AppImage carry.
      devShells.${system}.default = pkgs.mkShell {
        packages = [ pkgs.uv pkgs.python313 ];
        ROMM_SHELF_EMULATORS = "${emulators}";
        # A game's Steam shortcut runs the checkout's romm-shelf without this shell's environment:
        # it finds the emulators through this link (emulators.py CHECKOUT_EMULATORS), a GC root.
        shellHook = ''
          [ -f flake.nix ] && nix-store --add-root result-emulators --realise ${emulators} >/dev/null
        '';
      };
    };
}
